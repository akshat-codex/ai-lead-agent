from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.feedback import FeedbackDecision
from app.schemas.human_review import (
    PipelineSnapshot,
    ReviewDecision,
    ReviewExecutionStatus,
    ReviewRequest,
    feedback_decision_for,
)
from app.services.human_review import decide_review
from app.services.review_snapshot import build_pipeline_snapshot

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(hard_rule_result=None, **overrides) -> PipelineSnapshot:
    base = dict(
        company_id="company-1",
        person_id="person-1",
        hard_rule_result=hard_rule_result,
        hard_rule_reason_codes=(),
        final_score=None,
        icp_score=None,
        commercial_score=None,
        evidence_score=None,
        qualification_decision=None,
        qualification_confidence=None,
        qualification_summary=None,
        adversarial_result=None,
        adversarial_confidence=None,
        evidence_conflicts=(),
        evidence_missing_critical_fields=(),
        latest_verification_outcome=None,
    )
    base.update(overrides)
    return PipelineSnapshot(**base)


def _request(decision, **overrides) -> ReviewRequest:
    base = dict(lead_id="lead-1", icp_id="icp-1", decision=decision, reviewer_id="reviewer-1")
    base.update(overrides)
    return ReviewRequest(**base)


# --- ACCEPT/REJECT/HOLD/DUPLICATE -------------------------------------


def test_accept_is_recorded_when_hard_rule_passes():
    result = decide_review(_request(ReviewDecision.ACCEPT), 1, _snapshot(hard_rule_result="PASS"))
    assert result.effective_decision == ReviewDecision.ACCEPT
    assert result.status == ReviewExecutionStatus.RECORDED


def test_reject_is_recorded_regardless_of_pipeline_state():
    result = decide_review(_request(ReviewDecision.REJECT, reason_codes=["POOR_FIT"]), 1, _snapshot(hard_rule_result="PASS"))
    assert result.effective_decision == ReviewDecision.REJECT
    assert result.status == ReviewExecutionStatus.RECORDED


def test_hold_is_recorded():
    result = decide_review(
        _request(ReviewDecision.HOLD, reason_codes=["AWAITING_MORE_EVIDENCE"]), 1, _snapshot(hard_rule_result="HOLD")
    )
    assert result.effective_decision == ReviewDecision.HOLD
    assert result.status == ReviewExecutionStatus.RECORDED


def test_duplicate_is_recorded_with_reference():
    request = _request(ReviewDecision.DUPLICATE, duplicate_of_lead_id="lead-original")
    result = decide_review(request, 1, _snapshot())
    assert result.effective_decision == ReviewDecision.DUPLICATE
    assert result.duplicate_of_lead_id == "lead-original"


def test_duplicate_without_reference_id_is_rejected_by_schema():
    with pytest.raises(Exception):
        ReviewRequest(lead_id="lead-1", icp_id="icp-1", decision=ReviewDecision.DUPLICATE, reviewer_id="r1")


def test_non_duplicate_with_reference_id_is_rejected_by_schema():
    with pytest.raises(Exception):
        ReviewRequest(
            lead_id="lead-1", icp_id="icp-1", decision=ReviewDecision.ACCEPT, reviewer_id="r1", duplicate_of_lead_id="lead-x"
        )


# --- hard-rule failure protection -------------------------------------


def test_accept_is_blocked_and_downgraded_when_hard_rule_fails():
    result = decide_review(_request(ReviewDecision.ACCEPT), 1, _snapshot(hard_rule_result="FAIL"))
    assert result.effective_decision == ReviewDecision.REJECT
    assert result.status == ReviewExecutionStatus.BLOCKED_BY_HARD_FAIL
    assert result.requested_decision == ReviewDecision.ACCEPT


def test_reject_is_not_blocked_by_hard_fail():
    result = decide_review(_request(ReviewDecision.REJECT, reason_codes=["NOT_FIT"]), 1, _snapshot(hard_rule_result="FAIL"))
    assert result.effective_decision == ReviewDecision.REJECT
    assert result.status == ReviewExecutionStatus.RECORDED


def test_hold_is_not_blocked_by_hard_fail():
    result = decide_review(
        _request(ReviewDecision.HOLD, reason_codes=["AWAITING_MORE_EVIDENCE"]), 1, _snapshot(hard_rule_result="FAIL")
    )
    assert result.effective_decision == ReviewDecision.HOLD
    assert result.status == ReviewExecutionStatus.RECORDED


def test_duplicate_is_not_blocked_by_hard_fail():
    request = _request(ReviewDecision.DUPLICATE, duplicate_of_lead_id="lead-original")
    result = decide_review(request, 1, _snapshot(hard_rule_result="FAIL"))
    assert result.effective_decision == ReviewDecision.DUPLICATE
    assert result.status == ReviewExecutionStatus.RECORDED


def test_accept_not_blocked_when_hard_rule_is_hold_not_fail():
    """HOLD is not FAIL — a reviewer may still ACCEPT while acknowledging
    unresolved evidence; only a confirmed FAIL is absolute."""
    result = decide_review(_request(ReviewDecision.ACCEPT), 1, _snapshot(hard_rule_result="HOLD"))
    assert result.effective_decision == ReviewDecision.ACCEPT
    assert result.status == ReviewExecutionStatus.RECORDED


def test_accept_not_blocked_when_no_hard_validation_has_run_yet():
    result = decide_review(_request(ReviewDecision.ACCEPT), 1, _snapshot(hard_rule_result=None))
    assert result.effective_decision == ReviewDecision.ACCEPT


# --- missing/conflicting evidence stays visible -----------------------


def test_snapshot_surfaces_conflicts_and_missing_fields():
    company_evidence = [
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field="industry", value="Skincare",
            source_provider_id="provider-a", source_type=SourceType.PROVIDER, retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field="industry", value="Not Skincare",
            source_provider_id="provider-b", source_type=SourceType.PROVIDER, retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
    ]
    snapshot = build_pipeline_snapshot(
        company_id="company-1", person_id=None, company_evidence=company_evidence, person_evidence=[],
        latest_hard_validation=None, latest_score=None, latest_qualification=None,
        latest_adversarial_review=None, latest_verification_outcome=None,
    )
    assert "industry" in snapshot.evidence_conflicts
    assert "domain" in snapshot.evidence_missing_critical_fields  # a critical field with zero evidence


def test_snapshot_surfaces_unresolved_custom_rule_explanation():
    """P1 fix: a reviewer judging a HOLD lead caused by an unresolved
    custom rule must see the rule's own label/description (baked into the
    engine's explanation text), not just the bare CUSTOM_RULE_UNRESOLVED
    reason code — otherwise they'd have to separately look up the ICP to
    know what they're evaluating."""
    latest_hard_validation = {
        "overall_result": "HOLD",
        "reason_codes": ["CUSTOM_RULE_UNRESOLVED"],
        "rule_results": [
            {"rule": "industry", "status": "PASS", "explanation": "Candidate industry 'SaaS' is allowed."},
            {
                "rule": "custom_rule:Sells to providers",
                "status": "HOLD",
                "explanation": (
                    "No evidence yet on whether the candidate satisfies custom rule "
                    "'Sells to providers' (Must sell to healthcare providers, not payers)."
                ),
            },
        ],
    }
    snapshot = build_pipeline_snapshot(
        company_id="company-1", person_id=None, company_evidence=[], person_evidence=[],
        latest_hard_validation=latest_hard_validation, latest_score=None, latest_qualification=None,
        latest_adversarial_review=None, latest_verification_outcome=None,
    )
    assert len(snapshot.hard_rule_unresolved_explanations) == 1
    explanation = snapshot.hard_rule_unresolved_explanations[0]
    assert "Sells to providers" in explanation
    assert "Must sell to healthcare providers, not payers" in explanation
    # A PASS rule's explanation must never be surfaced here — this field
    # is scoped to what a reviewer needs to resolve, not a full dump.
    assert not any("SaaS" in e for e in snapshot.hard_rule_unresolved_explanations)


def test_snapshot_unresolved_explanations_empty_when_no_validation_has_run():
    snapshot = build_pipeline_snapshot(
        company_id="company-1", person_id=None, company_evidence=[], person_evidence=[],
        latest_hard_validation=None, latest_score=None, latest_qualification=None,
        latest_adversarial_review=None, latest_verification_outcome=None,
    )
    assert snapshot.hard_rule_unresolved_explanations == ()


def test_review_decision_never_fabricates_a_score_or_qualification():
    result = decide_review(_request(ReviewDecision.HOLD, reason_codes=["AWAITING_MORE_EVIDENCE"]), 1, _snapshot())
    assert result.snapshot.final_score is None
    assert result.snapshot.qualification_decision is None
    assert result.snapshot.adversarial_result is None


# --- existing Phase 4 feedback contract reuse -------------------------


def test_accept_maps_to_good_fit_feedback():
    assert feedback_decision_for(ReviewDecision.ACCEPT) == FeedbackDecision.GOOD_FIT


def test_reject_maps_to_not_fit_feedback():
    assert feedback_decision_for(ReviewDecision.REJECT) == FeedbackDecision.NOT_FIT


def test_hold_maps_to_hold_feedback():
    assert feedback_decision_for(ReviewDecision.HOLD) == FeedbackDecision.HOLD


def test_duplicate_has_no_feedback_equivalent():
    assert feedback_decision_for(ReviewDecision.DUPLICATE) is None


def test_service_never_defines_a_second_feedback_decision_enum():
    import inspect

    import app.schemas.human_review as module

    source = inspect.getsource(module)
    assert "class FeedbackDecision" not in source
    assert "from app.schemas.feedback import FeedbackDecision" in source


# --- determinism -------------------------------------------------------


def test_review_decision_is_deterministic():
    request = _request(ReviewDecision.ACCEPT)
    snapshot = _snapshot(hard_rule_result="PASS")
    first = decide_review(request, 1, snapshot)
    second = decide_review(request, 1, snapshot)
    assert first.effective_decision == second.effective_decision
    assert first.status == second.status


# --- no hard-rule / scoring / qualification duplication --------------------


def test_service_never_reimplements_hard_rule_or_scoring_logic():
    import inspect

    import app.services.human_review as review_module
    import app.services.review_snapshot as snapshot_module

    for module in (review_module, snapshot_module):
        source = inspect.getsource(module)
        for forbidden in ("evaluate_hard_rules", "score_lead", "qualify_lead", "run_adversarial_review", "deduplicate_lead"):
            assert forbidden not in source
