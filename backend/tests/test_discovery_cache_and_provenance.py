"""Phase 13A + 13B — regression tests for:

  13A: app/providers/explorium.py::_lookup_category's cache now only ever
       stores a REAL, CONFIRMED Explorium answer (a genuine exact match or
       a genuine no-match, both from a real HTTP 200 response) — a
       transient failure (timeout, network error, non-200, malformed body)
       is never cached, so the very next lookup for the same term retries
       against the real API instead of being permanently poisoned for the
       lifetime of the process (this provider is a module-level singleton
       — see app/providers/default_registry.py).

  13B: keyword-fallback records are now tagged with
       attributes["keyword_match_terms"] — the exact website_keywords
       term(s) actually in play for that round — carried through
       evidence_import.py's evidence_text (same field, same JSON-envelope
       pattern as Phase 11's industry_match provenance) into
       QualificationContext.discovery_keyword_terms, surfaced to the LLM
       verification prompt. Never touches hard_rule_engine.py, the Phase
       12 gate condition, or structured linkedin_category/naics_category
       behavior.

Follows test_explorium_provider.py's exact respx-mocking convention.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.services.evidence_import import KEYWORD_MATCH_PROVENANCE_KEY, _industry_match_provenance
from app.services.qualification_context import _discovery_keyword_terms, _discovery_match_type

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _provider() -> ExploriumCompanyDiscoveryProvider:
    return ExploriumCompanyDiscoveryProvider(api_key="test-key")


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query=query)


def _evidence(field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=EntityType.COMPANY,
        entity_id="company-1",
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


# ============================================================================
# 13A — cache correctness
# ============================================================================


@respx.mock
def test_transient_timeout_is_never_cached_next_lookup_retries():
    """A timeout on the FIRST autocomplete call must not poison the cache
    — a SECOND discovery round for the same term must retry against the
    real API rather than reusing a cached failure."""
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"})
    route.side_effect = [
        httpx.TimeoutException("timed out"),
        httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]),
    ]
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))  # first call: timeout, falls to keyword (uncached)
    provider.run(_request(industries=["Healthcare"]))  # second call: must RETRY, not reuse a cached failure

    assert route.call_count == 2


@respx.mock
def test_transient_http_500_is_never_cached_next_lookup_retries():
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"})
    route.side_effect = [
        httpx.Response(500, text="internal error"),
        httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]),
    ]
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))

    assert route.call_count == 2


@respx.mock
def test_malformed_json_body_is_never_cached_next_lookup_retries():
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"})
    route.side_effect = [
        httpx.Response(200, content=b"not json at all {{{"),
        httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]),
    ]
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))

    assert route.call_count == 2


@respx.mock
def test_failure_then_retry_then_successful_lookup_resolves_structurally():
    """End-to-end: a transient failure on round 1 must not prevent round 2
    from correctly resolving the term through the real structured
    linkedin_category match — i.e. the fix actually improves discovery
    quality, not just call counts."""
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"})
    route.side_effect = [
        httpx.TimeoutException("timed out"),
        httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]),
    ]
    search_route = respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))

    # The second call's actual filter must be the real structured
    # linkedin_category match, not another keyword fallback.
    sent_filters = json.loads(search_route.calls[-1].request.content)["filters"]
    assert sent_filters.get("linkedin_category") == {"values": ["healthcare"]}
    assert "website_keywords" not in sent_filters


@respx.mock
def test_genuine_no_exact_match_is_still_cached_never_repeated():
    """A REAL 200 response with no matching label is a genuine answer —
    caching it must be preserved exactly as before (regression guard)."""
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Widgetology", "label": "Something Else Entirely", "value": "x"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Widgetology"]))
    provider.run(_request(industries=["Widgetology"]))

    assert route.call_count == 1  # never repeated — genuine no-match is a confirmed, cacheable answer


@respx.mock
def test_genuine_exact_match_is_still_cached_never_repeated():
    """Regression guard for the pre-existing cache behavior
    (test_explorium_provider.py already covers this; repeated here to
    confirm 13A's change doesn't weaken it)."""
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))

    assert route.call_count == 1


@respx.mock
def test_repeated_transient_failures_retry_every_time_never_give_up_forever():
    route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"})
    route.side_effect = [httpx.TimeoutException("timed out")] * 3
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))

    assert route.call_count == 3  # every single round retries — no failure is ever cached


@respx.mock
def test_naics_tier_cache_also_never_poisoned_by_a_transient_failure():
    """Same fix, applied to the NAICS tier — 13A must not be
    linkedin_category-specific."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    naics_route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Widgetology"})
    naics_route.side_effect = [
        httpx.TimeoutException("timed out"),
        httpx.Response(200, json=[{"query": "Widgetology", "label": "Widgetology", "value": "999999"}]),
    ]
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Widgetology"]))
    provider.run(_request(industries=["Widgetology"]))

    assert naics_route.call_count == 2


# ============================================================================
# 13B — keyword discovery provenance
# ============================================================================


@respx.mock
def test_keyword_fallback_record_is_tagged_with_the_exact_keyword_terms():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C skincare"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "D2C skincare"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "kw0000000000000000000000000001",
                        "name": "Glow & Co Skincare",
                        "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
                    }
                ]
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C skincare"]))

    record = response.data[0]
    assert "industry_match_branch" not in record.attributes
    assert record.attributes["keyword_match_terms"] == ["D2C skincare"]


@respx.mock
def test_multiple_unmatched_terms_all_appear_in_the_keyword_tag():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C skincare"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "D2C skincare"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Consumer Goods"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Consumer Goods"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "kw0000000000000000000000000002", "name": "Some Co", "naics_description": "X"}]}
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C skincare", "Consumer Goods"]))

    record = response.data[0]
    assert set(record.attributes["keyword_match_terms"]) == {"D2C skincare", "Consumer Goods"}


@respx.mock
def test_company_types_terms_also_appear_in_the_keyword_tag():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "kw0000000000000000000000000003", "name": "Some Co", "naics_description": "X"}]}
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C"], company_types=["Wholesale"]))

    record = response.data[0]
    assert set(record.attributes["keyword_match_terms"]) == {"D2C", "Wholesale"}


@respx.mock
def test_structured_match_never_receives_a_keyword_match_tag():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "st0000000000000000000000000001",
                        "name": "Midwest Regional Health Group",
                        "naics_description": "General Medical and Surgical Hospitals",
                    }
                ]
            },
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))

    record = response.data[0]
    assert record.attributes["industry_match_branch"] == "linkedin_category"
    assert "keyword_match_terms" not in record.attributes


@respx.mock
def test_keyword_less_request_shape_has_no_keyword_tag_at_all():
    """Neither industries nor company_types stated — the pre-existing
    keyword-less request shape must be completely unaffected."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "nl0000000000000000000000000001", "name": "Some Co"}]}
        )
    )
    provider = _provider()
    response = provider.run(_request())

    record = response.data[0]
    assert "keyword_match_terms" not in record.attributes
    assert "industry_match_branch" not in record.attributes


def test_evidence_import_carries_keyword_match_terms_into_evidence_text():
    provenance = _industry_match_provenance({"keyword_match_terms": ["D2C skincare", "Consumer Goods"]})
    assert provenance is not None
    parsed = json.loads(provenance)
    assert parsed[KEYWORD_MATCH_PROVENANCE_KEY]["terms"] == ["D2C skincare", "Consumer Goods"]


def test_evidence_import_preserves_both_industry_match_and_keyword_match_when_both_present():
    """Phase 37 correction: a record CAN genuinely carry both tags at once
    — app/providers/explorium.py's own _merge_cross_branch_attributes
    combines provenance from every discovery branch that returned the
    SAME real company within one call (e.g. a structured Healthcare hit
    AND a keyword SaaS hit for a genuinely Healthcare+SaaS company). This
    is no longer an anomaly to defensively resolve by preferring one tag
    over the other — BOTH must be preserved, since
    app/services/hard_icp_validation.py::_cross_branch_corroborated_terms
    reads both to recognize genuine cross-branch corroboration for a
    compound ICP. Silently dropping either one here would make that
    corroboration invisible to validation no matter how the discovery
    layer merged it."""
    provenance = _industry_match_provenance(
        {
            "industry_match_branch": "linkedin_category",
            "industry_match_terms": ["Healthcare"],
            "keyword_match_terms": ["SaaS"],
        }
    )
    parsed = json.loads(provenance)
    assert "industry_match" in parsed
    assert "keyword_match" in parsed
    assert parsed["industry_match"]["icp_terms"] == ["Healthcare"]
    assert parsed["keyword_match"]["terms"] == ["SaaS"]


def test_discovery_keyword_terms_extracted_correctly_from_evidence():
    evidence = [
        _evidence("industry", "Consumer Goods", evidence_text=_industry_match_provenance({"keyword_match_terms": ["D2C skincare"]})),
        _evidence("country", "United States"),
    ]
    assert _discovery_keyword_terms(evidence) == ("D2C skincare",)
    assert _discovery_match_type(evidence) == "keyword_fallback"


def test_discovery_keyword_terms_empty_for_structured_match():
    evidence = [
        _evidence(
            "industry", "General Medical and Surgical Hospitals",
            evidence_text=_industry_match_provenance(
                {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]}
            ),
        ),
        _evidence("country", "United States"),
    ]
    assert _discovery_keyword_terms(evidence) == ()
    assert _discovery_match_type(evidence) == "structured"


def test_discovery_keyword_terms_empty_when_no_provenance_tag_at_all():
    evidence = [_evidence("industry", "Consumer Goods"), _evidence("country", "United States")]
    assert _discovery_keyword_terms(evidence) == ()
    assert _discovery_match_type(evidence) == "keyword_fallback"


def test_discovery_keyword_terms_never_crashes_on_malformed_evidence_text():
    evidence = [_evidence("industry", "Consumer Goods", evidence_text="not json {{{")]
    assert _discovery_keyword_terms(evidence) == ()


# --- Phase 12 gate logic unaffected -----------------------------------------


def test_phase_12_gate_still_fires_correctly_with_keyword_terms_present():
    """Confirms 13B does not change Phase 12's gate: a keyword_fallback
    candidate (now carrying discovery_keyword_terms) still reaches the LLM
    exactly as before; a structured candidate still skips it."""
    from app.schemas.canonical_icp import (
        CanonicalGeography,
        CanonicalHardRules,
        CanonicalICP,
        CanonicalSoftPreferences,
        EmployeeRange,
        GeographyEntry,
    )
    from app.schemas.hard_rule_result import OverallResult
    from app.schemas.llm_qualification import QualificationExecutionStatus
    from app.schemas.scoring import ResolutionSignal
    from app.services.hard_icp_validation import validate_against_icp
    from app.services.lead_scoring import score_lead
    from app.services.llm_providers.mock import MockLLMProvider
    from app.services.llm_qualification import qualify_lead

    icp = CanonicalICP(
        icp_id="icp-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("Consumer Goods",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=10, max=200),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    evidence = [
        _evidence("company_identity", "Some Co"),
        _evidence("industry", "Consumer Goods", evidence_text=_industry_match_provenance({"keyword_match_terms": ["Consumer Goods"]})),
        _evidence("country", "United States"),
        _evidence("employee_range", "51-200"),
    ]
    validation = validate_against_icp(icp, "company-1", evidence, None, [])
    assert validation.evaluation.overall_result == OverallResult.PASS

    score = score_lead(
        icp=icp, company_id="company-1", company_evidence=evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    from app.services.qualification_context import build_qualification_context

    context = build_qualification_context(
        icp=icp, company_id="company-1", company_evidence=evidence,
        hard_rule_evaluation=validation.evaluation, business_model=None,
        commercial_signals=[], score=score,
    )
    assert context.discovery_match_type == "keyword_fallback"
    assert context.discovery_keyword_terms == ("Consumer Goods",)

    call_tracker = {"called": False}

    class _TrackingProvider(MockLLMProvider):
        def _call(self, ctx):
            call_tracker["called"] = True
            return super()._call(ctx)

    good_fit = json.dumps(
        {
            "decision": "GOOD_FIT", "confidence": 80, "reason_codes": [], "summary": "ok",
            "supporting_evidence_ids": [context.evidence[0].id], "risk_evidence_ids": [], "missing_evidence": [],
            "commercial_fit_explanation": "", "hard_rule_acknowledgement": "PASS acknowledged.", "uncertainties": [],
        }
    )
    result = qualify_lead(context, _TrackingProvider(response_text=good_fit))

    assert call_tracker["called"] is True  # keyword_fallback still reaches the LLM, unchanged by 13B
    assert result.status == QualificationExecutionStatus.SUCCESS


def test_prompt_includes_discovery_keyword_terms():
    from app.schemas.llm_qualification import QualificationContext
    from app.schemas.hard_rule_result import OverallResult
    from app.services.llm_qualification import build_prompt

    context = QualificationContext(
        icp_id="icp-1", icp_version=1, icp_industries=("Consumer Goods",), icp_geography=("US",),
        icp_employee_range="10-200", icp_allowed_titles=(), icp_company_types=(), icp_exclusions=(),
        icp_soft_preferences=(), company_id="company-1", person_id=None, hard_rule_result=OverallResult.PASS,
        rule_results=(), reason_codes=(), discovery_match_type="keyword_fallback",
        discovery_keyword_terms=("Consumer Goods", "D2C"), business_model_summary=None,
        commercial_signal_summary=(), icp_score=80.0, commercial_score=50.0, evidence_score=70.0,
        freshness_score=None, identity_confidence=90.0, final_score=75.0, evidence=(), conflicting_fields=(),
        missing_critical_fields=(),
    )
    prompt = build_prompt(context)
    assert "Consumer Goods" in prompt
    assert "D2C" in prompt
    assert "discovery_keyword_terms" in prompt
