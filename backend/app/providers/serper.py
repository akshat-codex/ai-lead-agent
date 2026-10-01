"""Serper (Google Search API) COMPANY_DISCOVERY provider.

Same purpose, same query strategy, and same trust discipline as
app/providers/tavily.py — see that module's own docstring for the full
live-test rationale (the naive "<industry> companies" query reliably
surfaces listicle articles, never real companies; fixed by querying
content that is structurally single-company by construction: Crunchbase/
YC directory profiles, Lever/Greenhouse job postings). Serper wraps real
Google Search results, giving a second, independent web-search source
alongside Tavily rather than relying on one vendor's index/ranking alone.

Serper's API (https://serper.dev) returns Google's organic results as
{title, link, snippet} per result — structurally the same shape Tavily
returns ({title, url, content}), just different field names — so this
provider reuses tavily.py's extraction/verification logic
(_extract_directory_result, _extract_hiring_result, _is_job_role_title,
_verify_homepage_match, _is_homepage_host) directly rather than
duplicating it; the two providers are deliberately kept structurally
identical so Hard mode's parallel Explorium+Tavily+Serper run treats them
as equally-weighted, independent corroborating sources.

Also mirrors Tavily's own homepage-resolution step (see tavily.py's
"Homepage resolution" section) — a second, verified search per candidate
so NormalizedRecord.attributes["domain"] gets populated whenever it can
be genuinely corroborated, which is what makes cross-provider
corroboration with Explorium (via app/services/company_resolution.py's
domain-match merge path) possible at all for a Serper-sourced candidate.

Trust discipline: identical to Tavily and Hermes — never added to
app/services/evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS. A lone
Serper sighting is an honest, unverified web-research sighting, never a
structured taxonomy match.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.tavily import (
    _extract_directory_result,
    _extract_hiring_result,
    _host_of,
    _is_crunchbase_block_page,
    _is_homepage_host,
    _is_job_role_title,
    _looks_like_directory_profile_path,
    _sanitize_query_term,
    _url_path,
    _verify_homepage_match,
)
from app.services.company_identity import normalize_domain

logger = logging.getLogger(__name__)

SERPER_AUTH_FAILED = "SERPER_AUTH_FAILED"
SERPER_RATE_LIMITED = "SERPER_RATE_LIMITED"


class SerperCompanyDiscoveryProvider(ProviderAdapter):
    """Calls Serper's real POST /search endpoint with the same two
    LIVE-VERIFIED query types as app/providers/tavily.py::
    TavilyCompanyDiscoveryProvider — see that class's own docstring for
    the full rationale and confirmed-live examples of each. DIRECTORY
    (Crunchbase/YC company profiles) always runs when the ICP has
    industries; HIRING (Lever/Greenhouse job postings) only runs when the
    ICP's allowed_titles names a genuinely job-postable role (see
    _is_job_role_title)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://google.serper.dev",
        timeout_seconds: float = 30.0,
        max_results_per_query: int = 10,
    ) -> None:
        super().__init__(
            provider_id="serper-company-discovery-v1",
            provider_name="Serper",
            capabilities={ProviderCapability.COMPANY_DISCOVERY},
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_results_per_query = max_results_per_query

    def _geography_suffix(self, query: dict) -> str:
        geography_unrecognized = query.get("geography_unrecognized") or ()
        return f" in {geography_unrecognized[0]}" if len(geography_unrecognized) == 1 else ""

    def _build_directory_queries(self, query: dict) -> list[tuple[str, str]]:
        # Mirrors app/providers/tavily.py::TavilyCompanyDiscoveryProvider.
        # _build_directory_queries exactly — see that method's own
        # docstring for why company_types is folded in alongside
        # industries (both describe "what kind of company this is";
        # excluding company_types silently zeroed out results for any
        # ICP defined mainly by company type rather than industry).
        industries = query.get("industries") or ()
        company_types = query.get("company_types") or ()
        seen_terms: set[str] = set()
        terms: list[str] = []
        for raw_term in (*industries, *company_types):
            term = _sanitize_query_term(raw_term)
            key = term.lower()
            if not term or key in seen_terms:
                continue
            seen_terms.add(key)
            terms.append(term)

        geography_suffix = self._geography_suffix(query)
        pairs: list[tuple[str, str]] = []
        for term in terms:
            pairs.append((f'site:crunchbase.com/organization "{term}"{geography_suffix}', "directory"))
            pairs.append((f'site:ycombinator.com/companies "{term}"{geography_suffix}', "directory"))
        return pairs

    def _build_hiring_queries(self, query: dict) -> list[tuple[str, str]]:
        allowed_titles = query.get("allowed_titles") or ()
        job_roles = [title for title in allowed_titles if _is_job_role_title(title)]
        if not job_roles:
            return []
        geography_suffix = self._geography_suffix(query)
        pairs: list[tuple[str, str]] = []
        for role in job_roles:
            term = _sanitize_query_term(role)
            if not term:
                continue
            pairs.append((f'site:jobs.lever.co OR site:boards.greenhouse.io "{term}"{geography_suffix}', "hiring"))
        return pairs

    def _resolve_homepage_domain(self, company_name: str, candidate_description: str) -> str | None:
        """Serper's own version of app/providers/tavily.py::
        TavilyCompanyDiscoveryProvider._resolve_homepage_domain — see that
        module's "Homepage resolution" section (above its class
        definition) for the full rationale. Reuses the SAME verification
        logic (_verify_homepage_match) so both providers apply an
        identical, never-guess bar for accepting a resolved domain."""
        try:
            response = httpx.post(
                f"{self._base_url}/search",
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json={"q": f'"{_sanitize_query_term(company_name)}" official site', "num": 5},
                timeout=self._timeout_seconds,
            )
            if response.status_code != 200:
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("serper_homepage_resolution_failed company=%r error=%s", company_name, exc)
            return None

        for result in body.get("organic", []) or []:
            url = result.get("link")
            title = result.get("title") or ""
            snippet = result.get("snippet") or ""
            if not url:
                continue
            host = _host_of(url)
            if not host or not _is_homepage_host(host):
                continue
            # Path-shape guard (see app/providers/tavily.py::
            # _looks_like_directory_profile_path's own comment) — catches
            # a data-broker/directory site even when its hostname was
            # never added to the shared denylist.
            if _looks_like_directory_profile_path(_url_path(url)):
                continue
            if not _verify_homepage_match(company_name, candidate_description, title, snippet):
                continue
            domain = normalize_domain(host)
            if domain:
                return domain
        return None

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        query_pairs = self._build_directory_queries(request.query) + self._build_hiring_queries(request.query)
        if not query_pairs:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=False
                ),
                exhausted=True,
            )

        limit = request.query.get("limit") or 20
        records: list[NormalizedRecord] = []
        seen_external_ids: set[str] = set()

        for search_query, query_kind in query_pairs:
            response = httpx.post(
                f"{self._base_url}/search",
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json={"q": search_query, "num": self._max_results_per_query},
                timeout=self._timeout_seconds,
            )

            # Auth/rate-limit failures apply to the WHOLE API key, not one
            # query — every remaining query would fail identically, so
            # these still abort the call immediately, exactly as before.
            if response.status_code == 401 or response.status_code == 403:
                return self._error_response(request, SERPER_AUTH_FAILED, "Serper rejected the configured API key.")
            if response.status_code == 429:
                return self._error_response(request, SERPER_RATE_LIMITED, "Serper usage limit reached.", retryable=True)
            if response.status_code != 200:
                # A per-QUERY failure must never abort every OTHER query in
                # the same call — see tavily.py's identical comment at its
                # own equivalent call site for the full rationale.
                logger.warning(
                    "serper_query_failed query=%r status=%d body=%r", search_query, response.status_code, response.text[:200]
                )
                continue

            body = response.json()
            for result in body.get("organic", []) or []:
                url = result.get("link")
                title = result.get("title")
                snippet = result.get("snippet")
                if not url or not title:
                    continue
                if _is_crunchbase_block_page(snippet):
                    continue

                extracted = _extract_directory_result(url, title) if query_kind == "directory" else _extract_hiring_result(url, title)
                if extracted is None:
                    continue
                external_id_suffix, name = extracted
                external_id = f"serper:{external_id_suffix}"
                if external_id in seen_external_ids:
                    continue
                seen_external_ids.add(external_id)

                attributes: dict = {}
                description = snippet[:2000] if snippet else ""
                if description:
                    attributes["description"] = description

                domain = self._resolve_homepage_domain(name, description)
                if domain:
                    attributes["domain"] = domain

                records.append(NormalizedRecord(external_id=external_id, name=name, attributes=attributes))
                if len(records) >= limit:
                    break
            if len(records) >= limit:
                break

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(records),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=False
            ),
            # Same reasoning as tavily.py: not a resumable cursor-based
            # API, so every call is reported exhausted to avoid a wasted
            # repeat "Find More" round with nothing new to advance.
            exhausted=True,
        )

    def _error_response(self, request: ProviderRequest, code: str, message: str, retryable: bool = False) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=False,
            error=ProviderError(code=code, message=message, retryable=retryable),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=False
            ),
        )
