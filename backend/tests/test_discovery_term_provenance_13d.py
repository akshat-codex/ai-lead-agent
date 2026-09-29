"""Phase 13D — user-vs-AI term provenance + company_type isolation tests.

Adds an OPTIONAL, purely additive provenance layer on top of Phase 11/13B's
existing tagging — never changing what Explorium's real API request
contains, never changing merge_strategy_into_hard_rules's own deliberate
"user and AI terms become one indistinguishable list" design (see that
function's own docstring, unchanged), and never touching hard_rule_engine.py,
the Phase 11 bridge, or the Phase 12 gate condition:

  1. app/services/discovery_strategy.py::build_term_origin_map — captures
     user-vs-AI origin at the ONE point both are still simultaneously
     knowable (before the merge), as a small side map, never a schema
     change to CanonicalHardRules/CompanyDiscoveryQuery.
  2. app/services/company_discovery.py::run_company_discovery — a new
     OPTIONAL `term_origin` parameter, mirroring the existing provider_order/
     cursors precedent exactly (absent = old behavior, byte-for-byte).
  3. app/providers/explorium.py::execute() — optionally reads
     request.query["term_origin"] to tag industry_match_term_origins/
     keyword_match_term_origins onto NormalizedRecord.attributes; NEVER
     sends it to Explorium's real HTTP request.
  4. app/providers/explorium.py::execute() — keyword_match_term_sources:
     for each term actually sent in the keyword OR-list, whether it came
     from an unmatched INDUSTRY term or a COMPANY_TYPE term — read from
     the real local variables already used to build that OR-list, never
     an invented judgment.
  5. evidence_import.py / qualification_context.py — carry both additions
     through the existing evidence_text provenance channel into
     QualificationContext.discovery_keyword_term_sources, surfaced to
     Phase 12's prompt.

Follows test_explorium_provider.py's exact respx-mocking convention for
provider-level tests, and test_selective_semantic_verification.py's
convention for end-to-end evidence/qualification tests.
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
from app.schemas.discovery_strategy import DiscoveryStrategy
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.services.discovery_strategy import build_term_origin_map, merge_strategy_into_hard_rules
from app.services.evidence_import import _industry_match_provenance
from app.services.qualification_context import _discovery_keyword_term_sources, build_qualification_context
from app.services.hard_icp_validation import validate_against_icp
from app.services.lead_scoring import score_lead
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
        industries=("D2C skincare",),
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


# ============================================================================
# 1. user ICP terms are preserved
# ============================================================================


@respx.mock
def test_user_terms_still_reach_explorium_unchanged_with_term_origin_present():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C skincare", "Consumer Goods"], term_origin={"d2c skincare": "user", "consumer goods": "ai"}))

    calls = [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["D2C skincare", "Consumer Goods"]


# ============================================================================
# 2. AI terms are preserved when useful (structural resolution unaffected)
# ============================================================================


@respx.mock
def test_ai_term_that_resolves_structurally_is_still_used_with_term_origin_present():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"], term_origin={"healthcare": "ai"}))

    calls = [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["linkedin_category"] == {"values": ["healthcare"]}


# ============================================================================
# 3. user vs AI provenance is retained
# ============================================================================


def test_build_term_origin_map_labels_user_and_ai_terms_correctly():
    icp = _icp(industries=("D2C skincare",), company_types=("Wholesale",))
    strategy = DiscoveryStrategy(
        icp_id="icp-1", icp_version=1, status="SUCCESS",
        industry_terms=("Consumer Goods", "E-commerce"), company_type_terms=("D2C",),
        confidence=80, reasoning="expanded",
    )
    origins = build_term_origin_map(icp.hard_rules, strategy)

    assert origins["d2c skincare"] == "user"
    assert origins["wholesale"] == "user"
    assert origins["consumer goods"] == "ai"
    assert origins["e-commerce"] == "ai"
    assert origins["d2c"] == "ai"


def test_build_term_origin_map_prefers_user_when_a_term_appears_in_both():
    """The realistic case: the AI 'reaffirms' a term the user already
    typed (e.g. user typed 'Healthcare', AI also proposes 'healthcare') —
    the user's origin must win, since merge_strategy_into_hard_rules's own
    dedup means the AI's duplicate never separately survives in the merged
    tuple; there is only ever one truthful origin for this key."""
    icp = _icp(industries=("Healthcare",))
    strategy = DiscoveryStrategy(
        icp_id="icp-1", icp_version=1, status="SUCCESS",
        industry_terms=("healthcare",), confidence=80, reasoning="x",
    )
    origins = build_term_origin_map(icp.hard_rules, strategy)
    assert origins["healthcare"] == "user"


@respx.mock
def test_term_origin_tagged_onto_structured_match_evidence():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "hc001", "name": "Health Co", "naics_description": "General Medical"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"], term_origin={"healthcare": "ai"}))

    record = response.data[0]
    assert record.attributes["industry_match_term_origins"] == ["ai"]


@respx.mock
def test_term_origin_tagged_onto_keyword_match_evidence():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "kw001", "name": "Some Co", "naics_description": "X"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C skincare"], term_origin={"d2c skincare": "user"}))

    record = response.data[0]
    assert record.attributes["keyword_match_term_origins"] == ["user"]


@respx.mock
def test_term_origin_absent_produces_no_origin_tags_at_all_backward_compatible():
    """Regression guard: when term_origin is not supplied (every
    pre-Phase-13D caller), no origin keys are added at all — not even an
    "unknown" placeholder — confirming byte-for-byte backward
    compatibility."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "hc001", "name": "Health Co", "naics_description": "General Medical"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))  # no term_origin at all

    record = response.data[0]
    assert "industry_match_term_origins" not in record.attributes
    assert "keyword_match_term_origins" not in record.attributes


# ============================================================================
# 4. structured matches remain preferred (precedence unchanged)
# ============================================================================


@respx.mock
def test_structured_still_preferred_over_keyword_with_term_origin_present():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Widgetology"], term_origin={"healthcare": "user", "widgetology": "ai"}))

    calls = [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]
    assert len(calls) == 2
    all_filters = [json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert keyword["website_keywords"] == {"values": ["Widgetology"], "operator": "or"}


# ============================================================================
# 5. keyword fallback remains available for genuine unmatched concepts
# ============================================================================


@respx.mock
def test_keyword_fallback_still_fires_for_a_genuinely_unmatched_term():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "nd001", "name": "Niche Co", "naics_description": "X"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["microdrama short-video production"], term_origin={"microdrama short-video production": "user"}))

    assert len(response.data) == 1
    assert response.data[0].attributes["keyword_match_terms"] == ["microdrama short-video production"]


# ============================================================================
# 6. company_type does not unnecessarily pollute industry discovery
# ============================================================================


@respx.mock
def test_company_type_and_industry_keyword_terms_are_distinguishable_by_source():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "src001", "name": "Some Co", "naics_description": "X"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Entertainment"], company_types=["D2C"]))

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["Entertainment", "D2C"]
    assert record.attributes["keyword_match_term_sources"] == ["industry", "company_type"]


@respx.mock
def test_company_type_term_that_also_appears_as_an_industry_term_is_reported_as_industry():
    """A term appearing in BOTH the unmatched-industries list and
    company_types (the pre-existing dedup case) is reported as its
    industry origin, matching the existing merge's own "industry list
    wins the single surviving entry" behavior (all_keyword_terms starts
    from keyword_terms, company_types are only appended if not already
    present)."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "dup001", "name": "Some Co", "naics_description": "X"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Startup"], company_types=["Startup"]))

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["Startup"]
    assert record.attributes["keyword_match_term_sources"] == ["industry"]


@respx.mock
def test_company_types_alone_source_is_company_type():
    # Phase 41: company_types terms now attempt the same structured
    # autocomplete industries already get — mocked here as "no exact match
    # anywhere" so both terms still fall to the keyword tier, exactly the
    # scenario this test's own assertions describe.
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "ct001", "name": "Some Co", "naics_description": "X"}]})
    )
    provider = _provider()
    response = provider.run(_request(company_types=["D2C", "PE-backed"]))

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["D2C", "PE-backed"]
    assert record.attributes["keyword_match_term_sources"] == ["company_type", "company_type"]


# ============================================================================
# 7. broad/redundant terms do not create unnecessary keyword expansion
# (13C's dedup/cap already covers this; confirming it composes correctly
# with 13D's new tagging)
# ============================================================================


@respx.mock
def test_deduped_keyword_list_still_produces_correctly_aligned_sources():
    """13C's dedup runs before 13D's source-tagging — the aligned
    term_sources list must match the ALREADY-deduped term list length,
    never the pre-dedup one."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "rd001", "name": "Some Co", "naics_description": "X"}]})
    )
    provider = _provider()
    # "D2C" appears in both industries (unmatched) and company_types — deduped to one entry
    response = provider.run(_request(industries=["D2C", "Entertainment"], company_types=["D2C"]))

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["D2C", "Entertainment"]
    assert len(record.attributes["keyword_match_term_sources"]) == len(record.attributes["keyword_match_terms"])


# ============================================================================
# 8. keyword_match_terms remains accurate (13B regression guard)
# ============================================================================


@respx.mock
def test_keyword_match_terms_field_itself_unchanged_by_13d():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "kt001", "name": "Some Co", "naics_description": "X"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C skincare", "Consumer Goods"]))

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["D2C skincare", "Consumer Goods"]


# ============================================================================
# 9. Phase 12 receives correct discovery provenance
# ============================================================================


def test_discovery_keyword_term_sources_extracted_correctly():
    evidence_text = _industry_match_provenance(
        {"keyword_match_terms": ["Entertainment", "D2C"], "keyword_match_term_sources": ["industry", "company_type"]}
    )
    evidence = [_evidence("industry", "X", evidence_text=evidence_text), _evidence("country", "United States")]
    assert _discovery_keyword_term_sources(evidence) == ("industry", "company_type")


def test_discovery_keyword_term_sources_empty_when_not_supplied():
    evidence_text = _industry_match_provenance({"keyword_match_terms": ["D2C skincare"]})
    evidence = [_evidence("industry", "X", evidence_text=evidence_text)]
    assert _discovery_keyword_term_sources(evidence) == ()


def test_qualification_context_carries_term_sources_end_to_end():
    icp = _icp(industries=("Entertainment",))
    evidence_text = _industry_match_provenance(
        {"keyword_match_terms": ["Entertainment", "D2C"], "keyword_match_term_sources": ["industry", "company_type"]}
    )
    evidence = [
        _evidence("company_identity", "Some Co"),
        _evidence("industry", "Consumer Goods", evidence_text=evidence_text),
        _evidence("country", "United States"),
        _evidence("employee_range", "51-200"),
    ]
    validation = validate_against_icp(icp, "company-1", evidence, None, [])
    score = score_lead(
        icp=icp, company_id="company-1", company_evidence=evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    context = build_qualification_context(
        icp=icp, company_id="company-1", company_evidence=evidence,
        hard_rule_evaluation=validation.evaluation, business_model=None,
        commercial_signals=[], score=score,
    )
    assert context.discovery_keyword_term_sources == ("industry", "company_type")


def test_prompt_includes_discovery_keyword_term_sources():
    from app.schemas.hard_rule_result import OverallResult
    from app.schemas.llm_qualification import QualificationContext
    from app.services.llm_qualification import build_prompt

    context = QualificationContext(
        icp_id="icp-1", icp_version=1, icp_industries=("Entertainment",), icp_geography=("US",),
        icp_employee_range="10-200", icp_allowed_titles=(), icp_company_types=(), icp_exclusions=(),
        icp_soft_preferences=(), company_id="company-1", person_id=None, hard_rule_result=OverallResult.PASS,
        rule_results=(), reason_codes=(), discovery_match_type="keyword_fallback",
        discovery_keyword_terms=("Entertainment", "D2C"), discovery_keyword_term_sources=("industry", "company_type"),
        business_model_summary=None, commercial_signal_summary=(), icp_score=80.0, commercial_score=50.0,
        evidence_score=70.0, freshness_score=None, identity_confidence=90.0, final_score=75.0, evidence=(),
        conflicting_fields=(), missing_critical_fields=(),
    )
    prompt = build_prompt(context)
    assert "discovery_keyword_term_sources" in prompt
    assert "company_type" in prompt


# ============================================================================
# 10 & 11. no additional LLM calls / no additional discovery provider
# ============================================================================


def test_no_new_llm_call_introduced_by_batch_orchestration_wiring():
    """build_term_origin_map itself makes zero provider calls — confirmed
    directly: it only reads already-computed CanonicalHardRules/
    DiscoveryStrategy fields, never calls interpret_icp or any LLMProvider
    itself."""
    import inspect

    from app.services.discovery_strategy import build_term_origin_map

    source = inspect.getsource(build_term_origin_map)
    assert "provider.qualify" not in source
    assert "interpret_icp(" not in source


def test_no_new_discovery_provider_referenced_in_this_phases_files():
    import re

    import app.providers.explorium as explorium_module
    import app.services.discovery_strategy as strategy_module
    import app.services.company_discovery as discovery_module

    for module in (explorium_module, strategy_module, discovery_module):
        with open(module.__file__, encoding="utf-8") as f:
            source = f.read().lower()
        # Word-boundary matches only — "exa" alone would false-positive on
        # "example"/"exact"/etc., which this codebase's own comments use
        # constantly and legitimately. Apollo/Unipile are NOT checked here:
        # both are real, pre-existing providers this codebase already
        # references descriptively in comments (e.g. explorium.py's own
        # "mock/apollo-supplied attributes" note) — the actual constraint
        # is "no NEW discovery provider," and neither is one (Apollo is
        # PERSON_ENRICHMENT-only, Unipile is PEOPLE_DISCOVERY/
        # COMPANY_ENRICHMENT-only — neither has ever been COMPANY_DISCOVERY,
        # confirmed in every prior phase this session).
        assert re.search(r"\bexa\b", source) is None
        assert "perplexity" not in source
        assert "clay" not in source


# ============================================================================
# 12. existing Explorium behavior remains backward compatible
# ============================================================================


@respx.mock
def test_every_pre_13d_test_shape_still_works_without_term_origin():
    """Regression guard mirroring test_mixed_icp_uses_structured_and_keyword_branches
    from test_explorium_provider.py exactly, with NO term_origin passed at
    all — confirming zero behavior change for every caller that doesn't
    opt into the new provenance parameter."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Widgetology"]))

    calls = [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]
    assert len(calls) == 2
    all_filters = [json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert keyword["website_keywords"] == {"values": ["Widgetology"], "operator": "or"}


@respx.mock
def test_response_data_shape_unaffected_without_term_origin():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "bc001", "name": "Example Test Co", "domain": "example-test.invalid",
                        "country_name": "United States", "number_of_employees_range": "11-50",
                        "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
                        "linkedin_profile": "https://www.linkedin.com/company/example-test",
                        "yearly_revenue_range": "1M-10M",
                    }
                ]
            },
        )
    )
    provider = _provider()
    response = provider.run(_request())

    record = response.data[0]
    assert record.attributes == {
        "domain": "example-test.invalid",
        "country": "United States",
        "industry": "Cosmetics, Beauty Supplies, and Perfume Stores",
        "employee_range": "11-50",
        "linkedin_id": "https://www.linkedin.com/company/example-test",
        "revenue_range": "1M-10M",
    }
