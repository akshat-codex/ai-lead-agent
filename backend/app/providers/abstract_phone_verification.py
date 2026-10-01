"""Abstract API phone-verification PERSON_ENRICHMENT provider.

Root problem this closes: this codebase had zero phone-number capability at
all before this module — not even format validation. Market comparison
against Clay/Apollo/ZoomInfo/Cognism confirmed phone data/verification is
one of the most commonly-expected gaps versus those tools (though even
their own phone match rates are modest — Clay's own numbers show only
30-35% phone match, ZoomInfo's phone data goes stale over time per
third-party audits — so this module closes a real gap without claiming
parity with a $15-25k/year phone-data vendor like Cognism).

API: GET https://phonevalidation.abstractapi.com/v1/?api_key=...&phone=...
(confirmed live against docs.abstractapi.com/api/phone-validation and an
independent third-party mirror of the same docs, 2026-09-30 — the two
agreed with each other and NOT with abstractapi.com's own marketing page,
which describes a different, nested response shape; the docs-subdomain
shape below is what this adapter implements, since two independent sources
corroborating it is exactly the "2+ agreeing sources" bar this codebase's
own evidence engine applies to everything else). Free tier: 100
requests/month, a genuinely recurring monthly allowance (not an expiring
trial), same account family as app/providers/abstract_email_verification.py.

    {
      "phone": "14152007986",
      "valid": true,
      "format": {"international": "+14152007986", "local": "(415) 200-7986"},
      "country": {"code": "US", "name": "United States", "prefix": "+1"},
      "location": "California",
      "type": "mobile",
      "carrier": "T-Mobile USA, Inc."
    }

HONEST SCOPE: the documented response has NO real-time reachability/
"currently active" field — only `valid` (numbering-plan/format validity)
and `type` (line-type classification: mobile/landline/voip). This adapter
therefore never claims to verify a number is live/reachable right now,
only that it is a validly-formatted, real-carrier-assigned number of a
known line type — an honest, narrower claim than "verified," matching
this codebase's "never state more confidence than the source actually
gives" rule (see app/providers/sec_edgar.py's identical discipline).

Only `valid`, `type`, and `carrier` are mapped into
NormalizedRecord.attributes — the fields with real B2B value (is this a
real, callable-shaped number; is it a mobile/direct line vs. a landline
switchboard, which an SDR cares about). `format`/`country`/`location` are
deliberately NOT mapped: they are redundant with a phone number's own
digits/already-known company geography and add no fact this codebase
doesn't already have another way to state.

Requires a phone number to already be known — it verifies, it never
discovers a new phone number from a name/company alone, the identical
"verify, never discover" contract as the email-verification sibling.

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

ABSTRACT_PHONE_NO_PHONE_SUPPLIED = "ABSTRACT_PHONE_NO_PHONE_SUPPLIED"
ABSTRACT_PHONE_AUTH_FAILED = "ABSTRACT_PHONE_AUTH_FAILED"
ABSTRACT_PHONE_RATE_LIMITED = "ABSTRACT_PHONE_RATE_LIMITED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AbstractPhoneVerificationProvider(ProviderAdapter):
    """See module docstring for the full mechanism and honest scope."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://phonevalidation.abstractapi.com/v1",
        provider_id: str = "abstract-phone-verification-v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Abstract Phone Verification",
            capabilities={ProviderCapability.PERSON_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        phone = request.query.get("phone")
        if not phone:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ABSTRACT_PHONE_NO_PHONE_SUPPLIED,
                    message="No phone number supplied; this provider verifies a known number, it never discovers one.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        try:
            response = httpx.get(
                self._base_url,
                params={"api_key": self._api_key, "phone": phone},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Abstract phone verification request failed: {exc}") from exc

        if response.status_code in (401, 403):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(code=ABSTRACT_PHONE_AUTH_FAILED, message="Abstract API rejected the configured API key.", retryable=False),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )
        if response.status_code == 429:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ABSTRACT_PHONE_RATE_LIMITED,
                    message="Abstract API's free-tier monthly quota (100 requests/month) has been reached.",
                    retryable=True,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )
        if response.status_code != 200:
            raise RuntimeError(f"Abstract phone verification returned HTTP {response.status_code}")

        body: dict[str, Any] = response.json()
        attributes: dict[str, Any] = {}

        if isinstance(body.get("valid"), bool):
            attributes["phone_is_valid"] = body["valid"]

        line_type = body.get("type")
        if isinstance(line_type, str) and line_type:
            attributes["phone_line_type"] = line_type

        carrier = body.get("carrier")
        if isinstance(carrier, str) and carrier:
            attributes["phone_carrier"] = carrier

        if not attributes:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ABSTRACT_PHONE_NO_PHONE_SUPPLIED,
                    message="Abstract API returned no usable verification fields for this phone number.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        record = NormalizedRecord(external_id=phone, name=phone, attributes=attributes)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )
