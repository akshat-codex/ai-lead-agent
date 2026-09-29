import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.business_model import BusinessModel, BusinessModelClassificationResult, ClassificationStatus
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus, SourceType
from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import QualificationDecision, QualificationExecutionStatus, RawQualificationOutput
from app.schemas.scoring import ResolutionSignal
from app.services.hard_icp_validation import validate_against_icp
from app.services.lead_scoring import score_lead
from app.services.llm_providers.base import LLMProviderError, LLMProviderErrorCode, LLMProviderResponse
from app.services.llm_providers.mock import MockLLMProvider, build_good_fit_response
from app.services.llm_qualification import qualify_lead
from app.services.qualification_context import build_qualification_context

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _icp(
    industries=("Skincare",),
    countries=("US",),
    min_employees=10,
    max_employees=200,
    allowed_titles=(),
    company_types=(),
    exclusions=(),
    business_models=(),
    commercial_signals=(),
    icp_id="icp-1",
    version=1,
) -> CanonicalICP:
    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=CanonicalHardRules(
            industries=industries,
            geography=CanonicalGeography(countries=tuple(GeographyEntry(raw=c, code=c, label=c) for c in countries)),
            employee_range=EmployeeRange(min=min_employees, max=max_employees),
            allowed_titles=allowed_titles,
            company_types=company_types,
            exclusions=exclusions,
        ),
        soft_preferences=CanonicalSoftPreferences(
            business_models=business_models,
            commercial_signals=commercial_signals,
        ),
    )


def _evidence(entity_type, entity_id, field, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        value=value,
        source_provider_id="provider-a",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _company_evidence(company_id="company-1", **kwargs):
    fields = {
        "company_identity": "Acme Skincare Inc",
        "domain": "acme-skincare.invalid",
        "industry": "Skincare",
        "employee_count": 50,
        "country": "US",
    }
    fields.update(kwargs)
    records = []
    for field, value in fields.items():
        if value is None:
            continue
        records.append(_evidence(EntityType.COMPANY, company_id, field, value, source_provider_id="provider-a"))
        records.append(_evidence(EntityType.COMPANY, company_id, field, value, source_provider_id="provider-b"))
    return records


def _build_context(icp, company_evidence, business_model=None, commercial_signals=None, person_id=None, person_evidence=None):
    person_evidence = person_evidence or []
    validation = validate_against_icp(icp, "company-1", company_evidence, person_id, person_evidence)
    score = score_lead(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=business_model,
        commercial_signals=commercial_signals or [],
        person_id=person_id,
        person_evidence=person_evidence,
        person_resolutions=None,
        now=NOW,
    )
    return build_qualification_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals or [],
        score=score,
        person_id=person_id,
        person_evidence=person_evidence,
    )


# --- hard gate absoluteness ---------------------------------------------


def test_hard_fail_never_reaches_the_llm_and_is_rejected():
    icp = _icp(min_employees=1000)  # 50 employees -> FAIL
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=build_good_fit_response(context))  # even a "good" response must not matter
    result = qualify_lead(context, provider)

    assert context.hard_rule_result == OverallResult.FAIL
    assert result.status == QualificationExecutionStatus.HARD_REJECTED
    assert result.decision == QualificationDecision.REJECT


def test_hard_hold_never_becomes_accepted():
    icp = _icp()  # no employee count issue but no title evidence -> irrelevant; force HOLD via missing industry match instead
    icp_holding = _icp(industries=("Something Else",))
    context = _build_context(icp_holding, _company_evidence())
    provider = MockLLMProvider(response_text=build_good_fit_response(context))
    result = qualify_lead(context, provider)

    assert context.hard_rule_result == OverallResult.FAIL  # industry mismatch is a confirmed FAIL, not a HOLD
    # use a genuinely unresolved case instead: no employee_count evidence at all
    context_hold = _build_context(_icp(min_employees=1000), _company_evidence(employee_count=None))
    result_hold = qualify_lead(context_hold, MockLLMProvider(response_text=build_good_fit_response(context_hold)))
    assert context_hold.hard_rule_result == OverallResult.HOLD
    assert result_hold.status == QualificationExecutionStatus.HARD_HOLD
    assert result_hold.decision == QualificationDecision.HOLD


def test_hard_pass_reaches_the_llm():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=build_good_fit_response(context))
    result = qualify_lead(context, provider)

    assert context.hard_rule_result == OverallResult.PASS
    assert result.status == QualificationExecutionStatus.SUCCESS


# --- decision variety ----------------------------------------------------


def _response(decision, evidence_ids, **overrides):
    body = {
        "decision": decision,
        "confidence": 70,
        "reason_codes": [],
        "summary": "test summary",
        "supporting_evidence_ids": evidence_ids,
        "risk_evidence_ids": [],
        "missing_evidence": [],
        "commercial_fit_explanation": "",
        "hard_rule_acknowledgement": "PASS acknowledged",
        "uncertainties": [],
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.mark.parametrize("decision", ["GOOD_FIT", "WEAK_FIT", "NOT_FIT", "HOLD"])
def test_each_valid_decision_is_accepted(decision):
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    evidence_id = context.evidence[0].id
    provider = MockLLMProvider(response_text=_response(decision, [evidence_id]))
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.SUCCESS
    assert result.decision == QualificationDecision(decision)


def test_reject_decision_from_llm_is_rejected_by_schema():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("REJECT", []))
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.SCHEMA_INVALID


# --- anti-hallucination ----------------------------------------------------


def test_malformed_json_output_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text="not json at all {{{")
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.MALFORMED_OUTPUT
    assert result.decision is None


def test_schema_violation_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=json.dumps({"decision": "GOOD_FIT"}))  # missing required fields
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.SCHEMA_INVALID


def test_fabricated_evidence_id_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("GOOD_FIT", ["totally-made-up-id"]))
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.INVALID_EVIDENCE_IDS


def test_fabricated_risk_evidence_id_is_also_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    body = _response("NOT_FIT", [], risk_evidence_ids=["fabricated-risk-id"])
    provider = MockLLMProvider(response_text=body)
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.INVALID_EVIDENCE_IDS


def test_real_evidence_ids_are_accepted_and_preserved():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    real_ids = [e.id for e in context.evidence[:2]]
    provider = MockLLMProvider(response_text=_response("GOOD_FIT", real_ids))
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.SUCCESS
    assert set(result.supporting_evidence_ids) == set(real_ids)


def test_missing_evidence_field_is_preserved_not_dropped():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("HOLD", [], missing_evidence=["current_title"]))
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.SUCCESS
    assert "current_title" in result.missing_evidence


# --- conflicting evidence surfaced to context -----------------------------


def test_conflicting_evidence_is_surfaced_in_context():
    conflicting = _company_evidence() + [
        _evidence(EntityType.COMPANY, "company-1", "industry", "Something Else", source_provider_id="provider-c")
    ]
    icp = _icp()
    context = _build_context(icp, conflicting)
    assert "industry" in context.conflicting_fields


# --- provider failure handling ---------------------------------------------


def test_provider_failure_never_becomes_a_positive_qualification():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(raise_provider_error=True)
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.PROVIDER_ERROR
    assert result.decision is None
    assert result.error_message is not None


def test_provider_timeout_is_preserved_for_audit():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(raise_timeout=True)
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.PROVIDER_TIMEOUT
    assert result.decision is None


def test_empty_response_is_a_distinct_failure_not_a_positive():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(return_empty=True)
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.EMPTY_RESPONSE
    assert result.decision is None


def test_unexpected_provider_exception_is_caught_by_the_base_class():
    class ExplodingProvider(MockLLMProvider):
        def _call(self, context):
            raise RuntimeError("boom")

    icp = _icp()
    context = _build_context(icp, _company_evidence())
    result = qualify_lead(context, ExplodingProvider())
    assert result.status == QualificationExecutionStatus.PROVIDER_ERROR
    assert result.decision is None


# --- deterministic mock provider --------------------------------------------


def test_mock_provider_is_deterministic():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    response_text = build_good_fit_response(context)
    provider = MockLLMProvider(response_text=response_text)
    first = qualify_lead(context, provider)
    second = qualify_lead(context, provider)
    assert first == second


# --- strict schema validation ------------------------------------------------


def test_raw_output_rejects_unknown_fields():
    with pytest.raises(Exception):
        RawQualificationOutput.model_validate(
            {
                "decision": "GOOD_FIT",
                "confidence": 80,
                "summary": "x",
                "unexpected_field": "should not be allowed",
            }
        )


def test_confidence_out_of_bounds_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("GOOD_FIT", [], confidence=150))
    result = qualify_lead(context, provider)
    assert result.status == QualificationExecutionStatus.SCHEMA_INVALID


# --- multi-ICP isolation ---------------------------------------------------


def test_context_is_scoped_to_specific_icp_version():
    icp_v1 = _icp(icp_id="icp-x", version=1, business_models=("DTC",))
    icp_v2 = _icp(icp_id="icp-x", version=2, business_models=("B2B",))
    context_v1 = _build_context(icp_v1, _company_evidence())
    context_v2 = _build_context(icp_v2, _company_evidence())
    assert context_v1.icp_version == 1
    assert context_v2.icp_version == 2
    assert context_v1.icp_soft_preferences != context_v2.icp_soft_preferences


# --- no ICP / score / evidence mutation, no manager feedback ---------------


def test_context_builder_and_qualify_lead_have_no_db_or_manager_feedback_dependency():
    import inspect

    import app.services.llm_qualification as qual_module
    import app.services.qualification_context as ctx_module

    for module in (qual_module, ctx_module):
        source = inspect.getsource(module)
        assert "Session" not in source
        assert "manager_feedback" not in source
        assert "ManagerFeedback" not in source


def test_qualify_lead_never_calls_hard_rule_engine_directly():
    import inspect

    import app.services.llm_qualification as module

    source = inspect.getsource(module)
    assert "evaluate_hard_rules" not in source


def test_context_is_immutable():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    with pytest.raises(Exception):
        context.icp_id = "mutated"


def test_soft_preference_custom_rules_reach_the_qualification_context():
    """Phase 4 fix — root cause: a filter with no dedicated HardRules/
    SoftPreferences field (funding, revenue, technology, ... — see
    app/services/filter_projection.py's own "recognized-but-not-yet-
    field-backed" comment) reaches the canonical ICP only via
    soft_preferences.custom_preferences. Before this fix,
    app/services/qualification_context.py::_soft_preference_terms never
    read that field — the LLM qualification prompt never saw it, even
    though the user's own description correctly named it and it was
    correctly stored on the ICP. This is exactly the kind of silent
    meaning-loss the audit needed to prove before fixing."""
    from app.schemas.canonical_icp import CanonicalCustomRule

    icp = _icp().model_copy(
        update={
            "soft_preferences": CanonicalSoftPreferences(
                business_models=("Subscription",),
                custom_preferences=(CanonicalCustomRule(label="funding", description="eq: 'Recently raised Series A'"),),
            )
        }
    )
    context = _build_context(icp, _company_evidence())
    assert "Subscription" in context.icp_soft_preferences  # existing fields still work
    assert any("funding" in term and "Recently raised Series A" in term for term in context.icp_soft_preferences)
