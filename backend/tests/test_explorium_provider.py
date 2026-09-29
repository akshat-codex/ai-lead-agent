"""Unit tests for ExploriumCompanyDiscoveryProvider, following
test_apollo_provider.py's HTTP-mocking convention (respx, built for httpx)
exactly.
"""
import httpx
import respx

from app.providers.base import ProviderErrorCode
from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import (
    EXPLORIUM_AUTH_FAILED,
    EXPLORIUM_CREDITS_EXHAUSTED,
    ExploriumCompanyDiscoveryProvider,
)

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"


def _mock_autocomplete_no_matches():
    """Every industry-branch test that doesn't care about structured-match
    behavior mocks the real autocomplete call this adapter now always makes
    for any non-empty `industries` query, returning it as "no exact match
    for anything" — the entire query lands in the keyword branch, exactly
    preserving each of these tests' original single-branch assertions."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))


def _mock_autocomplete_exact_match(query: str, label: str, value: str):
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": query}).mock(
        return_value=httpx.Response(200, json=[{"query": query, "label": label, "value": value}])
    )


def _mock_autocomplete_naics_exact_match(query: str, label: str, value: str):
    """value is a NAICS numeric code string (e.g. "541512"), per
    Explorium's documented autocomplete response shape for naics_category
    — label is still the human-readable text matched against."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": query}).mock(
        return_value=httpx.Response(200, json=[{"query": query, "label": label, "value": value}])
    )


def _mock_autocomplete_naics_no_match(query: str):
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": query}).mock(
        return_value=httpx.Response(200, json=[])
    )


def _provider(api_key: str = "test-key-12345") -> ExploriumCompanyDiscoveryProvider:
    return ExploriumCompanyDiscoveryProvider(api_key=api_key)


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query=query)


@respx.mock
def test_successful_search_maps_documented_fields_only():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                        "name": "Example Test Co",
                        "domain": "example-test.invalid",
                        "country_name": "United States",
                        "number_of_employees_range": "11-50",
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

    assert response.success is True
    assert len(response.data) == 1
    record = response.data[0]
    assert record.external_id == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    assert record.name == "Example Test Co"
    assert record.attributes == {
        "domain": "example-test.invalid",
        "country": "United States",
        "industry": "Cosmetics, Beauty Supplies, and Perfume Stores",
        "employee_range": "11-50",
        "linkedin_id": "https://www.linkedin.com/company/example-test",
        "revenue_range": "1M-10M",
    }
    assert response.source is not None
    assert response.source.is_mock is False
    assert response.source.provider_id == "explorium-company-discovery-v1"


@respx.mock
def test_employee_range_never_coerced_into_employee_count():
    """number_of_employees_range is a string range, not a precise integer —
    it must land under attributes['employee_range'], never
    attributes['employee_count'], which would misrepresent precision the
    data doesn't have."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"business_id": "b1", "name": "Example Co", "number_of_employees_range": "51-200"}]},
        )
    )

    provider = _provider()
    response = provider.run(_request(min_employees=10, max_employees=300))

    attrs = response.data[0].attributes
    assert attrs["employee_range"] == "51-200"
    assert "employee_count" not in attrs


@respx.mock
def test_missing_fields_are_absent_never_null_filled():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "b2", "name": "Sample Widgets Inc"}]})
    )

    provider = _provider()
    response = provider.run(_request())

    attrs = response.data[0].attributes
    assert "domain" not in attrs
    assert "linkedin_id" not in attrs
    assert "employee_range" not in attrs
    assert "industry" not in attrs
    assert "country" not in attrs
    assert "revenue_range" not in attrs
    assert None not in attrs.values()


@respx.mock
def test_yearly_revenue_range_mapped_without_extra_api_call():
    """Phase 7G: yearly_revenue_range is a documented Explorium response
    field already present in every real /v2/businesses response body —
    previously silently discarded by _BUSINESS_ATTRIBUTE_MAP. This must be
    mapped through using the exact same single POST /v2/businesses call
    already made for discovery; no second/extra HTTP call is introduced."""
    route = respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"business_id": "rev1", "name": "Revenue Test Co", "yearly_revenue_range": "10M-50M"}]},
        )
    )

    provider = _provider()
    response = provider.run(_request())

    assert response.data[0].attributes["revenue_range"] == "10M-50M"
    # Exactly one HTTP call total (no separate enrichment/lookup request).
    assert route.call_count == 1


@respx.mock
def test_no_linkedin_profile_is_never_fabricated():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "b3", "name": "No LinkedIn Co", "domain": "no-li.invalid"}]})
    )

    provider = _provider()
    response = provider.run(_request())

    assert "linkedin_id" not in response.data[0].attributes


@respx.mock
def test_business_with_no_name_is_skipped_not_fabricated():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"business_id": "b4", "domain": "no-name.invalid"},
                    {"business_id": "b5", "name": "Valid Co", "domain": "valid.invalid"},
                ]
            },
        )
    )

    provider = _provider()
    response = provider.run(_request())

    assert len(response.data) == 1
    assert response.data[0].name == "Valid Co"


@respx.mock
def test_empty_results_returns_success_with_no_candidates():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    response = provider.run(_request())

    assert response.success is True
    assert response.data == ()


@respx.mock
def test_auth_failure_returns_specific_error_code_not_generic_provider_error():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))

    provider = _provider(api_key="wrong-key")
    response = provider.run(_request())

    assert response.success is False
    assert response.error.code == EXPLORIUM_AUTH_FAILED
    assert response.error.retryable is False


@respx.mock
def test_forbidden_also_maps_to_auth_failed():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))

    provider = _provider()
    response = provider.run(_request())

    assert response.error.code == EXPLORIUM_AUTH_FAILED


@respx.mock
def test_server_error_is_caught_by_run_as_generic_provider_error():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(500, text="internal error"))

    provider = _provider()
    response = provider.run(_request())

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR


@respx.mock
def test_network_timeout_is_caught_by_run_as_generic_provider_error():
    respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    provider = _provider()
    response = provider.run(_request())

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR


@respx.mock
def test_api_key_never_appears_in_response_or_error_message():
    secret_key = "sk-super-secret-explorium-key-do-not-leak"
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(500, text="internal error"))

    provider = _provider(api_key=secret_key)
    response = provider.run(_request())

    assert secret_key not in response.model_dump_json()
    if response.error:
        assert secret_key not in response.error.message


@respx.mock
def test_api_key_sent_as_header_not_query_param_or_body():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider(api_key="header-only-key")
    provider.run(_request())

    sent_request = respx.calls.last.request
    assert sent_request.headers["API_KEY"] == "header-only-key"
    assert "header-only-key" not in str(sent_request.url)
    assert b"header-only-key" not in sent_request.content


def test_provider_declares_only_company_discovery_capability():
    provider = _provider()
    assert provider.supports(ProviderCapability.COMPANY_DISCOVERY) is True
    assert provider.supports(ProviderCapability.COMPANY_ENRICHMENT) is False
    assert provider.supports(ProviderCapability.PEOPLE_DISCOVERY) is False
    assert provider.supports(ProviderCapability.PERSON_ENRICHMENT) is False


@respx.mock
def test_geography_filter_maps_to_lowercase_country_codes():
    route = respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(geography_codes=["US", "CA"]))

    sent_body = respx.calls.last.request.content
    import json as _json

    parsed = _json.loads(sent_body)
    assert parsed["filters"]["country_code"] == {"values": ["us", "ca"]}
    assert route.called


@respx.mock
def test_employee_range_filter_forwards_all_overlapping_buckets():
    """Phase 7G: any bucket that OVERLAPS [min, max] is forwarded, not only
    buckets fully contained within it — the fully-contained-only rule
    silently dropped the whole filter for many real ranges (see the
    15-150 and 20-300 tests below). For [10, 300]: 1-10 overlaps at the
    single point 10; 11-50, 51-200, 201-500 all overlap; 501-1000 does
    not (501 > 300)."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(min_employees=10, max_employees=300))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert parsed["filters"]["company_size"] == {"values": ["1-10", "11-50", "51-200", "201-500"]}


@respx.mock
def test_employee_range_15_150_never_silently_drops_filter():
    """The exact bug confirmed in the Phase 7F live Deep Tech test:
    _build_employee_size_filter(15, 150) previously returned None because
    no documented bucket is FULLY CONTAINED within [15, 150] (11-50 fails
    since 11 < 15; 51-200 fails since 200 > 150) — so no company_size
    filter was sent at all, and companies of any size (including
    10001+, e.g. IBM/Meta/Deloitte) passed through discovery
    unconstrained. Now 11-50 and 51-200 both overlap [15, 150] and are
    forwarded; 1-10 and 201-500 do not overlap and are excluded."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(min_employees=15, max_employees=150))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "company_size" in parsed["filters"], "employee filter must never be silently dropped"
    assert parsed["filters"]["company_size"] == {"values": ["11-50", "51-200"]}


@respx.mock
def test_employee_range_20_300_never_silently_drops_filter():
    """A second arbitrary, non-bucket-aligned range, confirming the fix
    generalizes rather than being special-cased to 15-150. 11-50, 51-200,
    201-500 all overlap [20, 300]; 1-10 does not (10 < 20)."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(min_employees=20, max_employees=300))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "company_size" in parsed["filters"], "employee filter must never be silently dropped"
    assert parsed["filters"]["company_size"] == {"values": ["11-50", "51-200", "201-500"]}


@respx.mock
def test_employee_range_bucket_aligned_still_works():
    """A range that exactly matches Explorium's own documented bucket
    boundaries (51-200) must still resolve to exactly that one bucket,
    confirming the overlap-based rewrite didn't regress the simple case
    the original fully-contained rule already handled correctly."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(min_employees=51, max_employees=200))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert parsed["filters"]["company_size"] == {"values": ["51-200"]}


@respx.mock
def test_employee_range_invalid_inverted_bounds_sends_no_filter():
    """min > max is not a real range Explorium has any honest bucket
    selection for — this stays "no filter sent" (the one case where
    omitting the filter is correct, since there is no valid overlap
    logic for an inverted range), rather than raising or guessing."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(min_employees=500, max_employees=10))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "filters" not in parsed or "company_size" not in parsed.get("filters", {})


@respx.mock
def test_no_employee_bounds_omits_company_size_filter():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request())

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "filters" not in parsed or "company_size" not in parsed.get("filters", {})


@respx.mock
def test_industries_with_no_structured_match_are_forwarded_as_website_keywords():
    """website_keywords is a genuine free-text filter — an ICP industry
    term with no real, exact linkedin_category match is forwarded directly
    and honestly via website_keywords, without inventing any category
    mapping."""
    _mock_autocomplete_no_matches()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Skincare", "Beauty"]))

    import json as _json

    business_calls = [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]
    assert len(business_calls) == 1
    parsed = _json.loads(business_calls[0].request.content)
    filters = parsed.get("filters", {})
    assert filters["website_keywords"] == {"values": ["Skincare", "Beauty"], "operator": "or"}
    assert "linkedin_category" not in filters


@respx.mock
def test_industries_never_guess_a_taxonomy_category_mapping():
    """No EXACT match was found for either term in EITHER taxonomy —
    narrowing by an invented/guessed near-miss value (from linkedin_
    category OR naics_category) would silently under-return real matches,
    so neither structured filter (nor google_category, never used at all)
    must appear, even though both autocomplete endpoints returned some
    unrelated near-miss suggestions for one of the two terms."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Skincare"}).mock(
        return_value=httpx.Response(
            200, json=[{"query": "Skincare", "label": "Health and beauty", "value": "health and beauty"}]
        )
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Beauty"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    # Both terms also get a near-miss NAICS suggestion — a non-exact label
    # match must be rejected at this tier exactly as it already is for
    # linkedin_category, never silently accepted as "close enough".
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Skincare"}).mock(
        return_value=httpx.Response(
            200, json=[{"query": "Skincare", "label": "Cosmetics, Beauty Supplies, and Perfume Stores", "value": "446120"}]
        )
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Beauty"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Skincare", "Beauty"]))

    import json as _json

    business_calls = [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]
    assert len(business_calls) == 1
    parsed = _json.loads(business_calls[0].request.content)
    filters = parsed.get("filters", {})
    assert "google_category" not in filters
    assert "naics_category" not in filters
    assert "linkedin_category" not in filters
    assert filters["website_keywords"] == {"values": ["Skincare", "Beauty"], "operator": "or"}


@respx.mock
def test_no_industries_omits_website_keywords_filter():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request())

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "website_keywords" not in parsed.get("filters", {})


@respx.mock
def test_company_types_alone_are_sent_as_website_keywords():
    """Phase 7N: CompanyDiscoveryQuery.company_types (populated end-to-end
    from the frontend's real 'Company type' field, e.g. 'D2C, PE-backed,
    Startup') previously reached this adapter's request.query and was
    silently never read at all. With no industries stated, company_types
    terms alone must still produce a real website_keywords filter — never
    an empty/missing filter that silently drops the user's requirement.

    Phase 41: company_types terms now attempt the same structured
    autocomplete industries already get — this test mocks "no exact
    match anywhere" for both terms so it still exercises the pure
    keyword-fallback outcome its own docstring describes."""
    _mock_autocomplete_no_matches()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(company_types=["D2C", "PE-backed"]))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert parsed["filters"]["website_keywords"] == {"values": ["D2C", "PE-backed"], "operator": "or"}
    assert "linkedin_category" not in parsed["filters"]


@respx.mock
def test_company_types_merge_into_keyword_branch_alongside_unmatched_industries():
    """company_types terms join the SAME keyword branch as unmatched
    industry terms when they don't resolve structurally themselves —
    never a separate call for an unresolved term, never dropped.

    Phase 41: "PE-backed" is mocked as no-match on both taxonomies here
    so it still falls to keyword, exercising this test's original intent
    unchanged."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "PE-backed"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("PE-backed")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Widgetology"], company_types=["PE-backed"]))

    calls = _business_calls()
    assert len(calls) == 2  # still exactly one structured + one keyword call, never 3
    import json as _json

    all_filters = [_json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert "PE-backed" not in structured.get("website_keywords", {}).get("values", [])
    assert sorted(keyword["website_keywords"]["values"]) == sorted(["Widgetology", "PE-backed"])
    assert keyword["website_keywords"]["operator"] == "or"


@respx.mock
def test_company_types_when_all_industries_are_structured_still_create_keyword_branch():
    """Every industry term matches structurally (no keyword_terms at all),
    but company_types is still present — a keyword branch must still be
    created for company_types alone, not silently skipped just because
    the industries side had nothing left over for it.

    Phase 41: "Startup" is mocked as no-match on both taxonomies so it
    still falls to keyword, exercising this test's original intent."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Startup"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Startup")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"], company_types=["Startup"]))

    calls = _business_calls()
    assert len(calls) == 2
    import json as _json

    all_filters = [_json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert keyword["website_keywords"] == {"values": ["Startup"], "operator": "or"}


@respx.mock
def test_duplicate_term_between_industries_and_company_types_not_repeated():
    """The same literal term appearing in both an unmatched industry and
    company_types must appear only once in the merged OR-list, never
    duplicated."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Startup"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Startup")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Startup"], company_types=["Startup"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"] == {"values": ["Startup"], "operator": "or"}


@respx.mock
def test_neither_industries_nor_company_types_omits_website_keywords_filter():
    """Regression guard for the pre-existing no-industries case, confirming
    the new company_types handling didn't change this path: with neither
    field populated, no website_keywords filter is sent at all (not an
    empty one)."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(company_types=[]))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "website_keywords" not in parsed.get("filters", {})


@respx.mock
def test_mode_is_always_full_to_maximize_documented_field_availability():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request())

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert parsed["mode"] == "full"


# --- pagination / cursor -------------------------------------------------


@respx.mock
def test_cursor_is_sent_as_top_level_next_cursor_field():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    request = ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query={}, cursor="abc123")
    provider.run(request)

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert parsed["next_cursor"] == "abc123"
    assert "page" not in parsed


@respx.mock
def test_no_cursor_omits_next_cursor_field():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request())

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert "next_cursor" not in parsed


@respx.mock
def test_response_cursor_is_read_from_page_next_cursor():
    """The outer cursor contract stays opaque (never a caller-interpreted
    shape) — with no industries there is exactly one branch (keyword), and
    its real Explorium next_cursor is packed into the single-branch state
    object this adapter always returns, per the Phase 7E multi-branch
    cursor design (see the module docstring)."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "total_results": 5, "page": {"size": 5, "next_cursor": "xyz789"}},
        )
    )

    provider = _provider()
    response = provider.run(_request())

    import json as _json

    assert _json.loads(response.cursor) == {"keyword": "xyz789"}
    assert response.exhausted is False


@respx.mock
def test_null_page_envelope_marks_exhausted():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [], "total_results": None, "page": None})
    )

    provider = _provider()
    response = provider.run(_request())

    assert response.exhausted is True
    assert response.cursor is None


@respx.mock
def test_last_page_with_results_but_no_next_cursor_is_exhausted():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"business_id": "b1", "name": "Last Page Co"}],
                "total_results": 1,
                "page": {"size": 1},
            },
        )
    )

    provider = _provider()
    response = provider.run(_request())

    assert len(response.data) == 1
    assert response.cursor is None
    assert response.exhausted is True


@respx.mock
def test_page_size_capped_at_100_not_500():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(limit=500))

    import json as _json

    parsed = _json.loads(respx.calls.last.request.content)
    assert parsed["page_size"] == 100


# --- 403 credits-exhausted vs auth-failed split ---------------------------


@respx.mock
def test_403_with_credits_body_maps_to_credits_exhausted_not_auth_failed():
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            403,
            json={"details": "You have insufficient credits to perform this operation. Please purchase additional credits."},
        )
    )

    provider = _provider()
    response = provider.run(_request())

    assert response.success is False
    assert response.error.code == EXPLORIUM_CREDITS_EXHAUSTED
    assert response.error.retryable is False


@respx.mock
def test_403_without_credits_body_still_maps_to_auth_failed():
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))

    provider = _provider()
    response = provider.run(_request())

    assert response.error.code == EXPLORIUM_AUTH_FAILED


# --- Phase 7E: per-industry discovery branching ---------------------------


def _business_calls():
    return [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]


@respx.mock
def test_healthcare_only_icp_uses_the_structured_branch():
    """A single ICP industry term with a real, exact linkedin_category
    match uses the structured branch alone — no website_keywords call at
    all, exactly the "genuinely relevant, not merely keyword-related"
    behavior this feature exists for."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["linkedin_category"] == {"values": ["healthcare"]}
    assert "website_keywords" not in filters


@respx.mock
def test_healthcare_saas_compound_icp_produces_two_independent_or_branches():
    """Phase 25 root-cause capture: the EXACT Explorium payload shape
    produced for a compound "Healthcare SaaS" ICP. "Healthcare" exact-
    matches linkedin_category; "SaaS" has no exact match anywhere (matches
    the real, live-observed Phase 24 behavior) and falls to the keyword
    branch. This confirms, at the wire-payload level, that the two terms
    become TWO SEPARATE, INDEPENDENT search requests whose results are
    later merged (OR semantics) — never a single request requiring both
    (which Explorium's filters-AND-together-within-one-call behavior would
    make impossible to express as "Healthcare AND SaaS" in the first
    place; see app/providers/explorium.py's own "or, not and" comment).
    This discovery-level OR is intentional/unchanged by Phase 25 — the fix
    is downstream, in app/services/hard_icp_validation.py's bridge no
    longer treating a single satisfied branch as proof of the whole
    compound requirement (see tests/test_discovery_qualification_bridge.py)."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "SaaS"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("SaaS")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "SaaS"]))

    calls = _business_calls()
    assert len(calls) == 2  # two independent branch requests, never one combined request

    import json as _json

    bodies = [_json.loads(c.request.content) for c in calls]
    structured_bodies = [b for b in bodies if "linkedin_category" in b.get("filters", {})]
    keyword_bodies = [b for b in bodies if "website_keywords" in b.get("filters", {})]
    assert len(structured_bodies) == 1
    assert len(keyword_bodies) == 1

    # Branch 1: structured, Healthcare ONLY — no trace of "SaaS" in this
    # request's filters at all.
    structured_filters = structured_bodies[0]["filters"]
    assert structured_filters["linkedin_category"] == {"values": ["healthcare"]}
    assert "website_keywords" not in structured_filters
    assert "naics_category" not in structured_filters

    # Branch 2: keyword, SaaS ONLY, "or" operator (an OR-list of ONE term
    # here, but the operator itself is what proves this branch can never
    # express "must ALSO be Healthcare" — it is a pure substring search).
    keyword_filters = keyword_bodies[0]["filters"]
    assert keyword_filters["website_keywords"] == {"values": ["SaaS"], "operator": "or"}
    assert "linkedin_category" not in keyword_filters

    # Neither request's filters reference the OTHER industry term at all —
    # this is the wire-level proof that Explorium is never asked, and
    # structurally cannot be asked in one call, for "Healthcare AND SaaS."
    assert "SaaS" not in _json.dumps(structured_filters)
    assert "Healthcare" not in _json.dumps(keyword_filters)


@respx.mock
def test_mixed_icp_uses_structured_and_keyword_branches():
    """Healthcare (exact match) and Widgetology (no match) together: one
    structured-branch call for Healthcare, one keyword-branch call for
    Widgetology only — never merged into a single request (Explorium's
    filters AND together, confirmed live, so mixing would wrongly exclude
    real matches for one side or the other)."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Widgetology"]))

    calls = _business_calls()
    assert len(calls) == 2
    import json as _json

    all_filters = [_json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert "website_keywords" not in structured
    assert keyword["website_keywords"] == {"values": ["Widgetology"], "operator": "or"}
    assert "linkedin_category" not in keyword


@respx.mock
def test_unmapped_terms_stay_in_keyword_fallback_when_nothing_matches():
    """No ICP term has a real structured match in EITHER taxonomy —
    behaves exactly like the pre-Phase-7E adapter: one call,
    website_keywords only, real evidence that the fallback is honest and
    unconditional, not a special case."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C Brands"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Microdrama Companies"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("D2C Brands")
    _mock_autocomplete_naics_no_match("Microdrama Companies")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C Brands", "Microdrama Companies"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"] == {"values": ["D2C Brands", "Microdrama Companies"], "operator": "or"}
    assert "linkedin_category" not in filters
    assert "naics_category" not in filters


@respx.mock
def test_autocomplete_failure_degrades_to_keyword_fallback_never_aborts_discovery():
    """An autocomplete outage (network error, non-200, malformed body) must
    never abort discovery — it degrades to the honest website_keywords
    fallback for that term, same as "no match found"."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))

    assert response.success is True
    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"] == {"values": ["Healthcare"], "operator": "or"}


@respx.mock
def test_each_active_branch_requests_up_to_the_full_limit_not_a_split():
    """Phase 40: replaces the original even-split-across-branches scheme
    (each branch got at most limit // branch_count) — that proportionally
    starved every branch as branch count grew, directly undercutting
    Phase 2's compound-ICP corroboration fix (a candidate needs a genuine
    hit from more than one branch to corroborate; a starved per-branch
    page size makes that less likely, not more). Each of the 2 active
    branches here must request the FULL limit directly — never a split —
    still bounded per-branch by Explorium's own page_size ceiling."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Widgetology"], limit=20))

    calls = _business_calls()
    assert len(calls) == 2
    import json as _json

    page_sizes = [_json.loads(c.request.content)["page_size"] for c in calls]
    assert page_sizes == [20, 20]  # each branch gets the FULL limit, never a fraction of it


@respx.mock
def test_per_branch_page_size_still_capped_at_explorium_ceiling():
    """Even with no division across branches, a per-branch request must
    never exceed Explorium's real, live-verified page_size ceiling (100) —
    the bound comes from min(limit, 100) applied before branch fan-out,
    unchanged by Phase 40's removal of the even split."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Widgetology"], limit=500))

    calls = _business_calls()
    assert len(calls) == 2
    import json as _json

    page_sizes = [_json.loads(c.request.content)["page_size"] for c in calls]
    assert page_sizes == [100, 100]


@respx.mock
def test_three_active_branches_each_still_get_the_full_limit():
    """A genuinely 3-branch round (linkedin_category + naics_category +
    website_keywords, all active simultaneously) — before Phase 40 this
    would have given each branch only limit // 3, proportionally starving
    every branch further as branch count grows. Each of the 3 branches
    here must still request the FULL limit, confirming the fix scales
    beyond the 2-branch case."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    _mock_autocomplete_naics_exact_match("Fintech", "Fintech", "522320")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Fintech"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("D2C")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "Fintech", "D2C"], limit=30))

    calls = _business_calls()
    assert len(calls) == 3  # linkedin_category, naics_category, website_keywords all active
    import json as _json

    page_sizes = [_json.loads(c.request.content)["page_size"] for c in calls]
    assert page_sizes == [30, 30, 30]  # every branch gets the full limit, none starved by branch count


@respx.mock
def test_no_duplicate_candidates_between_branches_same_business_id():
    """The same real company can legitimately satisfy both branches (e.g.
    a healthcare company whose site also mentions an unmapped term) —
    merged results must not contain it twice."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")

    call_count = {"n": 0}

    def _responder(request):
        call_count["n"] += 1
        return httpx.Response(200, json={"data": [{"business_id": "dup-1", "name": "Shared Co"}]})

    respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_responder)

    provider = _provider()
    response = provider.run(_request(industries=["Healthcare", "Widgetology"]))

    assert call_count["n"] == 2  # both branches really were called
    assert len(response.data) == 1
    assert response.data[0].external_id == "dup-1"


@respx.mock
def test_cursor_pagination_continues_correctly_across_both_branches():
    """Round 1: both branches return a next_cursor. Round 2 (passing round
    1's ProviderResponse.cursor back in): each branch must receive its OWN
    cursor back, never the other branch's, and never restart from page 1."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")

    def _responder(request):
        import json as _json

        body = _json.loads(request.content)
        filters = body.get("filters", {})
        incoming_cursor = body.get("next_cursor")
        if "linkedin_category" in filters:
            next_cursor = None if incoming_cursor == "structured-page-2" else "structured-page-2"
        else:
            next_cursor = None if incoming_cursor == "keyword-page-2" else "keyword-page-2"
        page = None if next_cursor is None else {"size": 1, "next_cursor": next_cursor}
        return httpx.Response(200, json={"data": [], "total_results": 1, "page": page})

    respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_responder)

    provider = _provider()
    round1 = provider.run(_request(industries=["Healthcare", "Widgetology"]))
    assert round1.exhausted is False
    assert round1.cursor is not None

    round2_request = ProviderRequest(
        capability=ProviderCapability.COMPANY_DISCOVERY,
        query={"industries": ["Healthcare", "Widgetology"]},
        cursor=round1.cursor,
    )
    round2 = provider.run(round2_request)

    import json as _json

    round1_state = _json.loads(round1.cursor)
    assert round1_state["structured"] == "structured-page-2"
    assert round1_state["keyword"] == "keyword-page-2"
    # Round 2: both branches now report exhausted (per the fake responder's
    # logic above) -> the whole round is exhausted, no further cursor.
    assert round2.exhausted is True
    assert round2.cursor is None


@respx.mock
def test_exhausted_branch_is_never_queried_again_on_a_later_round():
    """Once a branch is confirmed exhausted (via the sentinel packed into
    the cursor), a later round must not call it again — the per-branch
    analogue of the existing "an exhausted provider is never asked again"
    rule."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")

    def _responder(request):
        import json as _json

        body = _json.loads(request.content)
        filters = body.get("filters", {})
        if "linkedin_category" in filters:
            # structured branch is exhausted immediately
            return httpx.Response(200, json={"data": [], "total_results": 0, "page": None})
        return httpx.Response(
            200, json={"data": [], "total_results": 1, "page": {"size": 1, "next_cursor": "keyword-page-2"}}
        )

    respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_responder)

    provider = _provider()
    round1 = provider.run(_request(industries=["Healthcare", "Widgetology"]))
    assert len(_business_calls()) == 2  # both branches queried on round 1

    round2_request = ProviderRequest(
        capability=ProviderCapability.COMPANY_DISCOVERY,
        query={"industries": ["Healthcare", "Widgetology"]},
        cursor=round1.cursor,
    )
    provider.run(round2_request)

    # Round 2 must only have queried the still-active keyword branch — 3
    # total business calls (2 from round 1, 1 from round 2), never 4.
    assert len(_business_calls()) == 3


@respx.mock
def test_autocomplete_result_is_cached_never_repeated_for_the_same_term():
    """The autocomplete lookup for a given ICP term must happen at most
    once per process lifetime (this provider instance) — a second round for
    the same ICP must not repeat it."""
    autocomplete_route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))
    provider.run(_request(industries=["Healthcare"]))

    assert autocomplete_route.call_count == 1


@respx.mock
def test_generic_branching_works_identically_across_unrelated_icp_domains():
    """Phase 7M universality check: the SAME _plan_industry_branches logic,
    with zero ICP-name-specific code anywhere, must correctly split a
    completely different industry vocabulary (FinTech / SaaS / a made-up
    unmapped term) exactly the way it already does for Healthcare —
    proving the mechanism generalizes rather than having been tuned for
    any one ICP shape. FinTech and SaaS both get real (fabricated-for-this-
    test) exact autocomplete matches; 'Underwater Basket Weaving Tech' has
    none."""
    _mock_autocomplete_exact_match("FinTech", "FinTech", "fintech")
    _mock_autocomplete_exact_match("SaaS", "SaaS", "saas")
    respx.get(
        EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Underwater Basket Weaving Tech"}
    ).mock(return_value=httpx.Response(200, json=[]))
    _mock_autocomplete_naics_no_match("Underwater Basket Weaving Tech")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["FinTech", "SaaS", "Underwater Basket Weaving Tech"]))

    calls = _business_calls()
    assert len(calls) == 2  # one structured (FinTech+SaaS merged), one keyword
    import json as _json

    all_filters = [_json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert sorted(structured["linkedin_category"]["values"]) == ["fintech", "saas"]
    assert keyword["website_keywords"] == {"values": ["Underwater Basket Weaving Tech"], "operator": "or"}


@respx.mock
def test_generic_branching_all_structured_for_a_different_icp_shape():
    """A second, unrelated ICP shape (Semiconductor-only) where every term
    happens to match — must behave exactly like the existing Healthcare-only
    test, confirming there is no per-domain special case."""
    _mock_autocomplete_exact_match("Semiconductors", "Semiconductors", "semiconductors")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Semiconductors"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["linkedin_category"] == {"values": ["semiconductors"]}
    assert "website_keywords" not in filters


# --- Phase 8B: naics_category as a second structured tier -----------------


@respx.mock
def test_linkedin_exact_match_uses_structured_branch_naics_never_called():
    """A term with an exact linkedin_category match resolves at tier 1 —
    naics_category must never even be queried for it, exactly preserving
    Phase 7E's existing single-branch, single-autocomplete-call behavior
    for terms that already match at the first tier."""
    autocomplete_route = respx.get(
        EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}
    ).mock(return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]))
    naics_route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare"]))

    assert autocomplete_route.call_count == 1
    assert naics_route.call_count == 0  # tier 1 matched -> tier 2 never queried
    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["linkedin_category"] == {"values": ["healthcare"]}
    assert "naics_category" not in filters


@respx.mock
def test_linkedin_miss_naics_exact_match_uses_naics_structured_branch():
    """The exact Phase 8 finding: a term with NO linkedin_category match
    (e.g. "D2C") but a real, exact naics_category match must resolve into
    a naics_category structured branch, never falling straight to
    website_keywords."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_exact_match("D2C", "D2C", "454110")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["naics_category"] == {"values": ["454110"]}
    assert "linkedin_category" not in filters
    assert "website_keywords" not in filters


@respx.mock
def test_both_taxonomies_miss_falls_back_to_website_keywords():
    """Neither linkedin_category nor naics_category has an exact match —
    the term falls to website_keywords, exactly the pre-Phase-8B fallback
    behavior, never a guessed structured mapping from either taxonomy."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "FMCG"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("FMCG")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["FMCG"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"] == {"values": ["FMCG"], "operator": "or"}
    assert "linkedin_category" not in filters
    assert "naics_category" not in filters


@respx.mock
def test_mixed_icp_each_term_resolves_independently_across_all_three_tiers():
    """A single ICP with one term per tier — Healthcare (linkedin match),
    D2C (naics match), Widgetology (both miss) — each resolves
    independently into its own branch: 3 separate calls, never merged,
    each term landing in exactly the branch its own resolution earned."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    _mock_autocomplete_naics_no_match("Healthcare")  # never reached (tier 1 matched) but harmless if it were
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_exact_match("D2C", "D2C", "454110")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("Widgetology")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "D2C", "Widgetology"]))

    calls = _business_calls()
    assert len(calls) == 3
    import json as _json

    all_filters = [_json.loads(c.request.content)["filters"] for c in calls]
    linkedin_branch = next(f for f in all_filters if "linkedin_category" in f)
    naics_branch = next(f for f in all_filters if "naics_category" in f)
    keyword_branch = next(f for f in all_filters if "website_keywords" in f)
    assert linkedin_branch["linkedin_category"] == {"values": ["healthcare"]}
    assert naics_branch["naics_category"] == {"values": ["454110"]}
    assert keyword_branch["website_keywords"] == {"values": ["Widgetology"], "operator": "or"}
    # No branch carries another branch's filter key — never AND-merged.
    assert "naics_category" not in linkedin_branch and "website_keywords" not in linkedin_branch
    assert "linkedin_category" not in naics_branch and "website_keywords" not in naics_branch
    assert "linkedin_category" not in keyword_branch and "naics_category" not in keyword_branch


@respx.mock
def test_naics_branch_never_and_merges_with_linkedin_branch():
    """Two terms, one matching each structured taxonomy — must be two
    SEPARATE requests, never one request carrying both linkedin_category
    AND naics_category filters together (which would wrongly require a
    company to satisfy both simultaneously — Explorium's filters AND
    together within one request, confirmed live in Phase 7E)."""
    _mock_autocomplete_exact_match("Healthcare", "Healthcare", "healthcare")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_exact_match("D2C", "D2C", "454110")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Healthcare", "D2C"]))

    calls = _business_calls()
    assert len(calls) == 2
    import json as _json

    for call in calls:
        filters = _json.loads(call.request.content)["filters"]
        assert not ("linkedin_category" in filters and "naics_category" in filters)


@respx.mock
def test_naics_values_or_together_not_and():
    """Multiple ICP terms that both resolve to naics_category must OR
    together within that one branch (alternatives, not a simultaneous
    requirement) — the same semantics linkedin_category values already
    have, confirmed via the same {"values": [...]} shape (Explorium's OR
    list, not a nested AND structure)."""
    _mock_autocomplete_naics_exact_match("D2C", "D2C", "454110")
    _mock_autocomplete_naics_exact_match("FMCG", "FMCG", "424410")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "FMCG"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C", "FMCG"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert sorted(filters["naics_category"]["values"]) == sorted(["424410", "454110"])


@respx.mock
def test_naics_autocomplete_calls_remain_bounded_one_per_term():
    """Exactly one naics_category autocomplete call per term that reaches
    tier 2 (i.e. missed linkedin_category) — never more, regardless of ICP
    size. A term that matches at tier 1 never triggers a tier-2 call at
    all (see test_linkedin_exact_match_uses_structured_branch_naics_never_called)."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "FMCG"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    naics_d2c = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[{"query": "D2C", "label": "D2C", "value": "454110"}])
    )
    naics_fmcg = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "FMCG"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C", "FMCG"]))

    assert naics_d2c.call_count == 1
    assert naics_fmcg.call_count == 1


@respx.mock
def test_naics_match_is_cached_never_repeated_for_the_same_term():
    """The naics_category lookup for a given term must happen at most once
    per process lifetime (this provider instance) — a second round for the
    same ICP must not repeat it, mirroring the existing linkedin_category
    caching guarantee."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    naics_route = respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[{"query": "D2C", "label": "D2C", "value": "454110"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": [], "page": None}))

    provider = _provider()
    provider.run(_request(industries=["D2C"]))
    provider.run(_request(industries=["D2C"]))

    assert naics_route.call_count == 1


@respx.mock
def test_naics_autocomplete_failure_degrades_to_keyword_fallback():
    """A naics_category autocomplete failure (timeout, non-200, malformed
    body) must degrade to the keyword fallback for that term, never abort
    discovery — the same resilience guarantee linkedin_category's
    autocomplete already has."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "D2C"}).mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    response = provider.run(_request(industries=["D2C"]))

    assert response.success is True
    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"] == {"values": ["D2C"], "operator": "or"}


@respx.mock
def test_naics_never_fuzzy_matches_a_near_miss_label():
    """A naics_category suggestion whose label is close but not an exact
    match (e.g. "Direct Selling Establishments" for the query "D2C") must
    be rejected — never accepted as a guessed near-miss, mirroring the
    existing linkedin_category exact-match discipline."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "D2C"}).mock(
        return_value=httpx.Response(
            200, json=[{"query": "D2C", "label": "Direct Selling Establishments", "value": "454390"}]
        )
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert "naics_category" not in filters
    assert filters["website_keywords"] == {"values": ["D2C"], "operator": "or"}


@respx.mock
def test_generic_naics_resolution_works_for_unrelated_icp_vocabulary():
    """A completely different, unrelated ICP vocabulary (FinTech regulatory
    terms, not D2C/FMCG/Healthcare) resolves through the SAME generic
    3-tier mechanism — confirming this is not tuned to any one ICP shape."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "RegTech"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_exact_match("RegTech", "RegTech", "541611")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["RegTech"]))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["naics_category"] == {"values": ["541611"]}


@respx.mock
def test_employee_range_filter_unchanged_by_naics_branch_addition():
    """Regression guard: the employee_range/company_size filter logic must
    be completely untouched by the NAICS branch addition — a NAICS branch
    still receives the same base_filters (country_code/company_size) as
    every other branch."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_exact_match("D2C", "D2C", "454110")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C"], min_employees=15, max_employees=150))

    calls = _business_calls()
    assert len(calls) == 1
    import json as _json

    filters = _json.loads(calls[0].request.content)["filters"]
    assert filters["naics_category"] == {"values": ["454110"]}
    assert filters["company_size"] == {"values": ["11-50", "51-200"]}


@respx.mock
def test_company_type_still_falls_to_keyword_when_it_has_no_structured_match():
    """Regression guard, updated for Phase 41: a company_type term that
    genuinely has NO exact structured match anywhere (here, "PE-backed")
    still falls to the keyword branch's OR-list, exactly as it always
    has — Phase 41 only adds a structured ATTEMPT, it never forces a
    company_type term into a structured branch it doesn't genuinely,
    exactly resolve into. An unrelated INDUSTRY term ("D2C") resolving to
    its own naics_category branch here is completely unaffected."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "D2C"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_exact_match("D2C", "D2C", "454110")
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "PE-backed"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    _mock_autocomplete_naics_no_match("PE-backed")
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C"], company_types=["PE-backed"]))

    calls = _business_calls()
    assert len(calls) == 2  # industry naics branch + keyword branch (company_types alone)
    import json as _json

    all_filters = [_json.loads(c.request.content)["filters"] for c in calls]
    naics_branch = next(f for f in all_filters if "naics_category" in f)
    keyword_branch = next(f for f in all_filters if "website_keywords" in f)
    assert naics_branch["naics_category"] == {"values": ["454110"]}
    assert "PE-backed" not in naics_branch.get("website_keywords", {}).get("values", [])
    assert keyword_branch["website_keywords"] == {"values": ["PE-backed"], "operator": "or"}
