"""Unit tests for AbstractEmailVerificationProvider — a real, free-tier
PERSON_ENRICHMENT source that verifies (never discovers) an email's live
deliverability. See app/providers/abstract_email_verification.py's own
module docstring for the confirmed request/response shape.

Every HTTP call is respx-mocked — no live network calls.
"""
import httpx
import respx

from app.providers.abstract_email_verification import (
    ABSTRACT_EMAIL_AUTH_FAILED,
    ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED,
    ABSTRACT_EMAIL_RATE_LIMITED,
    AbstractEmailVerificationProvider,
)
from app.providers.contracts import ProviderCapability, ProviderRequest

VALIDATION_URL = "https://emailvalidation.abstractapi.com/v1"


def _provider() -> AbstractEmailVerificationProvider:
    return AbstractEmailVerificationProvider(api_key="test-key")


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.PERSON_ENRICHMENT, query=query)


def _bool_field(value: bool) -> dict:
    return {"value": value, "text": "TRUE" if value else "FALSE"}


@respx.mock
def test_deliverable_email_maps_deliverability_and_boolean_flags():
    respx.get(VALIDATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "email": "jane@acme.com",
                "deliverability": "DELIVERABLE",
                "quality_score": 0.9,
                "is_valid_format": _bool_field(True),
                "is_free_email": _bool_field(False),
                "is_disposable_email": _bool_field(False),
                "is_role_email": _bool_field(False),
                "is_catchall_email": _bool_field(False),
                "is_mx_found": _bool_field(True),
                "is_smtp_valid": _bool_field(True),
            },
        )
    )

    provider = _provider()
    response = provider.run(_request(email="jane@acme.com"))

    assert response.success is True
    record = response.data[0]
    assert record.attributes == {
        "email_deliverability": "DELIVERABLE",
        "email_is_disposable": False,
        "email_is_role_address": False,
        "email_is_catchall": False,
    }
    assert response.source.is_mock is False


@respx.mock
def test_role_based_catchall_email_is_flagged():
    respx.get(VALIDATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "email": "info@acme.com",
                "deliverability": "RISKY",
                "is_role_email": _bool_field(True),
                "is_catchall_email": _bool_field(True),
                "is_disposable_email": _bool_field(False),
            },
        )
    )

    provider = _provider()
    response = provider.run(_request(email="info@acme.com"))

    assert response.success is True
    assert response.data[0].attributes["email_is_role_address"] is True
    assert response.data[0].attributes["email_is_catchall"] is True
    assert response.data[0].attributes["email_deliverability"] == "RISKY"


@respx.mock
def test_no_email_supplied_is_an_honest_failure():
    provider = _provider()
    response = provider.run(_request())
    assert response.success is False
    assert response.error.code == ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED
    assert response.error.retryable is False


@respx.mock
def test_response_with_no_usable_fields_is_an_honest_failure():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(200, json={"email": "jane@acme.com"}))

    provider = _provider()
    response = provider.run(_request(email="jane@acme.com"))

    assert response.success is False
    assert response.error.code == ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED


@respx.mock
def test_auth_failure_is_reported_not_raised():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(401))

    provider = _provider()
    response = provider.run(_request(email="jane@acme.com"))

    assert response.success is False
    assert response.error.code == ABSTRACT_EMAIL_AUTH_FAILED


@respx.mock
def test_rate_limit_is_reported_as_retryable():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(429))

    provider = _provider()
    response = provider.run(_request(email="jane@acme.com"))

    assert response.success is False
    assert response.error.code == ABSTRACT_EMAIL_RATE_LIMITED
    assert response.error.retryable is True


@respx.mock
def test_unexpected_http_error_becomes_a_generic_provider_error_not_a_crash():
    respx.get(VALIDATION_URL).mock(return_value=httpx.Response(500))

    provider = _provider()
    response = provider.run(_request(email="jane@acme.com"))  # must not raise

    assert response.success is False
