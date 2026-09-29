"""Phase 9 — tests for the real OpenAIProvider (app/services/llm_providers/
openai_provider.py). All OpenAI HTTP calls are mocked via respx (matching
test_apollo_provider.py / test_explorium_provider.py's convention) — no
real network call, no API key required, no cost.

Scope: this file tests OpenAIProvider in isolation (request/response
mapping, error handling, both context shapes). It does NOT re-test
app/services/llm_qualification.py's own gating/validation logic (hard
gate, evidence-id anti-fabrication, schema validation) — that's already
covered by tests/test_llm_qualification.py and is provider-agnostic by
design; this file only has to prove OpenAIProvider hands qualify_lead the
same well-formed LLMProviderResponse shape any other LLMProvider would.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import respx

from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.llm_qualification import QualificationExecutionStatus
from app.schemas.scoring import ResolutionSignal
from app.services.adversarial_context import build_adversarial_context
from app.services.hard_icp_validation import validate_against_icp
from app.services.lead_scoring import score_lead
from app.services.llm_providers.base import LLMProviderErrorCode
from app.services.llm_providers.mock import MockLLMProvider
from app.services.llm_providers.openai_provider import OpenAIProvider
from app.services.llm_qualification import qualify_lead
from app.services.qualification_context import build_qualification_context

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _provider(base_url: str = "https://api.openai.com/v1") -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key", base_url=base_url, model_id="gpt-5-nano")


def _icp() -> CanonicalICP:
    return CanonicalICP(
        icp_id="icp-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("Skincare",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="US", code="US", label="US"),)),
            employee_range=EmployeeRange(min=10, max=200),
            allowed_titles=(),
            company_types=(),
            exclusions=(),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )


def _evidence(field: str, value, source_provider_id: str = "provider-a") -> EvidenceRecord:
    return EvidenceRecord(
        id=str(uuid4()),
        entity_type=EntityType.COMPANY,
        entity_id="company-1",
        field=field,
        value=value,
        source_provider_id=source_provider_id,
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=NOW,
    )


def _company_evidence() -> list[EvidenceRecord]:
    # Two independent, agreeing sources per field so every field reaches
    # EvidenceStatus.SUPPORTED (compute_field_status requires either 2+
    # corroborating records or a stated MEDIUM/HIGH confidence) — a single
    # UNKNOWN-confidence record only clears the bar for industry/country
    # (the two Explorium-trusted structured fields), so a fixture meant to
    # reach hard-rule PASS needs corroboration on every field, matching
    # tests/test_llm_qualification.py's own _company_evidence() helper.
    fields = {
        "company_identity": "Acme Skincare Inc",
        "industry": "Skincare",
        "employee_count": 50,
        "country": "US",
    }
    records = []
    for field, value in fields.items():
        records.append(_evidence(field, value, source_provider_id="provider-a"))
        records.append(_evidence(field, value, source_provider_id="provider-b"))
    return records


def _qualification_context():
    icp = _icp()
    company_evidence = _company_evidence()
    validation = validate_against_icp(icp, "company-1", company_evidence, None, [])
    score = score_lead(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None,
        commercial_signals=[],
        person_id=None,
        person_evidence=[],
        person_resolutions=None,
        now=NOW,
    )
    return build_qualification_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=None,
        commercial_signals=[],
        score=score,
    )


def _openai_success_body(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


_GOOD_FIT_PAYLOAD = {
    "decision": "GOOD_FIT",
    "confidence": 88,
    "reason_codes": ["STRONG_ICP_ALIGNMENT"],
    "summary": "Evidence-backed alignment with the stated ICP.",
    "supporting_evidence_ids": [],
    "risk_evidence_ids": [],
    "missing_evidence": [],
    "commercial_fit_explanation": "",
    "hard_rule_acknowledgement": "PASS acknowledged.",
    "uncertainties": [],
}


# --- successful qualification call --------------------------------------


@respx.mock
def test_qualify_lead_end_to_end_with_a_real_openai_call_shape():
    context = _qualification_context()
    payload = {**_GOOD_FIT_PAYLOAD, "supporting_evidence_ids": [e.id for e in context.evidence[:1]]}
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(200, json=_openai_success_body(payload)))

    provider = _provider()
    result = qualify_lead(context, provider)

    assert result.status == QualificationExecutionStatus.SUCCESS
    assert result.decision.value == "GOOD_FIT"
    assert result.provider_id == "openai-llm-v1"
    assert result.model_id == "gpt-5-nano"


@respx.mock
def test_request_sends_the_configured_model_and_strict_json_schema():
    context = _qualification_context()
    route = respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(200, json=_openai_success_body(_GOOD_FIT_PAYLOAD)))

    _provider().qualify(context)

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "gpt-5-nano"
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert "GOOD_FIT" in sent["response_format"]["json_schema"]["schema"]["properties"]["decision"]["enum"]
    assert "REJECT" not in sent["response_format"]["json_schema"]["schema"]["properties"]["decision"]["enum"]


@respx.mock
def test_api_key_is_sent_as_bearer_auth_header_never_in_body_or_response():
    context = _qualification_context()
    route = respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(200, json=_openai_success_body(_GOOD_FIT_PAYLOAD)))

    response = _provider().qualify(context)

    assert route.calls[0].request.headers["Authorization"] == "Bearer test-key"
    assert "test-key" not in json.loads(route.calls[0].request.content).__str__()
    assert response.raw_text is not None and "test-key" not in response.raw_text


# --- fabricated evidence is still caught one layer up --------------------


@respx.mock
def test_fabricated_evidence_id_from_openai_is_still_rejected_by_qualify_lead():
    context = _qualification_context()
    payload = {**_GOOD_FIT_PAYLOAD, "supporting_evidence_ids": ["totally-made-up-id"]}
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(200, json=_openai_success_body(payload)))

    result = qualify_lead(context, _provider())

    assert result.status == QualificationExecutionStatus.INVALID_EVIDENCE_IDS
    assert result.decision is None


# --- error mapping ---------------------------------------------------------


@respx.mock
def test_401_maps_to_provider_error_not_success():
    context = _qualification_context()
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}}))

    response = _provider().qualify(context)

    assert response.success is False
    assert response.error.code == LLMProviderErrorCode.PROVIDER_ERROR
    assert response.error.retryable is False


@respx.mock
def test_429_maps_to_rate_limited_and_is_retryable():
    context = _qualification_context()
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "rate limited"}}))

    response = _provider().qualify(context)

    assert response.success is False
    assert response.error.code == LLMProviderErrorCode.RATE_LIMITED
    assert response.error.retryable is True


@respx.mock
def test_500_maps_to_provider_error_and_is_retryable():
    context = _qualification_context()
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(500, text="internal error"))

    response = _provider().qualify(context)

    assert response.success is False
    assert response.error.code == LLMProviderErrorCode.PROVIDER_ERROR
    assert response.error.retryable is True


@respx.mock
def test_timeout_maps_to_timeout_error_code():
    context = _qualification_context()
    respx.post(OPENAI_CHAT_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    response = _provider().qualify(context)

    assert response.success is False
    assert response.error.code == LLMProviderErrorCode.TIMEOUT
    assert response.error.retryable is True


@respx.mock
def test_malformed_response_shape_is_a_clean_provider_error_not_a_crash():
    context = _qualification_context()
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))

    response = _provider().qualify(context)

    assert response.success is False
    assert response.error.code == LLMProviderErrorCode.PROVIDER_ERROR


@respx.mock
def test_empty_content_is_a_success_with_empty_raw_text_not_a_fabricated_response():
    context = _qualification_context()
    body = {"choices": [{"message": {"content": ""}}]}
    respx.post(OPENAI_CHAT_URL).mock(return_value=httpx.Response(200, json=body))

    result = qualify_lead(context, _provider())

    assert result.status == QualificationExecutionStatus.EMPTY_RESPONSE


@respx.mock
def test_network_error_never_propagates_out_of_qualify():
    context = _qualification_context()
    respx.post(OPENAI_CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    # qualify() (base.py) must convert this into a clean failure response,
    # exactly like every other LLMProvider — it must never raise into or
    # crash the caller.
    response = _provider().qualify(context)

    assert response.success is False
    assert response.error is not None
    assert response.error.code == LLMProviderErrorCode.PROVIDER_ERROR


# --- adversarial (Phase 17) call shape ------------------------------------


@respx.mock
def test_adversarial_context_uses_the_adversarial_schema_and_prompt():
    icp = _icp()
    company_evidence = [_evidence("company_identity", "Acme Skincare Inc"), _evidence("industry", "Skincare")]
    validation = validate_against_icp(icp, "company-1", company_evidence, None, [])
    score = score_lead(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None,
        commercial_signals=[],
        person_id=None,
        person_evidence=[],
        person_resolutions=None,
        now=NOW,
    )
    qual_context = build_qualification_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=None,
        commercial_signals=[],
        score=score,
    )
    # The first-pass qualification being challenged is built via the mock
    # provider — OpenAIProvider is the thing under test for the adversarial
    # CALL itself, not for producing the first-pass result it challenges.
    first_pass_result = qualify_lead(
        qual_context,
        MockLLMProvider(
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
        ),
    )
    adversarial_context = build_adversarial_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=None,
        commercial_signals=[],
        score=score,
        qualification_id="qual-1",
        qualification=first_pass_result,
    )

    route = respx.post(OPENAI_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_success_body(
                {
                    "adversarial_result": "SURVIVES",
                    "confidence": 70,
                    "contradictions": [],
                    "risk_codes": [],
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": [],
                    "unsupported_claims": [],
                    "missing_evidence": [],
                    "reasoning_summary": "No contradiction found.",
                    "recommendation": "",
                }
            ),
        )
    )

    response = _provider().qualify(adversarial_context)

    assert response.success is True
    parsed = json.loads(response.raw_text)
    assert parsed["adversarial_result"] == "SURVIVES"
    sent = json.loads(route.calls[0].request.content)
    assert "adversarial_result" in sent["response_format"]["json_schema"]["schema"]["properties"]
    assert "NOT_EXECUTED" not in sent["response_format"]["json_schema"]["schema"]["properties"]["adversarial_result"]["enum"]
