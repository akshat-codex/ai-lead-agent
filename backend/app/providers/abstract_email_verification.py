"""Abstract API email-verification PERSON_ENRICHMENT provider.

Root problem this closes: Apollo's own email_status ("verified"/"guessed")
is Apollo's INTERNAL confidence in an email it found/guessed itself — never
an independent, live deliverability check. This codebase's own market
comparison against Clay/Apollo/ZoomInfo/Cognism flagged real email
verification (SMTP/MX/catch-all/disposable checks) as a commonly-expected
gap; this module closes it with a second, independent PERSON_ENRICHMENT
provider rather than trusting one vendor's own self-reported guess.

API: GET https://emailvalidation.abstractapi.com/v1/?api_key=...&email=...
(confirmed live against Abstract's own docs, 2026-09-30) — a real-time,
single-email, API-key-authenticated call, genuinely free up to 100
requests/month (a recurring monthly allowance, not an expiring trial
credit — confirmed against abstractapi.com/pricing). Response shape
(confirmed against docs.abstractapi.com/api/email-validation's own example):

    {
      "email": "...",
      "deliverability": "DELIVERABLE" | "UNDELIVERABLE" | "RISKY" | "UNKNOWN",
      "quality_score": 0.9,
      "is_valid_format": {"value": true, "text": "TRUE"},
      "is_free_email": {"value": true, "text": "TRUE"},
      "is_disposable_email": {"value": false, "text": "FALSE"},
      "is_role_email": {"value": false, "text": "FALSE"},
      "is_catchall_email": {"value": false, "text": "FALSE"},
      "is_mx_found": {"value": true, "text": "TRUE"},
      "is_smtp_valid": {"value": true, "text": "TRUE"}
    }

Only `deliverability` and `is_disposable_email`/`is_role_email`/
`is_catchall_email` are mapped into NormalizedRecord.attributes — the
fields an ICP/reviewer can actually act on (this codebase's own "never
fabricate more than the source gives" discipline again: quality_score is a
vendor-proprietary blend with no documented formula, so it is deliberately
NOT surfaced as if it were a first-class fact this codebase understands).
`deliverability` is mapped verbatim (never coerced into this codebase's
ConfidenceLevel scale here — see app/api/people.py's own
_confidence_from_apollo_email_status for the one place a caller may choose
to derive a confidence from it, exactly mirroring how Apollo's email_status
is already handled there).

This provider requires an email to already be known (from Apollo or any
other source) — it verifies, it never discovers a new email address from a
name alone. A request with no email is an honest, non-retryable failure,
never a guess.

The API key is supplied at construction (read once, from Settings, by
app/providers/default_registry.py) and never read from the environment
inside execute(), never included in any ProviderResponse field, never
logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderReliabilityProfile,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)

ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED = "ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED"
ABSTRACT_EMAIL_AUTH_FAILED = "ABSTRACT_EMAIL_AUTH_FAILED"
ABSTRACT_EMAIL_RATE_LIMITED = "ABSTRACT_EMAIL_RATE_LIMITED"

# Response boolean fields this adapter maps, each shaped
# {"value": bool, "text": str} in Abstract's own response — only `value` is
# ever read; `text` is a redundant display string the docs themselves note
# just mirrors `value` ("TRUE"/"FALSE").
_BOOLEAN_FIELD_MAP: dict[str, str] = {
    "is_disposable_email": "email_is_disposable",
    "is_role_email": "email_is_role_address",
    "is_catchall_email": "email_is_catchall",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AbstractEmailVerificationProvider(ProviderAdapter):
    """See module docstring for the full mechanism and honest scope."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://emailvalidation.abstractapi.com/v1",
        provider_id: str = "abstract-email-verification-v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Abstract Email Verification",
            capabilities={ProviderCapability.PERSON_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        email = request.query.get("email")
        if not email:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED,
                    message="No email supplied; this provider verifies a known email, it never discovers one.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        try:
            response = httpx.get(
                self._base_url,
                params={"api_key": self._api_key, "email": email},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Abstract email verification request failed: {exc}") from exc

        if response.status_code in (401, 403):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(code=ABSTRACT_EMAIL_AUTH_FAILED, message="Abstract API rejected the configured API key.", retryable=False),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )
        if response.status_code == 429:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ABSTRACT_EMAIL_RATE_LIMITED,
                    message="Abstract API's free-tier monthly quota (100 requests/month) has been reached.",
                    retryable=True,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )
        if response.status_code != 200:
            raise RuntimeError(f"Abstract email verification returned HTTP {response.status_code}")

        body: dict[str, Any] = response.json()
        attributes: dict[str, Any] = {}

        deliverability = body.get("deliverability")
        if deliverability:
            attributes["email_deliverability"] = deliverability

        for abstract_field, our_field in _BOOLEAN_FIELD_MAP.items():
            field_obj = body.get(abstract_field)
            if isinstance(field_obj, dict) and "value" in field_obj and isinstance(field_obj["value"], bool):
                attributes[our_field] = field_obj["value"]

        if not attributes:
            # Honest "nothing usable" outcome — never a record with no real
            # attributes pretending to be a successful verification (same
            # discipline as app/providers/sec_edgar.py's own empty-attributes
            # guard).
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ABSTRACT_EMAIL_NO_EMAIL_SUPPLIED,
                    message="Abstract API returned no usable verification fields for this email.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        record = NormalizedRecord(external_id=email, name=email, attributes=attributes)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )
