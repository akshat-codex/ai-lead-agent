"""Unit tests for ApolloPersonEnrichmentProvider — the codebase's first real
(non-mock) provider adapter.

Every other provider test in this repo injects a fake ProviderAdapter
instance rather than mocking HTTP (see test_provider_registry.py), because
every other adapter is itself a mock with no real network call to make. This
file is the one place HTTP-layer mocking (via respx, built for httpx) is
introduced, since there is finally a real outbound call to intercept.
"""
import httpx
import pytest
import respx

from app.providers.apollo import APOLLO_AUTH_FAILED, APOLLO_NO_MATCH, ApolloPersonEnrichmentProvider
from app.providers.base import ProviderErrorCode
from app.providers.contracts import ProviderCapability, ProviderRequest

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"


def _provider(api_key: str = "test-key-12345") -> ApolloPersonEnrichmentProvider:
    return ApolloPersonEnrichmentProvider(api_key=api_key)


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.PERSON_ENRICHMENT, query=query)


@respx.mock
def test_successful_match_maps_documented_fields_only():
    respx.post(APOLLO_MATCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "person": {
                    "id": "apollo-person-1",
                    "name": "Jane Testperson",
                    "title": "Head of Growth",
                    "email": "jane@example-test.invalid",
                    "email_status": "verified",
                    "linkedin_url": "https://www.linkedin.com/in/janetestperson",
                    "organization_name": "Example Test Co",
                }
            },
        )
    )

    provider = _provider()
    response = provider.run(_request(full_name="Jane Testperson"))

    assert response.success is True
    assert len(response.data) == 1
    record = response.data[0]
    assert record.external_id == "apollo-person-1"
    assert record.name == "Jane Testperson"
    assert record.attributes == {
        "title": "Head of Growth",
        "email": "jane@example-test.invalid",
        "email_status": "verified",
        "linkedin_url": "https://www.linkedin.com/in/janetestperson",
        "organization_name": "Example Test Co",
    }
    assert response.source is not None
    assert response.source.is_mock is False
    assert response.source.provider_id == "apollo-person-enrichment-v1"


@respx.mock
def test_phone_is_never_requested_or_present_even_if_apollo_returns_it():
    """Apollo only delivers phone numbers asynchronously via a webhook this
    codebase has no receiver for — the adapter must never request or surface
    a phone field, even defensively if a future Apollo response includes one
    inline."""
    respx.post(APOLLO_MATCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "person": {
                    "id": "apollo-person-1",
                    "name": "Jane Testperson",
                    "contact": {"phone_numbers": [{"sanitized_number": "+15551234567"}]},
                }
            },
        )
    )

    provider = _provider()
    response = provider.run(_request(full_name="Jane Testperson"))

    assert response.success is True
    assert "phone" not in response.data[0].attributes
    request_sent = respx.calls.last.request
    assert "reveal_phone_number" not in str(request_sent.url)


@respx.mock
def test_missing_fields_are_absent_never_null_filled():
    respx.post(APOLLO_MATCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"person": {"id": "apollo-person-2", "name": "Alex Sampleuser"}},
        )
    )

    provider = _provider()
    response = provider.run(_request(full_name="Alex Sampleuser"))

    assert response.success is True
    attrs = response.data[0].attributes
    assert "email" not in attrs
    assert "title" not in attrs
    assert "linkedin_url" not in attrs
    assert None not in attrs.values()


@respx.mock
def test_no_match_returns_clean_failure_not_an_exception():
    respx.post(APOLLO_MATCH_URL).mock(return_value=httpx.Response(200, json={"person": None}))

    provider = _provider()
    response = provider.run(_request(full_name="Nobody Findable"))

    assert response.success is False
    assert response.error.code == APOLLO_NO_MATCH


@respx.mock
def test_auth_failure_returns_specific_error_code_not_generic_provider_error():
    respx.post(APOLLO_MATCH_URL).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))

    provider = _provider(api_key="wrong-key")
    response = provider.run(_request(full_name="Jane Testperson"))

    assert response.success is False
    assert response.error.code == APOLLO_AUTH_FAILED
    assert response.error.retryable is False


@respx.mock
def test_forbidden_also_maps_to_auth_failed():
    respx.post(APOLLO_MATCH_URL).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))

    provider = _provider()
    response = provider.run(_request(full_name="Jane Testperson"))

    assert response.error.code == APOLLO_AUTH_FAILED


@respx.mock
def test_server_error_is_caught_by_run_as_generic_provider_error():
    respx.post(APOLLO_MATCH_URL).mock(return_value=httpx.Response(500, text="internal error"))

    provider = _provider()
    response = provider.run(_request(full_name="Jane Testperson"))

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR


@respx.mock
def test_network_timeout_is_caught_by_run_as_generic_provider_error():
    respx.post(APOLLO_MATCH_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    provider = _provider()
    response = provider.run(_request(full_name="Jane Testperson"))

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR


@respx.mock
def test_api_key_never_appears_in_response_or_error_message():
    secret_key = "sk-super-secret-apollo-key-do-not-leak"
    respx.post(APOLLO_MATCH_URL).mock(return_value=httpx.Response(500, text="internal error"))

    provider = _provider(api_key=secret_key)
    response = provider.run(_request(full_name="Jane Testperson"))

    assert secret_key not in response.model_dump_json()
    if response.error:
        assert secret_key not in response.error.message


@respx.mock
def test_api_key_sent_as_header_not_query_param():
    respx.post(APOLLO_MATCH_URL).mock(return_value=httpx.Response(200, json={"person": {"id": "1", "name": "X"}}))

    provider = _provider(api_key="header-only-key")
    provider.run(_request(full_name="X"))

    sent_request = respx.calls.last.request
    assert sent_request.headers["x-api-key"] == "header-only-key"
    assert "header-only-key" not in str(sent_request.url)


def test_provider_declares_only_person_enrichment_capability():
    provider = _provider()
    assert provider.supports(ProviderCapability.PERSON_ENRICHMENT) is True
    assert provider.supports(ProviderCapability.PEOPLE_DISCOVERY) is False


@respx.mock
def test_query_params_map_to_apollo_field_names():
    route = respx.post(APOLLO_MATCH_URL).mock(
        return_value=httpx.Response(200, json={"person": {"id": "1", "name": "X"}})
    )

    provider = _provider()
    provider.run(
        _request(
            full_name="Jane Testperson",
            email="jane@x.invalid",
            linkedin_id="in/janetestperson",
            company_domain="x.invalid",
            company_name="X Corp",
        )
    )

    sent_params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert sent_params["name"] == "Jane Testperson"
    assert sent_params["email"] == "jane@x.invalid"
    assert sent_params["linkedin_url"] == "in/janetestperson"
    assert sent_params["domain"] == "x.invalid"
    assert sent_params["organization_name"] == "X Corp"
    assert "reveal_phone_number" not in sent_params
