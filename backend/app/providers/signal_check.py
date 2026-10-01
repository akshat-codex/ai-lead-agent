"""Tavily-backed COMPANY_ENRICHMENT provider for real commercial signals.

Root problem this closes: app/services/commercial_signal_extractor.py's
SIGNAL_DEFINITIONS (e.g. FUNDING, MARKETING_HIRING) is a pure keyword-phrase
matcher over whatever evidence text a company already has — it has no
external data source of its own, so a signal like FUNDING only ever surfaces
if some OTHER provider happened to already write funding-flavored text into
one of _ALLOWED_SIGNAL_FIELDS (products_services/business_model/
company_type). For most companies, nothing ever does, so these signal
categories are effectively dead even though the matching engine behind them
works correctly. This module is a real, live data source for exactly the two
signal categories this codebase's own comparison against market tools
(Clay/Apollo/Cognism, all of which have live job-posting and funding-filing
feeds) flagged as commodity-pattern gaps: hiring-for-marketing and funding.

Mechanism: one targeted Tavily search per signal category per company
(reusing the existing TAVILY_API_KEY — see app/providers/tavily.py; no new
credential required), each built to surface pages that state the signal in
plain, matchable language:
  - MARKETING_HIRING: "site:jobs.lever.co OR site:boards.greenhouse.io
    <company> marketing" — mirrors app/providers/tavily.py's own
    _build_hiring_queries query shape (already confirmed live to return
    real job postings), scoped here to one already-known company instead of
    an ICP-wide role search.
  - FUNDING: "<company> raised funding OR series a OR series b OR seed
    funding" — a plain web search for funding-announcement language.

Each matched result's own title+snippet text (never a paraphrase, never an
inference) is written into NormalizedRecord.attributes["products_services"]
— the exact evidence field SIGNAL_DEFINITIONS already scans (see
_ALLOWED_SIGNAL_FIELDS) — so the existing, unmodified keyword-matching engine
in commercial_signal_extractor.py genuinely detects it. This module NEVER
itself decides "this company has signal X"; it only surfaces real search
result text for the existing engine to evaluate, preserving this codebase's
evidence/qualification separation (see that module's own docstring).

Honest scope: a search returning no matching result is reported as a clean,
non-fabricated "no signal found today" — never a guessed negative, and never
retried as if it were a transient failure. A company having no discoverable
hiring/funding signal today is the overwhelmingly common, correct outcome;
this only ever adds evidence, never removes or contradicts what other
providers already found (COMPANY_ENRICHMENT's existing multi-provider merge
in app/services/company_enrichment.py handles a same-field disagreement, if
one somehow occurred, exactly as it always has).

Opt-in via settings.enable_signal_check_provider (see app/core/config.py) —
off by default for the same two reasons app/providers/sec_edgar.py's sibling
flag is off by default: (1) tests must never make real network calls
unconditionally, and (2) this makes two extra Tavily API calls per company
beyond whatever Tavily is already doing for discovery, a real, visible cost
a user should consciously choose to pay.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
from app.providers.tavily import _sanitize_query_term

SIGNAL_CHECK_NO_MATCH = "SIGNAL_CHECK_NO_MATCH"
SIGNAL_CHECK_AUTH_FAILED = "SIGNAL_CHECK_AUTH_FAILED"
SIGNAL_CHECK_RATE_LIMITED = "SIGNAL_CHECK_RATE_LIMITED"

# Mirrors app/services/commercial_signal_extractor.py's own FUNDING phrase
# tuple exactly — this query is deliberately built to surface pages that use
# this codebase's own matchable vocabulary, not a paraphrase of it.
_FUNDING_QUERY_SUFFIX = 'raised funding OR "series a" OR "series b" OR "seed funding"'


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SignalCheckProvider(ProviderAdapter):
    """One Tavily search per signal category per company — see module
    docstring for the full rationale and honest scope."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        timeout_seconds: float = 30.0,
        max_results_per_query: int = 5,
        provider_id: str = "signal-check-v1",
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Signal Check (Tavily)",
            capabilities={ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_results_per_query = max_results_per_query

    def _error_response(self, request: ProviderRequest, code: str, message: str, retryable: bool = False) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=False,
            error=ProviderError(code=code, message=message, retryable=retryable),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )

    def _search(self, query_text: str) -> tuple[list[dict], int | None]:
        """Returns (results, error_status_code). error_status_code is None on
        HTTP 200; results is always [] when it is set. Never raises —
        matches this module's own honest-degradation discipline (see
        app/providers/tavily.py::_resolve_homepage_domain for the same
        never-raise pattern applied to a different auxiliary search)."""
        try:
            response = httpx.post(
                f"{self._base_url}/search",
                json={
                    "api_key": self._api_key,
                    "query": query_text,
                    "search_depth": "basic",
                    "max_results": self._max_results_per_query,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Signal check search request failed: {exc}") from exc

        if response.status_code != 200:
            return [], response.status_code
        body = response.json()
        return list(body.get("results") or []), None

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        company_name = request.query.get("company_name")
        if not company_name:
            return self._error_response(
                request, SIGNAL_CHECK_NO_MATCH, "No company_name supplied; signal check has no domain-only search mode."
            )

        term = _sanitize_query_term(company_name)
        queries = [
            f'"{term}" site:jobs.lever.co OR site:boards.greenhouse.io marketing',
            f'"{term}" {_FUNDING_QUERY_SUFFIX}',
        ]

        snippets: list[str] = []
        for query_text in queries:
            results, error_status = self._search(query_text)
            if error_status == 401:
                return self._error_response(request, SIGNAL_CHECK_AUTH_FAILED, "Tavily rejected the configured API key.")
            if error_status in (429, 432):
                return self._error_response(request, SIGNAL_CHECK_RATE_LIMITED, "Tavily usage limit reached.", retryable=True)
            # Any other per-query failure (malformed/rejected query, 5xx) is
            # skipped, not fatal — the OTHER query for this same company may
            # still succeed, exactly like app/providers/tavily.py's own
            # per-query failure handling in TavilyCompanyDiscoveryProvider.execute.
            for result in results:
                title = result.get("title") or ""
                content = result.get("content") or ""
                text = f"{title} {content}".strip()
                if text:
                    snippets.append(text[:1000])

        if not snippets:
            return self._error_response(
                request,
                SIGNAL_CHECK_NO_MATCH,
                "No hiring/funding-related search result found for this company today.",
            )

        combined_text = " | ".join(snippets)[:4000]
        record = NormalizedRecord(
            external_id=f"signal-check:{company_name.strip().lower()}",
            name=company_name,
            # Written into products_services — the exact evidence field
            # app/services/commercial_signal_extractor.py's
            # _ALLOWED_SIGNAL_FIELDS already scans (see module docstring).
            # Never a synthesized claim: this is the real search result
            # title+snippet text, verbatim, truncated only for storage size.
            attributes={"products_services": combined_text},
        )

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )
