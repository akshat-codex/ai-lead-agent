"""Phase 5 extension — real Apollo.io provider adapter for PERSON_ENRICHMENT.

This is the first non-mock ProviderAdapter in the codebase. It calls Apollo's
real "People Match" API (POST /people/match, header x-api-key) and maps only
the fields Apollo actually documents returning synchronously — name, title,
email, email_status, linkedin_url, organization_name. Apollo only returns
phone numbers asynchronously via a webhook (reveal_phone_number requires
webhook_url); this codebase has no webhook receiver, so phone is never
requested and never appears in this adapter's output. Nothing here invents a
field Apollo did not actually return for a given person.

The API key is supplied at construction (read once, from Settings, by
app/providers/default_registry.py) and never read from the environment
inside execute(), never included in any ProviderRequest/ProviderResponse
field, and never written to a log line.
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

APOLLO_AUTH_FAILED = "APOLLO_AUTH_FAILED"
APOLLO_NO_MATCH = "APOLLO_NO_MATCH"

# Apollo request-parameter names this adapter is willing to forward, mapped
# from this codebase's own PersonEnrichmentQuery field names (see
# app/schemas/person_enrichment.py). reveal_phone_number is deliberately
# absent — see module docstring.
_QUERY_PARAM_MAP: dict[str, str] = {
    "full_name": "name",
    "email": "email",
    "linkedin_id": "linkedin_url",
    "company_domain": "domain",
    "company_name": "organization_name",
}

# Apollo response fields this adapter maps into NormalizedRecord.attributes.
# Only present when Apollo's own response actually includes them — never
# filled with None to "complete" the shape.
_PERSON_FIELD_MAP: dict[str, str] = {
    "title": "title",
    "email": "email",
    "email_status": "email_status",
    "linkedin_url": "linkedin_url",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApolloPersonEnrichmentProvider(ProviderAdapter):
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.apollo.io/api/v1",
        provider_id: str = "apollo-person-enrichment-v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Apollo.io",
            capabilities={ProviderCapability.PERSON_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _build_apollo_params(self, query: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for our_key, apollo_key in _QUERY_PARAM_MAP.items():
            value = query.get(our_key)
            if value:
                params[apollo_key] = value
        return params

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        params = self._build_apollo_params(request.query)

        try:
            response = httpx.post(
                f"{self._base_url}/people/match",
                params=params,
                headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Network-level failure (timeout, connection error, ...) — let
            # ProviderAdapter.run() convert this into a clean, generic
            # PROVIDER_ERROR response. The exception message never contains
            # self._api_key (httpx does not echo request headers in its
            # exception messages).
            raise RuntimeError(f"Apollo request failed: {exc}") from exc

        if response.status_code in (401, 403):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=APOLLO_AUTH_FAILED,
                    message="Apollo rejected the configured API key.",
                    retryable=False,
                ),
                source=SourceMetadata(
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    retrieved_at=_now(),
                    is_mock=False,
                ),
            )

        if response.status_code >= 400:
            # Any other HTTP error (rate limit, bad request, server error) —
            # raise with only the status code, never the response body
            # (which could theoretically echo request params back).
            raise RuntimeError(f"Apollo returned HTTP {response.status_code}")

        body = response.json()
        person = body.get("person")

        if not person:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(code=APOLLO_NO_MATCH, message="Apollo found no matching person.", retryable=False),
                source=SourceMetadata(
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    retrieved_at=_now(),
                    is_mock=False,
                ),
            )

        attributes: dict[str, Any] = {}
        for apollo_field, our_field in _PERSON_FIELD_MAP.items():
            value = person.get(apollo_field)
            if value:
                attributes[our_field] = value

        organization_name = person.get("organization_name")
        if organization_name:
            attributes["organization_name"] = organization_name

        record = NormalizedRecord(
            external_id=str(person.get("id") or request.request_id or ""),
            name=person.get("name") or "",
            attributes=attributes,
        )

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=False,
            ),
        )
