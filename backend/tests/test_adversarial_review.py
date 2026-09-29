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
from app.schemas.adversarial_review import AdversarialExecutionStatus, AdversarialResult, RawAdversarialOutput
from app.schemas.llm_qualification import QualificationDecision, QualificationExecutionStatus
from app.schemas.scoring import ResolutionSignal
from app.services.adversarial_context import build_adversarial_context, build_first_pass_brief
from app.services.adversarial_review import run_adversarial_review
from app.services.hard_icp_validation import validate_against_icp
from app.services.lead_scoring import score_lead
from app.services.llm_providers.mock import MockLLMProvider
from app.services.llm_providers.mock_adversarial import build_survives_response
from app.services.llm_qualification import qualify_lead

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


def _first_pass(icp, company_evidence, business_model=None, commercial_signals=None, qualification_provider=None):
    validation = validate_against_icp(icp, "company-1", company_evidence, None, [])
    score = score_lead(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=business_model,
        commercial_signals=commercial_signals or [],
        person_id=None,
        person_evidence=[],
        person_resolutions=None,
        now=NOW,
    )
    from app.services.qualification_context import build_qualification_context

    qual_context = build_qualification_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals or [],
        score=score,
    )
    provider = qualification_provider or MockLLMProvider(
        response_text=json.dumps(
            {
                "decision": "GOOD_FIT",
                "confidence": 80,
                "reason_codes": [],
                "summary": "Looks like a strong fit.",
                "supporting_evidence_ids": [e.id for e in qual_context.evidence[:1]],
                "risk_evidence_ids": [],
                "missing_evidence": [],
                "commercial_fit_explanation": "",
                "hard_rule_acknowledgement": "PASS acknowledged.",
                "uncertainties": [],
            }
        )
    )
    qualification = qualify_lead(qual_context, provider)
    return validation, score, qualification


def _build_context(icp, company_evidence, business_model=None, commercial_signals=None):
    validation, score, qualification = _first_pass(icp, company_evidence, business_model, commercial_signals)
    return build_adversarial_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals or [],
        score=score,
        qualification_id="qual-1",
        qualification=qualification,
    )


def _response(result, **overrides):
    body = {
        "adversarial_result": result,
        "confidence": 65,
        "contradictions": [],
        "risk_codes": [],
        "supporting_evidence_ids": [],
        "contradicting_evidence_ids": [],
        "unsupported_claims": [],
        "missing_evidence": [],
        "reasoning_summary": "test reasoning",
        "recommendation": "",
    }
    body.update(overrides)
    return json.dumps(body)


# --- hard gate authority --------------------------------------------------


def test_hard_fail_remains_fail_and_adversarial_never_invoked():
    icp = _icp(min_employees=1000)  # 50 employees -> FAIL
    context = _build_context(icp, _company_evidence())
    assert context.hard_rule_result == OverallResult.FAIL

    class ExplodingProvider(MockLLMProvider):
        def _call(self, ctx):
            raise AssertionError("adversarial LLM must never be called for a hard FAIL")

    result = run_adversarial_review(context, ExplodingProvider())
    assert result.status == AdversarialExecutionStatus.HARD_BLOCKED
    assert result.adversarial_result == AdversarialResult.NOT_EXECUTED


def test_hard_hold_remains_hold_and_adversarial_never_invoked():
    icp = _icp(min_employees=1000)
    context = _build_context(icp, _company_evidence(employee_count=None))  # no employee evidence -> HOLD
    assert context.hard_rule_result == OverallResult.HOLD

    class ExplodingProvider(MockLLMProvider):
        def _call(self, ctx):
            raise AssertionError("adversarial LLM must never be called for a hard HOLD")

    result = run_adversarial_review(context, ExplodingProvider())
    assert result.status == AdversarialExecutionStatus.HARD_BLOCKED
    assert result.adversarial_result == AdversarialResult.NOT_EXECUTED


def test_hard_pass_reaches_the_adversarial_llm():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    assert context.hard_rule_result == OverallResult.PASS
    provider = MockLLMProvider(response_text=build_survives_response(context))
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.SUCCESS


# --- decision variety -------------------------------------------------


def test_strong_lead_survives():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=build_survives_response(context))
    result = run_adversarial_review(context, provider)
    assert result.adversarial_result == AdversarialResult.SURVIVES


def test_genuine_contradiction_is_disproved():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    evidence_id = context.base.evidence[0].id
    provider = MockLLMProvider(
        response_text=_response(
            "DISPROVED",
            contradictions=["Evidence shows this is a wholesale distributor, not DTC as first-pass claimed."],
            contradicting_evidence_ids=[evidence_id],
        )
    )
    result = run_adversarial_review(context, provider)
    assert result.adversarial_result == AdversarialResult.DISPROVED
    assert evidence_id in result.contradicting_evidence_ids


def test_meaningful_risk_is_weakened():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(
        response_text=_response("WEAKENED", risk_codes=["SINGLE_WEAK_SOURCE"], contradictions=["Business model relies on a single uncorroborated source."])
    )
    result = run_adversarial_review(context, provider)
    assert result.adversarial_result == AdversarialResult.WEAKENED


def test_unresolved_evidence_is_hold():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("HOLD", missing_evidence=["business_model"]))
    result = run_adversarial_review(context, provider)
    assert result.adversarial_result == AdversarialResult.HOLD


def test_missing_evidence_alone_does_not_become_disproved():
    body = {
        "adversarial_result": "DISPROVED",
        "confidence": 60,
        "contradictions": [],
        "risk_codes": [],
        "supporting_evidence_ids": [],
        "contradicting_evidence_ids": [],
        "unsupported_claims": [],
        "missing_evidence": ["business_model"],
        "reasoning_summary": "evidence is simply missing",
        "recommendation": "",
    }
    with pytest.raises(Exception):
        RawAdversarialOutput.model_validate(body)  # DISPROVED requires a contradiction, not just missing evidence


# --- first-pass unsupported claim detection --------------------------------


def test_first_pass_unsupported_claim_is_recorded():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(
        response_text=_response(
            "WEAKENED",
            unsupported_claims=["First pass claimed DTC business model without citing any supporting evidence id."],
        )
    )
    result = run_adversarial_review(context, provider)
    assert result.unsupported_claims


# --- anti-hallucination -----------------------------------------------


def test_fabricated_supporting_evidence_id_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("SURVIVES", supporting_evidence_ids=["fabricated-id"]))
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.INVALID_EVIDENCE_IDS


def test_fabricated_contradicting_evidence_id_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("DISPROVED", contradictions=["x"], contradicting_evidence_ids=["fabricated-id"]))
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.INVALID_EVIDENCE_IDS


def test_real_evidence_ids_are_accepted():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    real_id = context.base.evidence[0].id
    provider = MockLLMProvider(response_text=_response("SURVIVES", supporting_evidence_ids=[real_id]))
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.SUCCESS
    assert real_id in result.supporting_evidence_ids


def test_malformed_json_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text="not json {{{")
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.MALFORMED_OUTPUT


def test_schema_violation_is_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=json.dumps({"adversarial_result": "SURVIVES"}))  # missing required fields
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.SCHEMA_INVALID


def test_not_executed_from_llm_is_rejected_by_schema():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("NOT_EXECUTED"))
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.SCHEMA_INVALID


# --- provider failure handling ------------------------------------------


def test_provider_failure_never_becomes_survives():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(raise_provider_error=True)
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.PROVIDER_ERROR
    assert result.adversarial_result is None


def test_provider_timeout_preserved():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(raise_timeout=True)
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.PROVIDER_TIMEOUT
    assert result.adversarial_result is None


def test_empty_response_is_not_survives():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(return_empty=True)
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.EMPTY_RESPONSE
    assert result.adversarial_result is None


# --- strict schema validation ------------------------------------------


def test_raw_adversarial_output_rejects_unknown_fields():
    with pytest.raises(Exception):
        RawAdversarialOutput.model_validate(
            {
                "adversarial_result": "SURVIVES",
                "confidence": 70,
                "reasoning_summary": "x",
                "unexpected_field": "nope",
            }
        )


def test_confidence_out_of_bounds_rejected():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    provider = MockLLMProvider(response_text=_response("SURVIVES", confidence=999))
    result = run_adversarial_review(context, provider)
    assert result.status == AdversarialExecutionStatus.SCHEMA_INVALID


# --- underlying data unchanged -------------------------------------------


def test_underlying_evidence_and_first_pass_are_not_mutated():
    icp = _icp()
    validation, score, qualification = _first_pass(icp, _company_evidence())
    context = build_adversarial_context(
        icp=icp, company_id="company-1", company_evidence=_company_evidence(),
        hard_rule_evaluation=validation.evaluation, business_model=None, commercial_signals=[],
        score=score, qualification_id="qual-1", qualification=qualification,
    )
    before = qualification.model_dump()
    provider = MockLLMProvider(response_text=build_survives_response(context))
    run_adversarial_review(context, provider)
    after = qualification.model_dump()
    assert before == after  # the original LLMQualificationResult object is frozen and untouched


def test_context_is_immutable():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    with pytest.raises(Exception):
        context.first_pass = None


# --- multi-ICP isolation ---------------------------------------------------


def test_multi_icp_isolation():
    icp_v1 = _icp(icp_id="icp-x", version=1, business_models=("DTC",))
    icp_v2 = _icp(icp_id="icp-x", version=2, business_models=("B2B",))
    context_v1 = _build_context(icp_v1, _company_evidence())
    context_v2 = _build_context(icp_v2, _company_evidence())
    assert context_v1.icp_version == 1
    assert context_v2.icp_version == 2
    assert context_v1.base.icp_soft_preferences != context_v2.base.icp_soft_preferences


# --- deterministic mock provider --------------------------------------------


def test_mock_provider_is_deterministic():
    icp = _icp()
    context = _build_context(icp, _company_evidence())
    response_text = build_survives_response(context)
    provider = MockLLMProvider(response_text=response_text)
    first = run_adversarial_review(context, provider)
    second = run_adversarial_review(context, provider)
    assert first == second


# --- no manager feedback / hard-rule duplication --------------------------


def test_no_manager_feedback_or_hard_rule_duplication_in_adversarial_modules():
    import inspect

    import app.services.adversarial_context as ctx_module
    import app.services.adversarial_review as review_module

    for module in (ctx_module, review_module):
        source = inspect.getsource(module)
        assert "manager_feedback" not in source
        assert "ManagerFeedback" not in source
        assert "evaluate_hard_rules" not in source
