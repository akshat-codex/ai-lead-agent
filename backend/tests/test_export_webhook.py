"""Unit tests for app/services/export_webhook.py — the generic, file-free
export delivery mechanism. See that module's own docstring for the
signing/never-raise/no-retry-queue design.

Every HTTP call is respx-mocked — no live network calls.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import respx

from app.schemas.export import SCHEMA_VERSION, ExportMetadata, ExportResult
from app.services.export_webhook import SIGNATURE_HEADER, send_export_webhook

WEBHOOK_URL = "https://hooks.example.invalid/lead-agent"
SECRET = "test-secret"


def _export_result() -> ExportResult:
    return ExportResult(
        metadata=ExportMetadata(
            schema_version=SCHEMA_VERSION,
            icp_id="icp-1",
            icp_version=1,
            batch_id="batch-1",
            lead_count=0,
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        leads=(),
    )


def test_unset_webhook_url_is_a_clean_no_op_never_attempted():
    result = send_export_webhook(_export_result(), webhook_url=None, webhook_secret=SECRET)
    assert result.attempted is False
    assert result.success is False
    assert result.reason == "not_configured"


def test_url_configured_without_a_secret_is_a_configuration_error_not_an_unsigned_send():
    result = send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=None)
    assert result.attempted is False
    assert result.reason == "missing_secret"


@respx.mock
def test_successful_delivery_reports_success_and_status_code():
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))

    result = send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=SECRET)

    assert route.called
    assert result.attempted is True
    assert result.success is True
    assert result.status_code == 200


@respx.mock
def test_payload_body_matches_the_exportresult_json_shape():
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))

    export_result = _export_result()
    send_export_webhook(export_result, webhook_url=WEBHOOK_URL, webhook_secret=SECRET)

    sent_request = respx.calls.last.request
    body = json.loads(sent_request.content)
    assert body["metadata"]["icp_id"] == "icp-1"
    assert body["leads"] == []


@respx.mock
def test_signature_header_is_a_valid_hmac_sha256_of_the_exact_sent_body():
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))

    send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=SECRET)

    sent_request = respx.calls.last.request
    signature = sent_request.headers[SIGNATURE_HEADER]
    expected = hmac.new(SECRET.encode("utf-8"), sent_request.content, hashlib.sha256).hexdigest()
    assert signature == expected


@respx.mock
def test_different_secret_produces_a_different_signature():
    """A receiver using the WRONG secret must never validate a payload —
    confirms the signature genuinely depends on the configured secret."""
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))

    send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=SECRET)
    sent_request = respx.calls.last.request
    signature = sent_request.headers[SIGNATURE_HEADER]

    wrong_signature = hmac.new(b"wrong-secret", sent_request.content, hashlib.sha256).hexdigest()
    assert signature != wrong_signature


@respx.mock
def test_non_2xx_response_is_reported_as_a_failed_delivery_not_raised():
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))

    result = send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=SECRET)  # must not raise

    assert result.attempted is True
    assert result.success is False
    assert result.status_code == 500
    assert result.reason == "non_2xx_response"


@respx.mock
def test_network_error_never_raises_into_the_caller():
    respx.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    result = send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=SECRET)  # must not raise

    assert result.attempted is True
    assert result.success is False
    assert result.reason == "request_error"


@respx.mock
def test_timeout_never_raises_into_the_caller():
    respx.post(WEBHOOK_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    result = send_export_webhook(_export_result(), webhook_url=WEBHOOK_URL, webhook_secret=SECRET)  # must not raise

    assert result.attempted is True
    assert result.success is False
    assert result.reason == "timeout"
