"""Phase 41 — company_type discovery precision regression tests.

Before this phase, CompanyDiscoveryQuery.company_types NEVER attempted a
structured taxonomy lookup at all: every company_type term went straight
into the low-precision website_keywords OR-list (which matches any word
appearing anywhere on a company's website — see
app/providers/explorium.py's own docstring, where a
website_keywords=[Healthcare] sample returned 0/10 genuinely healthcare
companies), regardless of whether a real, exact linkedin_category /
naics_category match existed for it. Industries always got the
structured-first attempt; company_types never did.

Phase 41 gives company_types the SAME structured-taxonomy-first attempt,
via the SAME live autocomplete mechanism, exact-match-only discipline,
and per-instance cache — with two hard safety properties this file
verifies directly:

  1. A resolved company_type term gets its OWN branch
     (_STRUCTURED_COMPANY_TYPE_BRANCH / _NAICS_COMPANY_TYPE_BRANCH),
     never merged into an industry structured branch — Explorium ANDs
     filters within one request, so combining them would wrongly require
     a candidate to satisfy an industry category AND a company-type
     category simultaneously.
  2. Its provenance uses a DISTINCT key set (company_type_match_*, never
     industry_match_*), so no downstream consumer can read a
     company_type structured match as INDUSTRY evidence. Crucially,
     app/services/evidence_import.py::_industry_match_provenance returns
     None for such a record (verified below), so
     hard_icp_validation.py's industry bridging/corroboration sees
     nothing from it at all — this phase creates no new PASS path, and
     the `company_type` hard rule's existing HOLD-only behavior for
     Explorium candidates (no company_type value is ever written from
     Explorium data — company_type is not in _BUSINESS_ATTRIBUTE_MAP) is
     completely unchanged.

Follows tests/test_explorium_provider.py's exact respx-mocking convention.
"""
import json

import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.services.evidence_import import _industry_match_provenance

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"


def _provider(api_key: str = "test-key-12345") -> ExploriumCompanyDiscoveryProvider:
    return ExploriumCompanyDiscoveryProvider(api_key=api_key)


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query=query)


def _business_calls():
    return [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]


def _filters_by_call():
    return [json.loads(c.request.content).get("filters", {}) for c in _business_calls()]


def _mock_linkedin_match(query: str, label: str, value: str):
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": query}).mock(
        return_value=httpx.Response(200, json=[{"query": query, "label": label, "value": value}])
    )


def _mock_naics_match(query: str, label: str, value: str):
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": query}).mock(
        return_value=httpx.Response(200, json=[{"query": query, "label": label, "value": value}])
    )


def _mock_no_match_everywhere_else():
    """Catch-all: every autocomplete query not explicitly mocked above
    reports a real, confirmed "no exact match" (HTTP 200, empty list) —
    never an unmocked-request error, and never a transient failure (which
    app/providers/explorium.py deliberately does NOT cache)."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))


def _one_company_response(business_id: str = "ct001", name: str = "Some Co", industry: str = "Software Publishers"):
    return httpx.Response(
        200, json={"data": [{"business_id": business_id, "name": name, "naics_description": industry}]}
    )


# ============================================================================
# 1. company_type-only ICP
# ============================================================================


@respx.mock
def test_company_type_only_icp_uses_a_structured_branch_when_the_term_resolves():
    """The core precision improvement: a company_type-only ICP whose term
    exactly resolves on linkedin_category now queries that real, closed
    taxonomy instead of the low-precision website_keywords OR-list."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["Startup"]))

    assert response.success is True
    filters = _filters_by_call()
    assert len(filters) == 1
    assert filters[0]["linkedin_category"] == {"values": ["startup"]}
    assert "website_keywords" not in filters[0]  # no low-precision fallback at all


@respx.mock
def test_company_type_only_icp_falls_back_to_keyword_when_nothing_resolves():
    """Fallback preserved exactly: a company_type term with no exact match
    on EITHER taxonomy still reaches Explorium via website_keywords, never
    dropped and never forced into a structured branch it doesn't resolve
    into — this is the "D2C / FMCG / OTT Platforms / Microdrama Companies"
    reality this module already confirmed live for industry-shaped terms."""
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["Microdrama Companies"]))

    assert response.success is True
    filters = _filters_by_call()
    assert len(filters) == 1
    assert filters[0]["website_keywords"] == {"values": ["Microdrama Companies"], "operator": "or"}
    assert "linkedin_category" not in filters[0]
    assert "naics_category" not in filters[0]


@respx.mock
def test_company_type_only_icp_resolving_via_naics_uses_the_naics_branch():
    """Second structured tier works for company_types too: no
    linkedin_category match, but a real naics_category one."""
    _mock_naics_match("Wholesale Distributor", "Wholesale Distributor", "424990")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    provider.run(_request(company_types=["Wholesale Distributor"]))

    filters = _filters_by_call()
    assert len(filters) == 1
    assert filters[0]["naics_category"] == {"values": ["424990"]}
    assert "website_keywords" not in filters[0]


# ============================================================================
# 2. company_type + industry ICP
# ============================================================================


@respx.mock
def test_industry_and_company_type_resolve_into_separate_branches_never_anded():
    """The hard correctness constraint: a resolved INDUSTRY
    linkedin_category value and a resolved COMPANY_TYPE linkedin_category
    value must never share one request — Explorium ANDs filters within a
    call, so combining them would wrongly require a candidate to be both
    simultaneously. They must be two SEPARATE calls, OR-unioned at this
    adapter's own merge layer (exactly as the industry structured/naics
    branches already are)."""
    _mock_linkedin_match("Healthcare", "Healthcare", "healthcare")
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"], company_types=["Startup"]))

    filters = _filters_by_call()
    assert len(filters) == 2  # one industry branch + one company_type branch, never one combined call
    linkedin_value_sets = sorted(tuple(f["linkedin_category"]["values"]) for f in filters)
    assert linkedin_value_sets == [("healthcare",), ("startup",)]
    # Neither call ANDs the two together.
    for f in filters:
        assert len(f["linkedin_category"]["values"]) == 1


@respx.mock
def test_industry_structured_plus_company_type_keyword_fallback_coexist():
    """Mixed tiers: the industry term resolves structurally, the
    company_type term doesn't and falls to keyword — both signals still
    reach Explorium, in their own separate branches."""
    _mock_linkedin_match("Healthcare", "Healthcare", "healthcare")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"], company_types=["PE-backed"]))

    filters = _filters_by_call()
    assert len(filters) == 2
    structured = next(f for f in filters if "linkedin_category" in f)
    keyword = next(f for f in filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert keyword["website_keywords"] == {"values": ["PE-backed"], "operator": "or"}


@respx.mock
def test_resolved_company_type_term_is_not_also_duplicated_into_the_keyword_list():
    """A company_type term that resolved structurally must NOT also appear
    in the keyword OR-list — that would be redundant (this adapter already
    OR-unions branches at its merge layer) and would silently re-introduce
    the exact low-precision matching the structured branch exists to
    avoid."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Widgetology"], company_types=["Startup"]))

    filters = _filters_by_call()
    keyword = next((f for f in filters if "website_keywords" in f), None)
    assert keyword is not None  # "Widgetology" (unmatched industry) still needs it
    assert keyword["website_keywords"]["values"] == ["Widgetology"]
    assert "Startup" not in keyword["website_keywords"]["values"]


# ============================================================================
# 3. multiple company types
# ============================================================================


@respx.mock
def test_multiple_company_types_resolving_on_the_same_taxonomy_share_one_branch():
    """Two company_type terms both resolving on linkedin_category share
    ONE branch's OR-list (Explorium accepts multiple values per field) —
    mirroring exactly how multiple resolved industry terms already
    behave."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_linkedin_match("Nonprofit", "Nonprofit", "nonprofit")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(company_types=["Startup", "Nonprofit"]))

    filters = _filters_by_call()
    assert len(filters) == 1
    assert filters[0]["linkedin_category"] == {"values": ["startup", "nonprofit"]}


@respx.mock
def test_multiple_company_types_split_across_taxonomies_and_keyword():
    """Three company_type terms, one per tier: linkedin_category,
    naics_category, and keyword fallback — each lands in its own correct
    branch, none dropped, none conflated."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_naics_match("Wholesale Distributor", "Wholesale Distributor", "424990")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(company_types=["Startup", "Wholesale Distributor", "PE-backed"]))

    filters = _filters_by_call()
    assert len(filters) == 3
    linkedin = next(f for f in filters if "linkedin_category" in f)
    naics = next(f for f in filters if "naics_category" in f)
    keyword = next(f for f in filters if "website_keywords" in f)
    assert linkedin["linkedin_category"] == {"values": ["startup"]}
    assert naics["naics_category"] == {"values": ["424990"]}
    assert keyword["website_keywords"] == {"values": ["PE-backed"], "operator": "or"}


# ============================================================================
# 4/5. provenance + trust status (the safety core)
# ============================================================================


@respx.mock
def test_structured_company_type_record_carries_distinct_company_type_provenance():
    """A company_type structured match is tagged with company_type_match_*
    keys — NEVER industry_match_*, which would let it be read as industry
    evidence downstream."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["Startup"]))

    record = response.data[0]
    assert record.attributes["company_type_match_branch"] == "linkedin_category"
    assert record.attributes["company_type_match_terms"] == ["Startup"]
    assert record.attributes["company_type_match_resolved_category_count"] == 1
    # The critical negative assertions — no industry/keyword provenance at all.
    assert "industry_match_branch" not in record.attributes
    assert "industry_match_terms" not in record.attributes
    assert "keyword_match_terms" not in record.attributes


@respx.mock
def test_company_type_structured_match_is_never_written_as_industry_evidence():
    """The end-to-end trust guarantee: evidence_import.py's own
    provenance writer produces NO industry provenance for a
    company_type-structured record, so hard_icp_validation.py's industry
    bridging/corroboration (Phases 11/25/37/38/39) sees nothing from it
    and this phase can never create a new industry PASS path."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["Startup"]))

    assert _industry_match_provenance(response.data[0].attributes) is None


@respx.mock
def test_company_type_keyword_fallback_records_still_tagged_as_company_type_sourced():
    """Unchanged Phase 13D behavior: an UNRESOLVED company_type term that
    falls to the keyword OR-list is still tagged keyword_match_term_sources
    == ["company_type"], so a downstream consumer can tell it apart from a
    broad unmatched-industry keyword hit — the low-precision signal is
    still honestly labeled as low-precision, never upgraded."""
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["PE-backed"]))

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["PE-backed"]
    assert record.attributes["keyword_match_term_sources"] == ["company_type"]
    assert "company_type_match_branch" not in record.attributes  # never claims a structured match it didn't get


@respx.mock
def test_company_type_term_origins_tagged_when_term_origin_supplied():
    """Phase 13D's user-vs-AI origin tagging extends to the new
    company_type structured provenance, additively — absent entirely when
    term_origin isn't supplied (every other test in this file)."""
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["Startup"], term_origin={"startup": "user"}))

    assert response.data[0].attributes["company_type_match_term_origins"] == ["user"]


@respx.mock
def test_cross_branch_merge_preserves_both_industry_and_company_type_provenance():
    """The SAME real company returned by BOTH an industry structured
    branch AND a company_type structured branch keeps BOTH provenance
    tags on the one merged record — neither is lost, and they remain
    separately identifiable."""
    _mock_linkedin_match("Healthcare", "Healthcare", "healthcare")
    _mock_naics_match("Startup", "Startup", "999999")
    _mock_no_match_everywhere_else()
    shared = {"business_id": "shared001", "name": "Acme Health Startup", "naics_description": "General Medical"}
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [shared]}))

    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"], company_types=["Startup"]))

    assert len(response.data) == 1  # merged into ONE record, never two
    attrs = response.data[0].attributes
    assert attrs["industry_match_branch"] == "linkedin_category"
    assert attrs["industry_match_terms"] == ["Healthcare"]
    assert attrs["company_type_match_branch"] == "naics_category"
    assert attrs["company_type_match_terms"] == ["Startup"]


# ============================================================================
# 6. Phase 0-3 compound behavior unchanged
# ============================================================================


@respx.mock
def test_compound_industry_cross_branch_corroboration_unaffected():
    """Phase 0/37's compound-ICP cross-branch corroboration (Healthcare
    structured + SaaS keyword, same company, one merged record carrying
    BOTH tags) must be byte-for-byte unaffected by this phase — verified
    with no company_types stated at all."""
    _mock_linkedin_match("Healthcare", "Healthcare", "healthcare")
    _mock_no_match_everywhere_else()
    shared = {"business_id": "corr001", "name": "Acme Health SaaS", "naics_description": "General Medical"}
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [shared]}))

    provider = _provider()
    response = provider.run(_request(industries=["Healthcare", "SaaS"]))

    assert len(response.data) == 1
    attrs = response.data[0].attributes
    assert attrs["industry_match_terms"] == ["Healthcare"]
    assert attrs["keyword_match_terms"] == ["SaaS"]


@respx.mock
def test_phase_40_per_branch_page_size_applies_to_company_type_branches_too():
    """Phase 40's "each active branch gets the full limit, never a split"
    fix applies uniformly to the new company_type branches — they are
    ordinary branches to that logic, not a special case."""
    _mock_linkedin_match("Healthcare", "Healthcare", "healthcare")
    _mock_linkedin_match("Startup", "Startup", "startup")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"], company_types=["Startup"], limit=25))

    page_sizes = [json.loads(c.request.content)["page_size"] for c in _business_calls()]
    assert page_sizes == [25, 25]


@respx.mock
def test_keyword_less_request_shape_still_unchanged():
    """Neither industries nor company_types stated — still exactly one
    plain, filter-less request, untouched by this phase."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request())

    calls = _business_calls()
    assert len(calls) == 1
    body = json.loads(calls[0].request.content)
    assert "website_keywords" not in body.get("filters", {})
    assert "linkedin_category" not in body.get("filters", {})


@respx.mock
def test_empty_company_types_never_triggers_an_autocomplete_call():
    """An empty company_types tuple must be a strict no-op — never a
    wasted autocomplete round-trip, and never an empty structured
    branch."""
    _mock_linkedin_match("Healthcare", "Healthcare", "healthcare")
    _mock_no_match_everywhere_else()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"], company_types=[]))

    filters = _filters_by_call()
    assert len(filters) == 1
    assert filters[0]["linkedin_category"] == {"values": ["healthcare"]}


@respx.mock
def test_autocomplete_failure_for_a_company_type_term_degrades_to_keyword():
    """A transient autocomplete failure must degrade this company_type
    term to the keyword tier for THIS call (never abort discovery, never
    be cached as a confirmed no-match) — the exact same discipline
    industries already have."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=_one_company_response())

    provider = _provider()
    response = provider.run(_request(company_types=["Startup"]))

    assert response.success is True
    filters = _filters_by_call()
    assert filters[0]["website_keywords"] == {"values": ["Startup"], "operator": "or"}
