"""Phase 7B — real Unipile provider adapter for PEOPLE_DISCOVERY and
(fallback) COMPANY_ENRICHMENT.

Calls Unipile's real LinkedIn Search API (POST /api/v1/linkedin/search,
header X-API-KEY, query param account_id) using api="classic",
category="people" or category="companies". Maps only the fields Unipile
actually documents returning for each — confirmed against Unipile's own
published API reference (developer.unipile.com/reference/linkedincontroller_search
and developer.unipile.com/docs/linkedin-search) before writing this mapping.
Nothing here is invented.

Unlike Apollo/Explorium's simple API-key auth, Unipile requires a
pre-connected LinkedIn account: every search is performed "on behalf of"
one specific account_id configured up front (see
app/providers/default_registry.py — all three of UNIPILE_API_KEY,
UNIPILE_DSN, UNIPILE_ACCOUNT_ID must be set together for this provider to
register at all).

PEOPLE_DISCOVERY (primary capability): a person's LinkedIn identity is
never guaranteed complete — public_profile_url, public_identifier, and even
the specific company_id in current_positions can each independently be
absent depending on network distance and profile visibility (confirmed
against Unipile's own documented examples, which show these fields as null
for out-of-network profiles). This adapter treats every one of them as
optional; person-to-company association is left ENTIRELY to the existing,
already-correct app/services/person_resolution.py — this adapter never
decides identity itself, it only reports what Unipile's search actually
returned.

COMPANY_ENRICHMENT (fallback for company LinkedIn, only used when
Explorium's discovery-time linkedin_id is absent): Unipile's LinkedIn
company search has no domain/website field, so it is queried by company
name only (the weakest identity signal in this codebase's own resolution
rules) — but enrichment facts are display/evidence data, never
identity-merging data (see app/services/company_enrichment.py's own
multi-provider-merge design, which already tolerates and flags disagreeing
providers rather than silently picking one), so a name-only lookup here
does not carry the same misidentification risk PEOPLE_DISCOVERY's identity
resolution guards against.

Every LinkedIn URL this adapter emits is normalized through the SAME
normalize_linkedin_identifier() the rest of the codebase already uses for
person LinkedIn values (app/services/person_identity.py) — reused here, not
duplicated — so a full Unipile-supplied URL (e.g.
"https://www.linkedin.com/company/netflix/") is stored in the same bare
handle form ("company/netflix") the existing mock/Explorium data and the
frontend's existing URL-building logic already expect. Without this, a raw
full URL passed straight into attributes["linkedin_id"] would make the
frontend build a malformed doubled URL.

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
from app.services.person_identity import normalize_linkedin_identifier

UNIPILE_AUTH_FAILED = "UNIPILE_AUTH_FAILED"
UNIPILE_MALFORMED_RESPONSE = "UNIPILE_MALFORMED_RESPONSE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UnipileProvider(ProviderAdapter):
    def __init__(
        self,
        api_key: str,
        dsn: str,
        account_id: str,
        provider_id: str = "unipile-linkedin-v1",
        timeout_seconds: float = 15.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Unipile",
            capabilities={ProviderCapability.PEOPLE_DISCOVERY, ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._api_key = api_key
        self._dsn = dsn.rstrip("/")
        self._account_id = account_id
        self._timeout_seconds = timeout_seconds

    def _post_search(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return httpx.post(
                f"{self._dsn}/api/v1/linkedin/search",
                params={"account_id": self._account_id},
                json=body,
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Network-level failure (timeout, connection error, ...) — let
            # ProviderAdapter.run() convert this into a clean, generic
            # PROVIDER_ERROR response. The exception message never contains
            # self._api_key (httpx does not echo request headers in its
            # exception messages).
            raise RuntimeError(f"Unipile request failed: {exc}") from exc

    def _handle_http_errors(self, response: httpx.Response, request: ProviderRequest) -> ProviderResponse | None:
        """Returns a clean failure ProviderResponse for a known error status,
        or None if the caller should continue processing a 2xx body."""
        if response.status_code in (401, 403):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=UNIPILE_AUTH_FAILED,
                    message="Unipile rejected the configured API key or connected account.",
                    retryable=False,
                ),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )
        if response.status_code >= 400:
            # Any other HTTP error (rate limit, bad request, server error) —
            # raise with only the status code, never the response body
            # (which could theoretically echo request params back).
            raise RuntimeError(f"Unipile returned HTTP {response.status_code}")
        return None

    def _parse_json(self, response: httpx.Response, request: ProviderRequest) -> dict[str, Any] | ProviderResponse:
        try:
            body = response.json()
        except ValueError as exc:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=UNIPILE_MALFORMED_RESPONSE,
                    message=f"Unipile returned a response that could not be parsed as JSON: {exc}",
                    retryable=False,
                ),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )
        if not isinstance(body, dict):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=UNIPILE_MALFORMED_RESPONSE,
                    message="Unipile's response body was not a JSON object.",
                    retryable=False,
                ),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )
        return body

    # --- PEOPLE_DISCOVERY -----------------------------------------------

    def _execute_people_discovery(self, request: ProviderRequest) -> ProviderResponse:
        query = request.query
        titles = query.get("titles") or []
        company_name = query.get("company_name")
        limit = query.get("limit")

        body: dict[str, Any] = {"api": "classic", "category": "people"}
        if company_name:
            body["company"] = [company_name]
        if titles:
            # Unipile's advanced_keywords.title is a single free-text field,
            # not a list filter — this codebase's ICP allowed_titles can be
            # multiple; Unipile itself only supports narrowing by one title
            # phrase per search, so the titles are joined into Unipile's own
            # documented OR-keyword-search convention via `keywords` instead
            # of silently dropping every title after the first.
            body["keywords"] = " OR ".join(titles)
        if limit:
            body["limit"] = min(int(limit), 10)  # Unipile Classic's documented max per page

        response = self._post_search(body)

        error_response = self._handle_http_errors(response, request)
        if error_response is not None:
            return error_response

        parsed = self._parse_json(response, request)
        if isinstance(parsed, ProviderResponse):
            return parsed

        items = parsed.get("items")
        if not isinstance(items, list):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=UNIPILE_MALFORMED_RESPONSE,
                    message="Unipile's people search response had no usable 'items' list.",
                    retryable=False,
                ),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )

        records: list[NormalizedRecord] = []
        for person in items:
            if not isinstance(person, dict):
                continue
            name = person.get("name")
            external_id = person.get("id")
            if not name or not external_id:
                # Neither a durable id nor a name means this isn't a usable
                # candidate — skip rather than fabricate either.
                continue

            attributes: dict[str, Any] = {}

            current_positions = person.get("current_positions")
            if isinstance(current_positions, list) and current_positions:
                first_position = current_positions[0]
                if isinstance(first_position, dict):
                    role = first_position.get("role")
                    if role:
                        attributes["title"] = role
                    company = first_position.get("company")
                    if company:
                        attributes["company association"] = company

            # LinkedIn URL preference order: the canonical public
            # linkedin.com/in/ URL first, then the raw public identifier
            # handle, then profile_url ONLY if it is itself a real
            # linkedin.com/in/ URL (Sales Navigator's profile_url points at
            # an internal /sales/lead/... page, never a public profile, and
            # must never be presented as one).
            linkedin_url = person.get("public_profile_url")
            if not linkedin_url:
                public_identifier = person.get("public_identifier")
                if public_identifier:
                    linkedin_url = f"https://www.linkedin.com/in/{public_identifier}"
            if not linkedin_url:
                candidate_url = person.get("profile_url")
                if candidate_url and "linkedin.com/in/" in candidate_url:
                    linkedin_url = candidate_url

            normalized_linkedin = normalize_linkedin_identifier(linkedin_url) if linkedin_url else None
            if normalized_linkedin:
                attributes["linkedin_id"] = normalized_linkedin
                attributes["linkedin_url"] = f"https://www.linkedin.com/{normalized_linkedin}"

            records.append(NormalizedRecord(external_id=str(external_id), name=str(name), attributes=attributes))

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(records),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
            ),
        )

    # --- COMPANY_ENRICHMENT (fallback company LinkedIn only) ------------

    def _execute_company_enrichment(self, request: ProviderRequest) -> ProviderResponse:
        company_name = request.query.get("company_name")
        if not company_name:
            # Unipile's company search has no domain/website filter — with
            # no name to search by, there is nothing honest to return.
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )

        body: dict[str, Any] = {"api": "classic", "category": "companies", "keywords": company_name, "limit": 1}
        response = self._post_search(body)

        error_response = self._handle_http_errors(response, request)
        if error_response is not None:
            return error_response

        parsed = self._parse_json(response, request)
        if isinstance(parsed, ProviderResponse):
            return parsed

        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )

        top_result = items[0]
        if not isinstance(top_result, dict):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )

        name = top_result.get("name")
        external_id = top_result.get("id")
        if not name or not external_id:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
                ),
            )

        attributes: dict[str, Any] = {}
        profile_url = top_result.get("profile_url")
        if profile_url:
            normalized = normalize_linkedin_identifier(profile_url)
            if normalized:
                attributes["linkedin_id"] = normalized

        record = NormalizedRecord(external_id=str(external_id), name=str(name), attributes=attributes)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False
            ),
        )

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.capability == ProviderCapability.PEOPLE_DISCOVERY:
            return self._execute_people_discovery(request)
        return self._execute_company_enrichment(request)
