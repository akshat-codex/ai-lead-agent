"""Unit tests for SignalCheckProvider — a Tavily-backed COMPANY_ENRICHMENT
source that surfaces real hiring/funding signal text. See
app/providers/signal_check.py's own module docstring for the two-query
mechanism and why matched text is written into the "products_services"
evidence field.

Every HTTP call is respx-mocked — no live network calls.
"""
import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.signal_check import (
    SIGNAL_CHECK_AUTH_FAILED,
    SIGNAL_CHECK_NO_MATCH,
    SIGNAL_CHECK_RATE_LIMITED,
    SignalCheckProvider,
)

SEARCH_URL = "https://api.tavily.com/search"


def _provider() -> SignalCheckProvider:
    return SignalCheckProvider(api_key="test-key")


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_ENRICHMENT, query=query)


def _search_response(results: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": results})


@respx.mock
def test_hiring_result_is_written_into_products_services():
    respx.post(SEARCH_URL).mock(
        return_value=_search_response(
            [{"title": "Acme Corp - Marketing Manager", "url": "https://jobs.lever.co/acme/123", "content": "Acme Corp is hiring a marketing manager"}]
        )
    )

    provider = _provider()
    response = provider.run(_request(company_name="Acme Corp"))

    assert response.success is True
    record = response.data[0]
    assert "hiring a marketing manager" in record.attributes["products_services"]
    assert response.source.is_mock is False


@respx.mock
def test_funding_result_is_also_captured_alongside_hiring():
    call_count = {"n": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _search_response([])
        return _search_response([{"title": "Acme Corp raises Series A", "url": "https://news.example.com/acme", "content": "Acme Corp raised funding in a Series A round"}])

    respx.post(SEARCH_URL).mock(side_effect=_responder)

    provider = _provider()
    response = provider.run(_request(company_name="Acme Corp"))

    assert response.success is True
    assert "Series A" in response.data[0].attributes["products_services"]


@respx.mock
def test_no_results_from_either_query_is_an_honest_no_match():
    respx.post(SEARCH_URL).mock(return_value=_search_response([]))

    provider = _provider()
    response = provider.run(_request(company_name="Totally Obscure Startup"))

    assert response.success is False
    assert response.error.code == SIGNAL_CHECK_NO_MATCH
    assert response.error.retryable is False


@respx.mock
def test_no_company_name_supplied_is_an_honest_failure():
    provider = _provider()
    response = provider.run(_request())
    assert response.success is False
    assert response.error.code == SIGNAL_CHECK_NO_MATCH


@respx.mock
def test_auth_failure_is_reported_not_raised():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(401))

    provider = _provider()
    response = provider.run(_request(company_name="Acme Corp"))

    assert response.success is False
    assert response.error.code == SIGNAL_CHECK_AUTH_FAILED


@respx.mock
def test_rate_limit_is_reported_as_retryable():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(429))

    provider = _provider()
    response = provider.run(_request(company_name="Acme Corp"))

    assert response.success is False
    assert response.error.code == SIGNAL_CHECK_RATE_LIMITED
    assert response.error.retryable is True


@respx.mock
def test_one_query_failing_does_not_block_the_other_from_succeeding():
    call_count = {"n": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500)
        return _search_response([{"title": "Acme Corp raised funding", "url": "https://news.example.com/acme", "content": "Acme Corp raised funding"}])

    respx.post(SEARCH_URL).mock(side_effect=_responder)

    provider = _provider()
    response = provider.run(_request(company_name="Acme Corp"))  # must not raise

    assert response.success is True
    assert "raised funding" in response.data[0].attributes["products_services"]


@respx.mock
def test_unexpected_http_error_on_both_queries_becomes_a_clean_no_match():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500))

    provider = _provider()
    response = provider.run(_request(company_name="Acme Corp"))  # must not raise

    assert response.success is False
    assert response.error.code == SIGNAL_CHECK_NO_MATCH
