"""Export webhook — generic, file-free delivery of a completed pipeline
run's export.

Root problem this closes: this codebase's ONLY delivery mechanism was a
file the user manually downloads (GET .../export?format=...). Every
sequencing/automation tool in the 2026 market (Zapier, Make, n8n) — and
every native CRM push integration those tools themselves enable — consumes
webhooks, not files. Rather than building one native integration (a much
bigger, riskier scope: OAuth, credential storage, a new trust boundary —
see app/services/lead_export.py's own HubSpot/Salesforce CSV column work
for the file-based alternative already shipped), this module adds ONE
generic outbound webhook: when a pipeline run completes, its export JSON
(the exact same shape app/api/export.py's own render_json produces) is
POSTed to a user-configured URL. That single webhook is what unlocks the
entire downstream ecosystem — a user wires it into Zapier/Make/n8n/a
Slack-incoming-webhook/their own server, and decides what happens next,
without this codebase ever needing to know about any specific downstream
tool.

Security: every payload is HMAC-SHA256 signed (header
X-Lead-Agent-Signature, hex digest of the raw request body under
settings.export_webhook_secret) so a receiver can always verify a request
genuinely came from this app and was not forged/replayed with a modified
body. No OAuth, no stored third-party credential of any kind — the ONLY
secret involved is one this deployment's own operator generates and
controls.

NEVER blocks or fails a pipeline run over a webhook problem: send_export_webhook
never raises (mirrors app/services/homepage_fetch.py's own never-raise
discipline for exactly the same reason — one flaky/misconfigured downstream
endpoint must never be able to break the pipeline that's delivering to
it). A failed delivery is logged, never retried automatically (no
background job/queue exists in this codebase — see app/api/batch.py's own
module docstring on why Celery is unused — so retry logic would be a new
piece of infrastructure this feature does not need); a user can always
re-fetch the same export via the existing GET endpoint if a webhook
delivery was missed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx

from app.schemas.export import ExportResult

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Lead-Agent-Signature"


class WebhookDeliveryResult:
    """Plain result object (not a pydantic model — never crosses an API
    boundary or gets persisted, only consumed in-process, mirroring
    app/services/homepage_fetch.py's own HomepageFetchResult shape)."""

    __slots__ = ("attempted", "success", "status_code", "reason")

    def __init__(self, attempted: bool, success: bool, status_code: int | None = None, reason: str | None = None) -> None:
        self.attempted = attempted
        self.success = success
        self.status_code = status_code
        self.reason = reason


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def send_export_webhook(
    export_result: ExportResult,
    webhook_url: str | None,
    webhook_secret: str | None,
    timeout_seconds: float = 10.0,
) -> WebhookDeliveryResult:
    """Never raises. Returns attempted=False (not an error — the expected,
    default state) when webhook_url is unset, exactly like every other
    opt-in provider in this codebase degrades when its own enabling
    setting is absent (see app/providers/signal_check.py's own gating
    pattern). A configured URL with no secret is treated as a
    configuration error, not silently sent unsigned — signing is not
    optional once a URL is set, since an unsigned webhook gives its
    receiver no way to ever distinguish this app's real payload from
    anything else POSTed to the same URL."""
    if not webhook_url:
        return WebhookDeliveryResult(attempted=False, success=False, reason="not_configured")
    if not webhook_secret:
        logger.error("export_webhook_misconfigured url=%r reason=missing_secret", webhook_url)
        return WebhookDeliveryResult(attempted=False, success=False, reason="missing_secret")

    body = json.dumps(export_result.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, webhook_secret)

    try:
        response = httpx.post(
            webhook_url,
            content=body,
            headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        logger.warning("export_webhook_timeout url=%r error=%s", webhook_url, exc)
        return WebhookDeliveryResult(attempted=True, success=False, reason="timeout")
    except httpx.HTTPError as exc:
        logger.warning("export_webhook_error url=%r error=%s", webhook_url, exc)
        return WebhookDeliveryResult(attempted=True, success=False, reason="request_error")

    if 200 <= response.status_code < 300:
        return WebhookDeliveryResult(attempted=True, success=True, status_code=response.status_code)

    logger.warning("export_webhook_non_2xx url=%r status=%d", webhook_url, response.status_code)
    return WebhookDeliveryResult(attempted=True, success=False, status_code=response.status_code, reason="non_2xx_response")
