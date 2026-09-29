"""Phase 15 — Broad Structured Match Verification (observability only).

Phase 14's audit found that broad structured Explorium categories can be
almost as imprecise as keyword fallback, but a Phase 15 audit (this file's
own companion investigation, documented in app/providers/explorium.py's
own Phase 15 comments) found NO reliable, deterministic way to classify a
structured match as "broad" vs "strong" using only currently-available
data:

  - Explorium's autocomplete response ({query, label, value}) carries no
    confidence/breadth signal at all.
  - Explorium's search response DOES carry a real, previously-uncaptured
    `total_results` field — but it reflects the WHOLE branch's filtered
    request (however many resolved categories were OR'd together, plus
    geography/employee-size), not any single category in isolation.
  - In Phase 14's own 8 realistic scenarios, MOST structured branches
    resolved 2-3 categories into one OR'd request (the confirmed common
    case) — meaning total_results is attributable to a single category
    only in the minority, single-term case.

Per the task's own explicit fallback instruction, this phase therefore
implements ONLY the smallest deterministic observability improvement:

  - app/providers/explorium.py: captures industry_match_resolved_category_count
    and industry_match_scope ("single_category" | "multi_category") on every
    structured-match record, plus industry_match_branch_total_results ONLY
    when scope == "single_category" (the one case it's honestly attributable).
  - app/services/evidence_import.py / qualification_context.py: carry both
    through to QualificationContext.discovery_structured_match_scope /
    discovery_structured_match_total_results, surfaced to Phase 12's prompt.
  - app/services/llm_qualification.py::qualify_lead's gate condition
    (PASS + discovery_match_type == "structured" -> trusted, no LLM call)
    is COMPLETELY UNCHANGED — this data exists for a future phase with a
    principled threshold to use, not wired into any decision here.

Follows test_explorium_provider.py's respx convention and
test_selective_semantic_verification.py's evidence/qualification-context
convention.
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
from app.services.qualification_context import (
    _discovery_structured_match_scope,
    _discovery_structured_match_total_results,
    build_qualification_context,
)
from app.schemas.scoring import ResolutionSignal

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _provider(api_key: str = "test-key") -> ExploriumCompanyDiscoveryProvider:
    return ExploriumCompanyDiscoveryProvider(api_key=api_key)


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query=query)


def _icp(**hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("Healthcare",),
        geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
        employee_range=EmployeeRange(min=10, max=200),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id="icp-1", version=1, hard_rules=CanonicalHardRules(**hard_defaults), soft_preferences=CanonicalSoftPreferences()
    )


def _evidence(field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field=field, value=value,
        source_provider_id="explorium-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _company_evidence_from_attributes(company_id: str, attributes: dict) -> list[EvidenceRecord]:
    records = [_evidence("company_identity", attributes.get("name", "Some Company"))]
    if attributes.get("domain"):
        records.append(_evidence("domain", attributes["domain"]))
    if attributes.get("industry"):
        records.append(_evidence("industry", attributes["industry"], evidence_text=_industry_match_provenance(attributes)))
    if attributes.get("country"):
        records.append(_evidence("country", attributes["country"]))
    if attributes.get("employee_range"):
        records.append(_evidence("employee_range", attributes["employee_range"]))
    return records


def _build_context(icp: CanonicalICP, company_evidence: list[EvidenceRecord]):
    validation = validate_against_icp(icp, "company-1", company_evidence, None, [])
    score = score_lead(
        icp=icp, company_id="company-1", company_evidence=company_evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    context = build_qualification_context(
        icp=icp, company_id="company-1", company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation, business_model=None,
        commercial_signals=[], score=score,
    )
    return validation, context


def _good_fit_response(context) -> str:
    return json.dumps(
        {
            "decision": "GOOD_FIT", "confidence": 82, "reason_codes": [], "summary": "ok",
            "supporting_evidence_ids": [e.id for e in context.evidence[:1]], "risk_evidence_ids": [],
            "missing_evidence": [], "commercial_fit_explanation": "",
            "hard_rule_acknowledgement": f"Hard ICP result is {context.hard_rule_result.value}; treated as authoritative.",
            "uncertainties": [],
        }
    )


# ============================================================================
# Provider-level: single_category vs multi_category attribution
# ============================================================================


@respx.mock
def test_single_resolved_category_gets_total_results_attached():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"business_id": "h1", "name": "Health Co", "naics_description": "General Medical"}], "total_results": 4200, "page": None},
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))

    record = response.data[0]
    assert record.attributes["industry_match_resolved_category_count"] == 1
    assert record.attributes["industry_match_scope"] == "single_category"
    assert record.attributes["industry_match_branch_total_results"] == 4200


@respx.mock
def test_multi_resolved_category_never_gets_total_results_attached():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Medical Practices"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Medical Practices", "label": "Medical Practices", "value": "medical-practice"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"business_id": "h2", "name": "Health Co 2", "naics_description": "General Medical"}], "total_results": 9800, "page": None},
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare", "Medical Practices"]))

    record = response.data[0]
    assert record.attributes["industry_match_resolved_category_count"] == 2
    assert record.attributes["industry_match_scope"] == "multi_category"
    assert "industry_match_branch_total_results" not in record.attributes  # never fabricated/misattributed


@respx.mock
def test_naics_branch_single_category_also_gets_attribution():
    """Generic across taxonomies — not linkedin_category-specific."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Widgetology", "label": "Widgetology", "value": "999999"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "n1", "name": "Widget Co", "naics_description": "Widgetology"}], "total_results": 300, "page": None}
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Widgetology"]))

    record = response.data[0]
    assert record.attributes["industry_match_branch"] == "naics_category"
    assert record.attributes["industry_match_scope"] == "single_category"
    assert record.attributes["industry_match_branch_total_results"] == 300


@respx.mock
def test_missing_total_results_in_response_never_fabricates_a_value():
    """Explorium's response simply omitting total_results (as most existing
    test fixtures do) must never produce a fabricated number."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "h3", "name": "Health Co 3", "naics_description": "General Medical"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))

    record = response.data[0]
    assert record.attributes["industry_match_scope"] == "single_category"
    assert "industry_match_branch_total_results" not in record.attributes


@respx.mock
def test_internal_branch_total_results_field_never_leaks_as_public_attribute():
    """The internal _branch_total_results signal (set unconditionally by
    _run_one_branch, branch-agnostic) must never appear in the final
    public attributes dict, on ANY branch — structured, naics, or keyword."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "k1", "name": "Some Co", "naics_description": "X"}], "total_results": 5000, "page": None}
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C skincare"]))  # falls to keyword

    record = response.data[0]
    assert "_branch_total_results" not in record.attributes
    assert "industry_match_branch_total_results" not in record.attributes  # keyword match never gets this either


@respx.mock
def test_keyword_less_request_shape_unaffected_by_phase_15():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "nl1", "name": "Some Co"}], "total_results": 100, "page": None})
    )
    provider = _provider()
    response = provider.run(_request())

    record = response.data[0]
    assert "_branch_total_results" not in record.attributes
    assert "industry_match_scope" not in record.attributes


# ============================================================================
# Full chain: evidence -> QualificationContext
# ============================================================================


@respx.mock
def test_single_category_scope_and_total_results_reach_qualification_context():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"business_id": "h4", "name": "Health Co 4", "naics_description": "General Medical", "country_name": "United States", "number_of_employees_range": "51-200"}],
                "total_results": 777, "page": None,
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))
    record = response.data[0]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    icp = _icp(industries=("Healthcare",))
    validation, context = _build_context(icp, evidence)

    assert context.discovery_structured_match_scope == "single_category"
    assert context.discovery_structured_match_total_results == 777


@respx.mock
def test_multi_category_scope_reaches_context_with_no_total_results():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Medical Practices"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Medical Practices", "label": "Medical Practices", "value": "medical-practice"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"business_id": "h5", "name": "Health Co 5", "naics_description": "General Medical", "country_name": "United States", "number_of_employees_range": "51-200"}],
                "total_results": 5000, "page": None,
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare", "Medical Practices"]))
    record = response.data[0]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    icp = _icp(industries=("Healthcare", "Medical Practices"))
    validation, context = _build_context(icp, evidence)

    assert context.discovery_structured_match_scope == "multi_category"
    assert context.discovery_structured_match_total_results is None


def test_keyword_fallback_scope_is_unknown():
    evidence = [
        _evidence("industry", "Consumer Goods", evidence_text=_industry_match_provenance({"keyword_match_terms": ["D2C skincare"]})),
        _evidence("country", "United States"),
    ]
    assert _discovery_structured_match_scope(evidence) == "unknown"
    assert _discovery_structured_match_total_results(evidence) is None


def test_no_industry_evidence_scope_is_unknown():
    assert _discovery_structured_match_scope([]) == "unknown"
    assert _discovery_structured_match_total_results([]) is None


def test_pre_phase_15_evidence_without_scope_tag_defaults_to_unknown():
    """An industry_match evidence record predating Phase 15 (no
    match_scope key at all) must default safely, never crash."""
    evidence_text = _industry_match_provenance({"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]})
    evidence = [_evidence("industry", "General Medical", evidence_text=evidence_text)]
    assert _discovery_structured_match_scope(evidence) == "unknown"
    assert _discovery_structured_match_total_results(evidence) is None


def test_malformed_evidence_text_never_crashes():
    evidence = [_evidence("industry", "X", evidence_text="not json {{{")]
    assert _discovery_structured_match_scope(evidence) == "unknown"
    assert _discovery_structured_match_total_results(evidence) is None


# ============================================================================
# CRITICAL: Phase 12 gate is completely unchanged by this phase
# ============================================================================


@respx.mock
def test_single_category_structured_match_with_unverifiable_resolution_holds():
    """A single_category (attributable total_results) structured match
    whose own reported naics_description ("General Medical") never
    exact-matches the requested ICP term ("Healthcare") cannot be trusted
    as proof of membership — no provider data establishes that
    relationship. Per the structured-bridge retirement this correctly
    HOLDs; the LLM is still never called, since HOLD is a deterministic
    hard-rule outcome, not something semantic verification adjudicates."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"business_id": "h6", "name": "Health Co 6", "naics_description": "General Medical", "country_name": "United States", "number_of_employees_range": "51-200"}],
                "total_results": 50, "page": None,
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))
    record = response.data[0]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    icp = _icp(industries=("Healthcare",))
    validation, context = _build_context(icp, evidence)
    assert context.discovery_structured_match_scope == "single_category"
    assert validation.evaluation.overall_result == OverallResult.HOLD

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_HOLD


@respx.mock
def test_multi_category_structured_match_with_unverifiable_resolution_also_holds():
    """Same invariant as the single_category case above, for the
    MULTI-category case (the common one, per Phase 14): resolving 2+
    categories doesn't make the returned naics_description text any more
    provably a member of the ICP terms, so it likewise HOLDs."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Medical Practices"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Medical Practices", "label": "Medical Practices", "value": "medical-practice"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"business_id": "h7", "name": "Health Co 7", "naics_description": "General Medical", "country_name": "United States", "number_of_employees_range": "51-200"}],
                "total_results": 9999, "page": None,
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare", "Medical Practices"]))
    record = response.data[0]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    icp = _icp(industries=("Healthcare", "Medical Practices"))
    validation, context = _build_context(icp, evidence)
    assert context.discovery_structured_match_scope == "multi_category"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_HOLD


@respx.mock
def test_structured_match_with_exact_matching_resolution_still_skips_llm():
    """Positive control: when the branch's own reported naics_description
    text DOES exact-match the requested ICP term, the hard rule
    legitimately PASSes on real evidence, and Phase 12's skip-the-LLM gate
    still trusts it without the extra call — proving that gate itself is
    unaffected by the structured-bridge retirement."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "General Medical"}).mock(
        return_value=httpx.Response(200, json=[{"query": "General Medical", "label": "General Medical", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"business_id": "h8", "name": "Health Co 8", "naics_description": "General Medical", "country_name": "United States", "number_of_employees_range": "51-200"}],
                "total_results": 50, "page": None,
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["General Medical"]))
    record = response.data[0]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    icp = _icp(industries=("General Medical",))
    validation, context = _build_context(icp, evidence)
    assert validation.evaluation.overall_result == OverallResult.PASS

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))

    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.STRUCTURED_MATCH_TRUSTED
    assert result.decision == QualificationDecision.GOOD_FIT


def test_fail_never_reaches_llm_regardless_of_scope_fields():
    icp = _icp(employee_range=EmployeeRange(min=1000, max=5000))
    evidence = [
        _evidence("industry", "Healthcare", evidence_text=_industry_match_provenance({"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]})),
        _evidence("country", "United States"),
        _evidence("employee_range", "11-50"),
    ]
    validation, context = _build_context(icp, evidence)
    assert context.hard_rule_result == OverallResult.FAIL

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))
    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_REJECTED


def test_hold_never_reaches_llm_regardless_of_scope_fields():
    icp = _icp()
    evidence = [
        _evidence("industry", "Healthcare", evidence_text=_industry_match_provenance({"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]})),
        _evidence("country", "United States"),
    ]  # no employee evidence -> HOLD
    validation, context = _build_context(icp, evidence)
    assert context.hard_rule_result == OverallResult.HOLD

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))
    assert call_tracker["called"] is False
    assert result.status == QualificationExecutionStatus.HARD_HOLD


def test_keyword_fallback_still_reaches_llm_unaffected_by_phase_15():
    icp = _icp(industries=("Consumer Goods",))
    evidence = [
        _evidence("industry", "Consumer Goods", evidence_text=_industry_match_provenance({"keyword_match_terms": ["Consumer Goods"]})),
        _evidence("country", "United States"),
        _evidence("employee_range", "51-200"),
    ]
    validation, context = _build_context(icp, evidence)
    assert context.hard_rule_result == OverallResult.PASS
    assert context.discovery_match_type == "keyword_fallback"
    assert context.discovery_structured_match_scope == "unknown"

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    result = qualify_lead(context, _TrackingProvider(response_text=_good_fit_response(context)))
    assert call_tracker["called"] is True
    assert result.status == QualificationExecutionStatus.SUCCESS


# ============================================================================
# User vs AI term provenance still preserved (Phase 13D unaffected)
# ============================================================================


@respx.mock
def test_term_origin_still_correctly_tagged_alongside_new_scope_fields():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "h8", "name": "Health Co 8", "naics_description": "General Medical"}], "total_results": 42, "page": None})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"], term_origin={"healthcare": "ai"}))

    record = response.data[0]
    assert record.attributes["industry_match_term_origins"] == ["ai"]
    assert record.attributes["industry_match_scope"] == "single_category"
    assert record.attributes["industry_match_branch_total_results"] == 42


# ============================================================================
# No fabricated evidence can produce acceptance (existing anti-hallucination check)
# ============================================================================


def test_fabricated_evidence_id_still_rejected_with_scope_fields_present():
    icp = _icp(industries=("Consumer Goods",))
    evidence = [
        _evidence("industry", "Consumer Goods", evidence_text=_industry_match_provenance({"keyword_match_terms": ["Consumer Goods"]})),
        _evidence("country", "United States"),
        _evidence("employee_range", "51-200"),
    ]
    validation, context = _build_context(icp, evidence)

    fabricated = json.dumps(
        {
            "decision": "GOOD_FIT", "confidence": 90, "reason_codes": [], "summary": "ok",
            "supporting_evidence_ids": ["totally-made-up-id"], "risk_evidence_ids": [], "missing_evidence": [],
            "commercial_fit_explanation": "", "hard_rule_acknowledgement": "ok", "uncertainties": [],
        }
    )
    result = qualify_lead(context, MockLLMProvider(response_text=fabricated))
    assert result.status == QualificationExecutionStatus.INVALID_EVIDENCE_IDS
    assert result.decision is None


# ============================================================================
# No new discovery provider / no unnecessary OpenAI calls (structural guards)
# ============================================================================


def test_no_new_discovery_provider_referenced():
    import re

    import app.providers.explorium as explorium_module

    with open(explorium_module.__file__, encoding="utf-8") as f:
        source = f.read().lower()
    assert re.search(r"\bexa\b", source) is None
    assert "perplexity" not in source
    assert "clay" not in source


def test_exactly_one_llm_call_across_a_multi_candidate_batch_with_mixed_scopes():
    """Two structured PASS candidates (one single_category, one
    multi_category) plus one keyword_fallback PASS candidate — only the
    keyword one should ever call the LLM, exactly as before Phase 15."""
    icp = _icp(industries=("Healthcare", "Medical Practices"))

    call_count = {"n": 0}

    class _CountingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_count["n"] += 1
            return super()._call(ctx)

    provider = _CountingProvider(response_text="")

    # Candidate A: single_category structured
    ev_a = [
        _evidence("industry", "General Medical", evidence_text=_industry_match_provenance({"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"], "industry_match_scope": "single_category", "industry_match_resolved_category_count": 1, "industry_match_branch_total_results": 50})),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context_a = _build_context(icp, ev_a)
    provider._response_text = _good_fit_response(context_a)
    qualify_lead(context_a, provider)

    # Candidate B: multi_category structured
    ev_b = [
        _evidence("industry", "General Medical", evidence_text=_industry_match_provenance({"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare", "Medical Practices"], "industry_match_scope": "multi_category", "industry_match_resolved_category_count": 2})),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context_b = _build_context(icp, ev_b)
    provider._response_text = _good_fit_response(context_b)
    qualify_lead(context_b, provider)

    # Candidate C: keyword_fallback
    icp_kw = _icp(industries=("Consumer Goods",))
    ev_c = [
        _evidence("industry", "Consumer Goods", evidence_text=_industry_match_provenance({"keyword_match_terms": ["Consumer Goods"]})),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context_c = _build_context(icp_kw, ev_c)
    provider._response_text = _good_fit_response(context_c)
    qualify_lead(context_c, provider)

    assert call_count["n"] == 1  # only candidate C called the LLM
