"""Unit tests for AbstractPhoneVerificationProvider — a real, free-tier
PERSON_ENRICHMENT source that verifies (never discovers) a phone number's
format validity, line type, and carrier. See app/providers/
abstract_phone_verification.py's own module docstring for the confirmed
request/response shape and its honest "no reachability claim" scope.

Every HTTP call is respx-mocked — no live network calls.
"""
import httpx
import respx

from app.providers.abstract_phone_verification import (
    ABSTRACT_PHONE_AUTH_FAILED,
    ABSTRACT_PHONE_NO_PHONE_SUPPLIED,
    ABSTRACT_PHONE_RATE_LIMITED,
    AbstractPhoneVerificationProvider,
)
from app.providers.contracts import ProviderCapability, ProviderRequest

VALIDATION_URL = "https://phonevalidation.abstractapi.com/v1"


def _provider() -> AbstractPhoneVerificationProvider:
    return AbstractPhoneVerificationProvider(api_key="test-key")


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.PERSON_ENRICHMENT, query=query)


@respx.mock
def test_valid_mobile_number_maps_validity_type_and_carrier():
    respx.get(VALIDATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "phone": "14152007986",
                "valid": True,
                "format": {"international": "+14152007986", "local": "(415) 200-7986"},
                "country": {"code": "US", "name": "United States", "prefix": "+1"},
                "location": "California",
                "type": "mobile",
                "carrier": "T-Mobile USA, Inc.",
            },
        )
    )

    provider = _provider()
    response = provider.run(_request(phone="+14152007986"))

    assert response.success is True
    record = response.data[0]
    assert record.attributes == {
        "phone_is_valid": True,
        "phone_line_type": "mobile",
        "phone_carrier": "T-Mobile USA, Inc.",
    }
    assert response.source.is_mock is False


@respx.mock
def test_invalid_number_still_reports_validity_false_not_a_failure():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(200, json={"phone": "123", "valid": False}))

    provider = _provider()
    response = provider.run(_request(phone="123"))

    assert response.success is True
    assert response.data[0].attributes["phone_is_valid"] is False


@respx.mock
def test_no_phone_supplied_is_an_honest_failure():
    provider = _provider()
    response = provider.run(_request())
    assert response.success is False
    assert response.error.code == ABSTRACT_PHONE_NO_PHONE_SUPPLIED
    assert response.error.retryable is False


@respx.mock
def test_response_with_no_usable_fields_is_an_honest_failure():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(200, json={"phone": "14152007986"}))

    provider = _provider()
    response = provider.run(_request(phone="+14152007986"))

    assert response.success is False
    assert response.error.code == ABSTRACT_PHONE_NO_PHONE_SUPPLIED


@respx.mock
def test_auth_failure_is_reported_not_raised():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(401))

    provider = _provider()
    response = provider.run(_request(phone="+14152007986"))

    assert response.success is False
    assert response.error.code == ABSTRACT_PHONE_AUTH_FAILED


@respx.mock
def test_rate_limit_is_reported_as_retryable():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(429))

    provider = _provider()
    response = provider.run(_request(phone="+14152007986"))

    assert response.success is False
    assert response.error.code == ABSTRACT_PHONE_RATE_LIMITED
    assert response.error.retryable is True


@respx.mock
def test_unexpected_http_error_becomes_a_generic_provider_error_not_a_crash():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(500))

    provider = _provider()
    response = provider.run(_request(phone="+14152007986"))  # must not raise

    assert response.success is False


@respx.mock
def test_no_reachability_field_is_ever_fabricated():
    """Honest-scope guard: the real, verified response shape has no
    liveness/reachability field — this adapter must never invent one even
    if a future response happened to include an unexpected extra key."""
    respx.get(VALIDATION_URL).mock(
        return_value=httpx.Response(200, json={"phone": "14152007986", "valid": True, "type": "mobile", "line_status": "active"})
    )

    provider = _provider()
    response = provider.run(_request(phone="+14152007986"))

    assert "phone_line_status" not in response.data[0].attributes
    assert "phone_reachable" not in response.data[0].attributes
