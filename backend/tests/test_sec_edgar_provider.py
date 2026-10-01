"""Unit tests for SecEdgarCompanyEnrichmentProvider — a real, free,
no-API-key COMPANY_ENRICHMENT source. See app/providers/sec_edgar.py's own
module docstring for the two-call (ticker index -> submissions) mechanism
and its honest "US public companies only" scope.

Every HTTP call is respx-mocked — no live network calls, matching this
codebase's own established provider-test convention (test_apollo_provider.py).
"""
import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.sec_edgar import SEC_EDGAR_NO_MATCH, SecEdgarCompanyEnrichmentProvider

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"

_TICKERS_BODY = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
}


def _provider() -> SecEdgarCompanyEnrichmentProvider:
    return SecEdgarCompanyEnrichmentProvider()


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_ENRICHMENT, query=query)


@respx.mock
def test_exact_name_match_maps_industry_and_country():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))
    respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "cik": "0000320193",
                "name": "Apple Inc.",
                "sic": "3571",
                "sicDescription": "Electronic Computers",
                "addresses": {"business": {"stateOrCountryDescription": "CA"}},
            },
        )
    )

    provider = _provider()
    response = provider.run(_request(company_name="Apple Inc."))

    assert response.success is True
    record = response.data[0]
    assert record.external_id == "0000320193"
    assert record.name == "Apple Inc."
    assert record.attributes == {"industry": "Electronic Computers", "country": "CA"}
    assert response.source.is_mock is False


@respx.mock
def test_name_match_is_case_insensitive_but_still_exact():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))
    respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(200, json={"name": "Apple Inc.", "sicDescription": "Electronic Computers"})
    )

    provider = _provider()
    response = provider.run(_request(company_name="apple inc."))

    assert response.success is True


@respx.mock
def test_no_match_for_a_private_company_is_an_honest_failure_not_a_guess():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))

    provider = _provider()
    response = provider.run(_request(company_name="Some Random Private Startup LLC"))

    assert response.success is False
    assert response.error.code == SEC_EDGAR_NO_MATCH
    assert response.error.retryable is False


@respx.mock
def test_substring_or_partial_name_never_matches():
    """A candidate named just "Apple" (not the exact registered name "Apple
    Inc.") must NEVER be fuzzy-matched to the real Apple Inc. — see module
    docstring's own "exact match only" rationale."""
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))

    provider = _provider()
    response = provider.run(_request(company_name="Apple"))

    assert response.success is False
    assert response.error.code == SEC_EDGAR_NO_MATCH


@respx.mock
def test_ticker_index_is_fetched_at_most_once_per_provider_instance():
    """The ~800KB ticker index must be cached, not re-downloaded on every
    enrichment call — confirmed via respx's own call-count tracking."""
    route = respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))
    respx.get(SUBMISSIONS_URL).mock(return_value=httpx.Response(200, json={"name": "Apple Inc.", "sicDescription": "Electronic Computers"}))

    provider = _provider()
    provider.run(_request(company_name="Apple Inc."))
    provider.run(_request(company_name="Apple Inc."))

    assert route.call_count == 1


@respx.mock
def test_no_company_name_supplied_is_an_honest_failure():
    provider = _provider()
    response = provider.run(_request())
    assert response.success is False
    assert response.error.code == SEC_EDGAR_NO_MATCH


@respx.mock
def test_record_with_no_usable_fields_is_reported_as_no_match_not_an_empty_success():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))
    respx.get(SUBMISSIONS_URL).mock(return_value=httpx.Response(200, json={"name": "Apple Inc."}))

    provider = _provider()
    response = provider.run(_request(company_name="Apple Inc."))

    assert response.success is False
    assert response.error.code == SEC_EDGAR_NO_MATCH


@respx.mock
def test_submissions_404_is_treated_as_no_match():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))
    respx.get(SUBMISSIONS_URL).mock(return_value=httpx.Response(404))

    provider = _provider()
    response = provider.run(_request(company_name="Apple Inc."))

    assert response.success is False
    assert response.error.code == SEC_EDGAR_NO_MATCH


@respx.mock
def test_unexpected_http_error_becomes_a_generic_provider_error_not_a_crash():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=_TICKERS_BODY))
    respx.get(SUBMISSIONS_URL).mock(return_value=httpx.Response(500))

    provider = _provider()
    response = provider.run(_request(company_name="Apple Inc."))  # must not raise

    assert response.success is False
