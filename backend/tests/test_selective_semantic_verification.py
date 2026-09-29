"""Phase 12 — selective semantic verification tests.

Improves company-discovery precision by sending ONLY keyword-fallback
candidates (the population most at risk of being an "agency that merely
mentions the ICP's industry" false positive) through the existing Phase 16
LLM qualification interface — reusing OpenAIProvider/LLMProvider exactly as
built in Phase 9, no second AI architecture. Structured linkedin_category/
naics_category matches are trusted without the extra call
(QualificationExecutionStatus.STRUCTURED_MATCH_TRUSTED,
decision=GOOD_FIT — backend-assigned, never an LLM output, mirroring how
HARD_REJECTED's REJECT decision already works).

Runs the REAL chain end-to-end wherever practical: ExploriumCompanyDiscoveryProvider
(mocked HTTP via respx) -> the same evidence field-mapping evidence_import.py
uses -> validate_against_icp -> build_qualification_context -> qualify_lead.
This proves the gate is driven by real, evidenced Phase 11 provenance, not a
hand-constructed shortcut.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import QualificationDecision, QualificationExecutionStatus
from app.services.evidence_import import _industry_match_provenance
from app.services.hard_icp_validation import validate_against_icp
from app.services.lead_scoring import score_lead
from app.services.llm_providers.base import LLMProviderErrorCode
from app.services.llm_providers.mock import MockLLMProvider
from app.services.llm_qualification import qualify_lead
from app.services.qualification_context import _discovery_match_type, build_qualification_context
from app.schemas.scoring import ResolutionSignal

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _icp(icp_id="icp-1", **hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("D2C skincare",),
        geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
        employee_range=EmployeeRange(min=10, max=200),
        allowed_titles=(),
        company_types=(),
        exclusions=(),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id=icp_id, version=1, hard_rules=CanonicalHardRules(**hard_defaults), soft_preferences=CanonicalSoftPreferences()
    )


def _evidence(company_id: str, field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=EntityType.COMPANY,
        entity_id=company_id,
        field=field,
        value=value,
        source_provider_id="explorium-company-discovery-v1",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _company_evidence_from_attributes(company_id: str, attributes: dict) -> list[EvidenceRecord]:
    """Mirrors evidence_import.py's real field mapping, including the
    Phase 11 evidence_text provenance carry-through — the exact same
    helper style test_discovery_qualification_bridge.py already uses."""
    records = [_evidence(company_id, "company_identity", attributes.get("name", "Some Company"))]
    if attributes.get("domain"):
        records.append(_evidence(company_id, "domain", attributes["domain"]))
    if attributes.get("industry"):
        records.append(_evidence(company_id, "industry", attributes["industry"], evidence_text=_industry_match_provenance(attributes)))
    if attributes.get("country"):
        records.append(_evidence(company_id, "country", attributes["country"]))
    if attributes.get("employee_range"):
        records.append(_evidence(company_id, "employee_range", attributes["employee_range"]))
    return records


def _discover_one(icp: CanonicalICP, autocomplete_mocks: list[dict], search_response: dict):
    with respx.mock:
        for mock in autocomplete_mocks:
            respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": mock["field"], "query": mock["query"]}).mock(
                return_value=httpx.Response(200, json=mock["response"])
            )
        respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
        respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json=search_response))

        provider = ExploriumCompanyDiscoveryProvider(api_key="test-key")
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_DISCOVERY,
            query={
                "industries": icp.hard_rules.industries,
                "geography_codes": tuple(e.code for e in icp.hard_rules.geography.countries),
                "min_employees": icp.hard_rules.employee_range.min,
                "max_employees": icp.hard_rules.employee_range.max,
                "limit": 10,
            },
        )
        response = provider.run(request)
    assert response.success is True
    assert len(response.data) == 1
    return response.data[0]


def _build_context(icp: CanonicalICP, company_evidence: list[EvidenceRecord]):
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
    context = build_qualification_context(
        icp=icp,
        company_id="company-1",
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=None,
        commercial_signals=[],
        score=score,
    )
    return validation, context


def _good_fit_response(context, decision="GOOD_FIT", **overrides) -> str:
    payload = {
        "decision": decision,
        "confidence": 82,
        "reason_codes": [],
        "summary": "Site content confirms the company sells its own D2C skincare products.",
        "supporting_evidence_ids": [e.id for e in context.evidence[:1]],
        "risk_evidence_ids": [],
        "missing_evidence": [],
        "commercial_fit_explanation": "",
        "hard_rule_acknowledgement": f"Hard ICP result is {context.hard_rule_result.value}; treated as authoritative.",
        "uncertainties": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- 1. keyword-fallback candidate -> AI verification IS invoked ----------


def test_keyword_fallback_candidate_reaches_the_llm():
    # The ICP's own industry term is set to exact-match what the mocked
    # search response returns (so the plain, unbridged hard-rule industry
    # check PASSes on its own — this test isn't about the Phase 11
    # bridge), while the autocomplete lookup for that term returns no
    # structured match at all, so discovery genuinely falls to the
    # keyword branch. This is what makes it a real keyword-fallback
    # candidate: PASS on industry, but with no structured provenance tag.
    icp = _icp(industries=("Cosmetics, Beauty Supplies, and Perfume Stores",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {"field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "kw0000000000000000000000000001",
                "name": "Glow & Co Skincare",
                "domain": "glowandco.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert "industry_match_branch" not in record.attributes  # confirms this really is keyword fallback

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)
    assert validation.evaluation.overall_result == OverallResult.PASS
    assert context.discovery_match_type == "keyword_fallback"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is True
    assert result.status == QualificationExecutionStatus.SUCCESS


# --- 2 & 3. structured candidate -> AI verification is NOT invoked --------


def test_structured_linkedin_candidate_with_unverifiable_resolution_holds_and_skips_ai():
    """A structured linkedin_category branch resolves for "Healthcare", but
    Explorium's /businesses response reports naics_description "General
    Medical and Surgical Hospitals" — a different label with no provider
    data proving it's a member of "Healthcare". Per the structured-bridge
    retirement, this can never be trusted as a PASS: it correctly HOLDs.
    The LLM is still never called — HOLD is a deterministic hard-rule
    outcome, not something semantic verification adjudicates."""
    icp = _icp(industries=("Healthcare",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "hospital-health-care"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "st0000000000000000000000000001",
                "name": "Midwest Regional Health Group",
                "domain": "midwestregionalhealth.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "General Medical and Surgical Hospitals",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry_match_branch"] == "linkedin_category"

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)
    assert validation.evaluation.overall_result == OverallResult.HOLD
    assert context.discovery_match_type == "structured"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_HOLD


def test_structured_naics_candidate_with_unverifiable_resolution_also_holds_and_skips_ai():
    """Same invariant as the linkedin_category case above, for the
    naics_category branch: the resolved NAICS code's own returned
    naics_description text ("All Other Miscellaneous General Purpose
    Machinery Manufacturing") never exact-matches the requested ICP term
    ("industrial automation"), and no provider data proves the
    relationship — so it correctly HOLDs rather than being trusted."""
    icp = _icp(industries=("industrial automation",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "industrial automation", "response": []},
        {"field": "naics_category", "query": "industrial automation", "response": [{"query": "industrial automation", "label": "industrial automation", "value": "333999"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "st0000000000000000000000000002",
                "name": "Precision Robotics Corp",
                "domain": "precisionrobotics.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "All Other Miscellaneous General Purpose Machinery Manufacturing",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry_match_branch"] == "naics_category"

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)
    assert validation.evaluation.overall_result == OverallResult.HOLD
    assert context.discovery_match_type == "structured"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_HOLD


def test_structured_candidate_with_exact_matching_resolution_still_skips_ai_verification():
    """Positive control: when the structured branch's own reported
    naics_description text DOES exact-match the requested ICP term, the
    hard rule legitimately PASSes on real evidence (no bridging or
    invented relationship needed), and the candidate is still trusted
    without the extra LLM call — proving Phase 12's skip-the-LLM gate
    itself is unchanged by the structured-bridge retirement."""
    icp = _icp(industries=("General Medical and Surgical Hospitals",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "General Medical and Surgical Hospitals", "response": [{"query": "General Medical and Surgical Hospitals", "label": "General Medical and Surgical Hospitals", "value": "hospital-health-care"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "st0000000000000000000000000003",
                "name": "Midwest Regional Health Group",
                "domain": "midwestregionalhealth.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "General Medical and Surgical Hospitals",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry_match_branch"] == "linkedin_category"

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)
    assert validation.evaluation.overall_result == OverallResult.PASS
    assert context.discovery_match_type == "structured"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.STRUCTURED_MATCH_TRUSTED
    assert result.decision == QualificationDecision.GOOD_FIT


# --- 4. agency/staffing false-positive -> rejected/held appropriately -----


def test_keyword_fallback_agency_false_positive_is_rejected_by_the_ai():
    icp = _icp(industries=("Consumer Goods",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Consumer Goods", "response": []},
        {"field": "naics_category", "query": "Consumer Goods", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "fp0000000000000000000000000001",
                "name": "Beauty Brand Growth Partners",
                "domain": "beautybrandgrowth.invalid",
                "country_name": "United States",
                "number_of_employees_range": "11-50",
                "naics_description": "Consumer Goods",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)
    assert context.discovery_match_type == "keyword_fallback"

    not_fit_response = json.dumps(
        {
            "decision": "NOT_FIT",
            "confidence": 88,
            "reason_codes": ["WRONG_BUSINESS_MODEL"],
            "summary": "Evidence shows a growth/marketing agency serving beauty brands, not a D2C skincare brand itself.",
            "supporting_evidence_ids": [e.id for e in context.evidence if e.field == "company_identity"],
            "risk_evidence_ids": [e.id for e in context.evidence if e.field == "industry"],
            "missing_evidence": [],
            "commercial_fit_explanation": "",
            "hard_rule_acknowledgement": "PASS acknowledged; industry taxonomy label alone does not confirm the business model.",
            "uncertainties": [],
        }
    )
    result = qualify_lead(context, MockLLMProvider(response_text=not_fit_response))

    assert result.status == QualificationExecutionStatus.SUCCESS
    assert result.decision == QualificationDecision.NOT_FIT


def test_keyword_fallback_staffing_false_positive_can_be_held_pending_more_evidence():
    icp = _icp(industries=("Information Technology and Services",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Information Technology and Services", "response": []},
        {"field": "naics_category", "query": "Information Technology and Services", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "fp0000000000000000000000000002",
                "name": "Apex IT Staffing Solutions",
                "domain": "apexitstaffing.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Information Technology and Services",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)

    hold_response = json.dumps(
        {
            "decision": "HOLD",
            "confidence": 40,
            "reason_codes": [],
            "summary": "Company name suggests IT staffing, not a SaaS product company, but the available evidence does not confirm this directly.",
            "supporting_evidence_ids": [],
            "risk_evidence_ids": [e.id for e in context.evidence if e.field == "company_identity"],
            "missing_evidence": ["business_description"],
            "commercial_fit_explanation": "",
            "hard_rule_acknowledgement": "PASS acknowledged.",
            "uncertainties": ["Cannot confirm from supplied evidence alone whether this is a SaaS product company or a staffing agency."],
        }
    )
    result = qualify_lead(context, MockLLMProvider(response_text=hold_response))

    assert result.status == QualificationExecutionStatus.SUCCESS
    assert result.decision == QualificationDecision.HOLD


# --- 5. genuine keyword candidate -> accepted appropriately ----------------


def test_genuine_keyword_fallback_candidate_is_accepted():
    icp = _icp(industries=("Cosmetics, Beauty Supplies, and Perfume Stores",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {"field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "gk0000000000000000000000000001",
                "name": "Radiance Naturals",
                "domain": "radiancenaturals.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)

    result = qualify_lead(context, MockLLMProvider(response_text=_good_fit_response(context)))

    assert result.status == QualificationExecutionStatus.SUCCESS
    assert result.decision == QualificationDecision.GOOD_FIT


# --- 6. missing evidence -> no hallucinated PASS ---------------------------


def test_missing_evidence_produces_hold_not_a_hallucinated_pass():
    icp = _icp(industries=("Consumer Goods",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Consumer Goods", "response": []},
        {"field": "naics_category", "query": "Consumer Goods", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "me0000000000000000000000000001",
                "name": "Unclear Co",
                "domain": "unclearco.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Consumer Goods",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)

    hold_response = json.dumps(
        {
            "decision": "HOLD",
            "confidence": 30,
            "reason_codes": [],
            "summary": "Insufficient evidence to determine whether this company sells its own products or serves other brands.",
            "supporting_evidence_ids": [],
            "risk_evidence_ids": [],
            "missing_evidence": ["business_description", "product_or_service_offering"],
            "commercial_fit_explanation": "",
            "hard_rule_acknowledgement": "PASS acknowledged.",
            "uncertainties": ["No site content or description evidence was supplied."],
        }
    )
    result = qualify_lead(context, MockLLMProvider(response_text=hold_response))

    assert result.status == QualificationExecutionStatus.SUCCESS
    assert result.decision == QualificationDecision.HOLD
    assert result.decision != QualificationDecision.GOOD_FIT


def test_ai_fabricated_evidence_id_is_still_rejected_by_the_existing_anti_hallucination_check():
    icp = _icp(industries=("Cosmetics, Beauty Supplies, and Perfume Stores",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {"field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "fab0000000000000000000000000001",
                "name": "Glow & Co Skincare",
                "domain": "glowandco.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)

    fabricated_response = _good_fit_response(context, supporting_evidence_ids=["totally-made-up-id"])
    result = qualify_lead(context, MockLLMProvider(response_text=fabricated_response))

    assert result.status == QualificationExecutionStatus.INVALID_EVIDENCE_IDS
    assert result.decision is None


# --- 7. OpenAI failure/timeout/malformed -> graceful degradation ----------


def test_provider_error_on_keyword_fallback_candidate_never_crashes():
    icp = _icp(industries=("Cosmetics, Beauty Supplies, and Perfume Stores",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {"field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "err0000000000000000000000000001",
                "name": "Glow & Co Skincare",
                "domain": "glowandco.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    validation, context = _build_context(icp, evidence)

    result = qualify_lead(context, MockLLMProvider(raise_provider_error=True))

    assert result.status == QualificationExecutionStatus.PROVIDER_ERROR
    assert result.decision is None


def test_provider_timeout_on_keyword_fallback_candidate_degrades_cleanly():
    icp = _icp()
    validation, context = _build_context(
        icp, [_evidence("company-1", "industry", "D2C skincare"), _evidence("company-1", "country", "United States"), _evidence("company-1", "employee_range", "51-200")]
    )
    result = qualify_lead(context, MockLLMProvider(raise_timeout=True))

    assert result.status == QualificationExecutionStatus.PROVIDER_TIMEOUT
    assert result.decision is None


def test_malformed_json_from_provider_degrades_cleanly_never_a_false_pass():
    icp = _icp()
    validation, context = _build_context(
        icp, [_evidence("company-1", "industry", "D2C skincare"), _evidence("company-1", "country", "United States"), _evidence("company-1", "employee_range", "51-200")]
    )
    result = qualify_lead(context, MockLLMProvider(response_text="not json at all {{{"))

    assert result.status == QualificationExecutionStatus.MALFORMED_OUTPUT
    assert result.decision is None


def test_empty_response_from_provider_degrades_cleanly():
    icp = _icp()
    validation, context = _build_context(
        icp, [_evidence("company-1", "industry", "D2C skincare"), _evidence("company-1", "country", "United States"), _evidence("company-1", "employee_range", "51-200")]
    )
    result = qualify_lead(context, MockLLMProvider(return_empty=True))

    assert result.status == QualificationExecutionStatus.EMPTY_RESPONSE
    assert result.decision is None


# --- 8. existing discovery + hard-rule regression guards -------------------


def test_hard_fail_still_never_reaches_the_llm_regardless_of_discovery_match_type():
    icp = _icp(employee_range=EmployeeRange(min=1000, max=5000))  # candidate will be far too small -> FAIL
    validation, context = _build_context(
        icp, [_evidence("company-1", "industry", "Consumer Goods"), _evidence("company-1", "country", "United States"), _evidence("company-1", "employee_range", "11-50")]
    )
    assert context.hard_rule_result == OverallResult.FAIL

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_REJECTED


def test_hard_hold_still_never_reaches_the_llm_regardless_of_discovery_match_type():
    icp = _icp()
    # Industry evidence exact-matches the ICP's own term (PASS on its
    # own), but no employee_range/employee_count evidence at all is
    # supplied -> the employee_range rule HOLDs, which is what this test
    # is actually about.
    validation, context = _build_context(icp, [_evidence("company-1", "industry", "D2C skincare"), _evidence("company-1", "country", "United States")])
    assert context.hard_rule_result == OverallResult.HOLD

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_HOLD


def test_discovery_match_type_defaults_to_unknown_and_still_calls_the_llm_for_pre_phase_12_contexts():
    # A QualificationContext built without discovery_match_type at all
    # (e.g. constructed directly, the way older tests do) must default to
    # "unknown" and proceed to the LLM exactly like a keyword_fallback
    # candidate would — never silently skipped.
    from app.schemas.llm_qualification import QualificationContext

    context = QualificationContext(
        icp_id="icp-1", icp_version=1, icp_industries=("Skincare",), icp_geography=("US",),
        icp_employee_range="10-200", icp_allowed_titles=(), icp_company_types=(), icp_exclusions=(),
        icp_soft_preferences=(), company_id="company-1", person_id=None, hard_rule_result=OverallResult.PASS,
        rule_results=(), reason_codes=(), business_model_summary=None, commercial_signal_summary=(),
        icp_score=80.0, commercial_score=50.0, evidence_score=70.0, freshness_score=None,
        identity_confidence=90.0, final_score=75.0, evidence=(), conflicting_fields=(), missing_critical_fields=(),
    )
    assert context.discovery_match_type == "unknown"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))
    assert call_tracker["called"] is True
    assert result.status == QualificationExecutionStatus.SUCCESS


def test_discovery_match_type_classification_itself():
    # Direct unit coverage of the classifier, independent of the full chain.
    assert _discovery_match_type([]) == "unknown"
    assert _discovery_match_type([_evidence("c1", "industry", "Consumer Goods")]) == "keyword_fallback"
    tagged = _evidence(
        "c1", "industry", "General Medical and Surgical Hospitals",
        evidence_text='{"industry_match": {"taxonomy_field": "linkedin_category", "icp_terms": ["Healthcare"]}}',
    )
    assert _discovery_match_type([tagged]) == "structured"
    # Malformed evidence_text on the only industry record -> not structured,
    # correctly falls back to the safer keyword_fallback classification
    # (never crashes, never silently treated as trusted).
    malformed = _evidence("c1", "industry", "Consumer Goods", evidence_text="not json {{{")
    assert _discovery_match_type([malformed]) == "keyword_fallback"


# --- 9. multiple candidates -> bounded AI calls -----------------------------


def test_ai_is_called_only_for_the_keyword_fallback_subset_across_multiple_candidates():
    icp = _icp(industries=("Healthcare",))
    # Candidate A: structured match (skips AI)
    autocomplete_mocks_a = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "hospital-health-care"}]},
    ]
    search_response_a = {
        "data": [
            {
                "business_id": "multi0000000000000000000000001",
                "name": "Midwest Regional Health Group",
                "domain": "midwestregionalhealth.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "General Medical and Surgical Hospitals",
            }
        ]
    }
    record_a = _discover_one(icp, autocomplete_mocks_a, search_response_a)

    # Candidate B: keyword fallback (needs AI). Industry evidence exact-
    # matches the ICP's own term (so the hard-rule PASSes, reaching
    # qualify_lead's provider call), while carrying no structured-branch
    # provenance tag (confirmed below) — exactly a real keyword-fallback
    # candidate: found via substring search, industry rule PASSes on a
    # literal match, but nothing has verified it's genuinely in-industry.
    icp_b = _icp(industries=("Wellness Staffing Co",))
    autocomplete_mocks_b = [
        {"field": "linkedin_category", "query": "Wellness Staffing Co", "response": []},
        {"field": "naics_category", "query": "Wellness Staffing Co", "response": []},
    ]
    search_response_b = {
        "data": [
            {
                "business_id": "multi0000000000000000000000002",
                "name": "Wellness Staffing Co",
                "domain": "wellnessstaffing.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Wellness Staffing Co",
            }
        ]
    }
    record_b = _discover_one(icp_b, autocomplete_mocks_b, search_response_b)
    assert "industry_match_branch" not in record_b.attributes

    call_count = {"n": 0}

    class _CountingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_count["n"] += 1
            return super()._call(ctx)

    provider = _CountingProvider(response_text="")  # response body is per-context below, set fresh each call

    evidence_a = _company_evidence_from_attributes("company-a", record_a.attributes)
    _, context_a = _build_context(icp, evidence_a)
    provider._response_text = _good_fit_response(context_a)
    qualify_lead(context_a, provider)

    evidence_b = _company_evidence_from_attributes("company-b", record_b.attributes)
    _, context_b = _build_context(icp_b, evidence_b)
    provider._response_text = _good_fit_response(context_b)
    qualify_lead(context_b, provider)

    # Only ONE of the two candidates (the keyword-fallback one) actually
    # reached the provider — the structured match was trusted without it.
    assert call_count["n"] == 1
