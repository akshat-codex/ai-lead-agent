from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.human_review import PipelineSnapshot
from app.schemas.lead_confidence import ReadinessLevel, ReadinessReasonCode
from app.services.lead_confidence import classify_readiness

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(**overrides) -> PipelineSnapshot:
    base = dict(
        company_id="company-1",
        person_id=None,
        hard_rule_result=None,
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


def _evidence(field, value, entity_type=EntityType.COMPANY, entity_id="company-1", **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()), entity_type=entity_type, entity_id=entity_id, field=field, value=value,
        source_provider_id="provider-a", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _all_critical_company_evidence(company_id="company-1"):
    fields = {
        "company_identity": "Acme Inc", "domain": "acme.invalid", "industry": "Skincare",
        "employee_range": {"min": 11, "max": 50}, "country": "US", "company_type": "D2C",
        "business_model": "DTC", "linkedin_id": "acme-inc",
    }
    records = []
    for field, value in fields.items():
        records.append(_evidence(field, value, entity_id=company_id, source_provider_id="provider-a"))
        records.append(_evidence(field, value, entity_id=company_id, source_provider_id="provider-b"))
    return records


# --- fully supported lead -------------------------------------------


def test_fully_supported_pass_lead_is_verified():
    snapshot = _snapshot(hard_rule_result="PASS", evidence_missing_critical_fields=())
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.VERIFIED
    assert ReadinessReasonCode.CRITICAL_FIELDS_FULLY_SUPPORTED in result.reason_codes


def test_verified_lead_shows_supporting_evidence_ids():
    evidence = _all_critical_company_evidence()
    snapshot = _snapshot(hard_rule_result="PASS")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", evidence, [], generated_at=NOW)
    industry_item = next(i for i in result.supporting_evidence if i.field == "industry")
    assert industry_item.status == "SUPPORTED"
    assert len(industry_item.evidence_ids) == 2


# --- missing evidence --------------------------------------------------


def test_pass_with_missing_critical_fields_is_partially_verified():
    snapshot = _snapshot(hard_rule_result="PASS", evidence_missing_critical_fields=("domain", "linkedin_id"))
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", [_evidence("industry", "Skincare")], [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.PARTIALLY_VERIFIED
    assert ReadinessReasonCode.CRITICAL_FIELDS_MISSING in result.reason_codes
    assert "domain" in result.missing_critical_fields


def test_missing_evidence_never_fabricated_as_supported():
    snapshot = _snapshot(hard_rule_result="PASS", evidence_missing_critical_fields=("domain",))
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", [], [], generated_at=NOW)
    domain_item = next((i for i in result.supporting_evidence if i.field == "domain"), None)
    assert domain_item is not None
    assert domain_item.status == "UNKNOWN"
    assert domain_item.evidence_ids == ()


# --- conflicting evidence ------------------------------------------------


def test_conflicting_evidence_is_conflicted_regardless_of_hard_rule_pass():
    snapshot = _snapshot(hard_rule_result="PASS", evidence_conflicts=("industry",))
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", [], [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.CONFLICTED
    assert ReadinessReasonCode.EVIDENCE_CONFLICTS_PRESENT in result.reason_codes


def test_conflict_takes_priority_over_hard_fail():
    """A conflict is surfaced as CONFLICTED even when the hard rule
    already failed for an unrelated reason — both facts matter, and
    CONFLICTED is the more specific, actionable signal."""
    snapshot = _snapshot(hard_rule_result="FAIL", evidence_conflicts=("industry",))
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", [], [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.CONFLICTED


# --- hard FAIL -----------------------------------------------------------


def test_hard_fail_is_insufficient_evidence_never_verified():
    snapshot = _snapshot(hard_rule_result="FAIL")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.INSUFFICIENT_EVIDENCE
    assert ReadinessReasonCode.HARD_RULE_FAIL in result.reason_codes
    assert result.readiness != ReadinessLevel.VERIFIED
    assert result.readiness != ReadinessLevel.PARTIALLY_VERIFIED


def test_hard_fail_never_promoted_even_with_perfect_qualification_and_human_accept():
    snapshot = _snapshot(hard_rule_result="FAIL", qualification_decision="GOOD_FIT", adversarial_result="SURVIVES")
    result = classify_readiness(
        "icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [],
        human_review_decision="ACCEPT", generated_at=NOW,
    )
    assert result.readiness == ReadinessLevel.INSUFFICIENT_EVIDENCE


# --- hard HOLD -------------------------------------------------------


def test_hard_hold_is_insufficient_evidence():
    snapshot = _snapshot(hard_rule_result="HOLD")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", [_evidence("industry", "Skincare")], [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.INSUFFICIENT_EVIDENCE
    assert ReadinessReasonCode.HARD_RULE_HOLD in result.reason_codes


# --- qualification / adversarial disagreement -----------------------------


def test_good_fit_disproved_by_adversarial_is_flagged_as_disagreement():
    snapshot = _snapshot(hard_rule_result="PASS", qualification_decision="GOOD_FIT", adversarial_result="DISPROVED")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert ReadinessReasonCode.QUALIFICATION_ADVERSARIAL_DISAGREEMENT in result.reason_codes
    # still VERIFIED at the evidence level - disagreement is a diagnostic flag, not an override
    assert result.readiness == ReadinessLevel.VERIFIED


def test_not_fit_survived_by_adversarial_is_also_flagged():
    snapshot = _snapshot(hard_rule_result="PASS", qualification_decision="NOT_FIT", adversarial_result="SURVIVES")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert ReadinessReasonCode.QUALIFICATION_ADVERSARIAL_DISAGREEMENT in result.reason_codes


def test_agreeing_qualification_and_adversarial_produces_no_disagreement_flag():
    snapshot = _snapshot(hard_rule_result="PASS", qualification_decision="GOOD_FIT", adversarial_result="SURVIVES")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert ReadinessReasonCode.QUALIFICATION_ADVERSARIAL_DISAGREEMENT not in result.reason_codes


# --- human ACCEPT/REJECT --------------------------------------------


def test_human_accept_is_surfaced_but_does_not_change_readiness_level():
    snapshot = _snapshot(hard_rule_result="PASS")
    result = classify_readiness(
        "icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [],
        human_review_decision="ACCEPT", generated_at=NOW,
    )
    assert ReadinessReasonCode.HUMAN_ACCEPTED in result.reason_codes
    assert result.readiness == ReadinessLevel.VERIFIED  # unchanged by human decision
    assert result.human_review_decision == "ACCEPT"


def test_human_reject_is_surfaced_but_does_not_rescue_or_worsen_readiness():
    snapshot = _snapshot(hard_rule_result="PASS")
    result = classify_readiness(
        "icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [],
        human_review_decision="REJECT", generated_at=NOW,
    )
    assert ReadinessReasonCode.HUMAN_REJECTED in result.reason_codes
    assert result.readiness == ReadinessLevel.VERIFIED  # evidence-level readiness is independent of the human decision


# --- multiple ICPs ----------------------------------------------------


def test_same_signals_different_icp_scope_are_independent():
    snapshot = _snapshot(hard_rule_result="PASS")
    result_a = classify_readiness("icp-a", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    result_b = classify_readiness("icp-b", 7, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert result_a.icp_id == "icp-a"
    assert result_b.icp_id == "icp-b"
    assert result_b.icp_version == 7
    assert result_a.readiness == result_b.readiness  # same underlying data -> same classification, independent of ICP identity


# --- deterministic results -----------------------------------------------


def test_classification_is_deterministic():
    snapshot = _snapshot(hard_rule_result="PASS")
    evidence = _all_critical_company_evidence()
    first = classify_readiness("icp-1", 1, snapshot, "lead-1", evidence, [], generated_at=NOW)
    second = classify_readiness("icp-1", 1, snapshot, "lead-1", evidence, [], generated_at=NOW)
    assert first == second


# --- evidence provenance --------------------------------------------


def test_supporting_evidence_ids_are_real_and_traceable():
    evidence = _all_critical_company_evidence()
    all_ids = {e.id for e in evidence}
    snapshot = _snapshot(hard_rule_result="PASS")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", evidence, [], generated_at=NOW)
    for item in result.supporting_evidence:
        for eid in item.evidence_ids:
            assert eid in all_ids  # never a fabricated id


def test_person_evidence_included_when_person_id_present():
    company_evidence = _all_critical_company_evidence()
    person_evidence = [
        _evidence("person_identity", "Jane Doe", entity_type=EntityType.PERSON, entity_id="person-1"),
        _evidence("current_title", "CMO", entity_type=EntityType.PERSON, entity_id="person-1"),
    ]
    snapshot = _snapshot(hard_rule_result="PASS", person_id="person-1")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", company_evidence, person_evidence, generated_at=NOW)
    person_items = [i for i in result.supporting_evidence if i.entity_type == "PERSON"]
    assert len(person_items) > 0


# --- no fabrication -------------------------------------------------


def test_no_pipeline_data_at_all_is_unknown_not_guessed():
    snapshot = _snapshot(hard_rule_result=None)
    result = classify_readiness("icp-1", 1, snapshot, None, [], [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.UNKNOWN
    assert ReadinessReasonCode.NO_PIPELINE_DATA in result.reason_codes
    assert result.lead_id is None


def test_some_evidence_but_no_hard_validation_yet_is_insufficient_not_verified():
    snapshot = _snapshot(hard_rule_result=None)
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", [_evidence("industry", "Skincare")], [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.INSUFFICIENT_EVIDENCE
    assert ReadinessReasonCode.HARD_RULE_UNKNOWN in result.reason_codes


def test_duplicate_occurrence_flag_never_changes_readiness_level():
    snapshot = _snapshot(hard_rule_result="PASS")
    result = classify_readiness(
        "icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [],
        is_duplicate_occurrence=True, generated_at=NOW,
    )
    assert ReadinessReasonCode.DUPLICATE_OCCURRENCE in result.reason_codes
    assert result.readiness == ReadinessLevel.VERIFIED


# --- structural safety: no second scoring/qualification/ranking system ----


def test_module_never_imports_mutating_pipeline_functions():
    import ast
    import inspect

    import app.services.lead_confidence as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "evaluate_hard_rules", "validate_against_icp", "score_lead", "qualify_lead",
        "run_adversarial_review", "decide_review", "rank_leads", "deduplicate_lead",
    }
    assert not (forbidden & imported_names)


def test_module_has_no_database_or_persistence_calls():
    import inspect

    import app.services.lead_confidence as module

    source = inspect.getsource(module)
    for forbidden in ("db.add(", "db.commit(", ".query(", "Session"):
        assert forbidden not in source


# --- verification unresolved still degrades from VERIFIED ------------------


def test_unresolved_verification_downgrades_fully_supported_pass_to_partial():
    snapshot = _snapshot(hard_rule_result="PASS", latest_verification_outcome="UNRESOLVED")
    result = classify_readiness("icp-1", 1, snapshot, "lead-1", _all_critical_company_evidence(), [], generated_at=NOW)
    assert result.readiness == ReadinessLevel.PARTIALLY_VERIFIED
    assert ReadinessReasonCode.VERIFICATION_UNRESOLVED in result.reason_codes
