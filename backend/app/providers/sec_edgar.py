"""SEC EDGAR COMPANY_ENRICHMENT provider — a real, free-forever, no-API-key
data source (confirmed live against data.sec.gov before writing this).

Honest scope, stated up front: SEC EDGAR only covers **US SEC-registered
public companies and large foreign private issuers** — this is a narrow,
best-effort supplementary source, never a broad enrichment layer. Most
companies a real ICP search discovers (private SMBs) will simply not be
found here, and that is the expected, correct outcome — this provider
returns a clean "not found" rather than guessing at a name match, exactly
like every other provider in this codebase degrades honestly on a miss
(see app/providers/apollo.py's own APOLLO_NO_MATCH for the identical
pattern applied to a different provider).

Two real HTTP calls, no authentication:
  1. GET https://www.sec.gov/files/company_tickers.json — a small, static
     file mapping company name -> CIK (SEC's own central index key). This
     is the ONLY way to resolve a company by name; SEC EDGAR has no
     search-by-domain or fuzzy-name endpoint, so matching here is
     deliberately conservative (exact, case-insensitive name match only —
     never fuzzy/substring, to avoid enriching the wrong public company
     under a similar name).
  2. GET https://data.sec.gov/submissions/CIK{10-digit-cik}.json — the
     real per-company submissions record, from which this adapter maps
     ONLY fields SEC actually returns: `sic`/`sicDescription` (a real
     industry classification, mapped to this codebase's own "industry"
     evidence field) and the business address's `stateOrCountryDescription`
     (mapped to "country"). Employee count is deliberately NEVER read here
     — SEC's own structured employee-count field only exists inside
     inconsistently-tagged XBRL company-facts data (not every filer tags
     it), and guessing at it from unreliable data would violate this
     codebase's own "never fabricate a value we aren't confident in" rule.

SEC's fair-access policy requires a descriptive User-Agent identifying the
requester (not an API key) — set once, here, never varying per request.
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

SEC_EDGAR_NO_MATCH = "SEC_EDGAR_NO_MATCH"
SEC_EDGAR_LOOKUP_UNAVAILABLE = "SEC_EDGAR_LOOKUP_UNAVAILABLE"

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SecEdgarCompanyEnrichmentProvider(ProviderAdapter):
    """Free, no-key, best-effort COMPANY_ENRICHMENT — see module docstring
    for the honest "public companies only" scope. Registered unconditionally
    (no API key setting to gate it on — see app/providers/default_registry.py),
    since there is nothing to configure; it is simply always available,
    exactly like a public government API should be."""

    def __init__(
        self,
        base_url: str = "https://data.sec.gov",
        tickers_url: str = _TICKERS_URL,
        user_agent: str = "LeadAgent (open-source project; contact via GitHub issues)",
        provider_id: str = "sec-edgar-company-enrichment-v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="SEC EDGAR",
            capabilities={ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._base_url = base_url.rstrip("/")
        self._tickers_url = tickers_url
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        # The tickers file is ~800KB and changes infrequently (SEC updates
        # it periodically, not per-request) — fetched at most once per
        # process lifetime, never once per enrichment call, so enriching
        # N companies in one batch never means N downloads of this file.
        self._cik_by_name: dict[str, str] | None = None

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent}

    def _load_cik_index(self) -> dict[str, str]:
        if self._cik_by_name is not None:
            return self._cik_by_name

        response = httpx.get(self._tickers_url, headers=self._headers(), timeout=self._timeout_seconds)
        if response.status_code != 200:
            raise RuntimeError(f"SEC EDGAR ticker index returned HTTP {response.status_code}")

        body: dict[str, dict[str, Any]] = response.json()
        index: dict[str, str] = {}
        for entry in body.values():
            title = entry.get("title")
            cik = entry.get("cik_str")
            if isinstance(title, str) and title and cik is not None:
                # Exact, case-insensitive key only — never a substring/fuzzy
                # index. A public company's registered name colliding with
                # an unrelated private company's similar name is a real
                # risk this deliberately avoids by requiring an exact match
                # at lookup time too (see execute()).
                index[title.strip().lower()] = str(cik).zfill(10)
        self._cik_by_name = index
        return index

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        company_name = request.query.get("company_name")
        if not company_name:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=SEC_EDGAR_NO_MATCH,
                    message="No company_name supplied; SEC EDGAR cannot look up a company by domain alone.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        try:
            index = self._load_cik_index()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"SEC EDGAR ticker index request failed: {exc}") from exc

        cik = index.get(company_name.strip().lower())
        if cik is None:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=SEC_EDGAR_NO_MATCH,
                    message="No SEC-registered public company found with this exact name — expected for most private companies.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        try:
            response = httpx.get(
                _SUBMISSIONS_URL.format(cik=cik), headers=self._headers(), timeout=self._timeout_seconds
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"SEC EDGAR submissions request failed: {exc}") from exc

        if response.status_code == 404:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(code=SEC_EDGAR_NO_MATCH, message="SEC EDGAR has no submissions record for this CIK.", retryable=False),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )
        if response.status_code != 200:
            raise RuntimeError(f"SEC EDGAR submissions endpoint returned HTTP {response.status_code}")

        body = response.json()
        attributes: dict[str, Any] = {}

        sic_description = body.get("sicDescription")
        if sic_description:
            attributes["industry"] = sic_description

        business_address = (body.get("addresses") or {}).get("business") or {}
        country_description = business_address.get("stateOrCountryDescription")
        if country_description:
            attributes["country"] = country_description

        if not attributes:
            # SEC had a record but none of the fields we map were present —
            # an honest "nothing usable" outcome, never a record with no
            # real attributes pretending to be a successful enrichment.
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=SEC_EDGAR_NO_MATCH,
                    message="SEC EDGAR record found but had no usable industry/country fields.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        record = NormalizedRecord(external_id=cik, name=body.get("name") or company_name, attributes=attributes)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )
