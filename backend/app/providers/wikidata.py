"""Wikidata SPARQL COMPANY_ENRICHMENT provider — a real, free-forever,
no-API-key data source (confirmed live against query.wikidata.org before
writing this).

Honest scope, stated up front: Wikidata only has an item for companies
notable enough to have structured coverage (roughly: large, well-known,
publicly covered, or well-funded companies) — most small/mid-size private
B2B companies a real ICP search discovers will simply have no Wikidata
item, and that is the expected, correct outcome. This is a narrow,
best-effort supplementary source, never a broad enrichment layer — exactly
the same honest-degradation posture as app/providers/sec_edgar.py's own
module docstring.

One real HTTP call, no authentication: a SPARQL query against Wikidata's
public endpoint, matching an item by an EXACT English label (never a
fuzzy/substring match, to avoid attaching an unrelated company's Wikidata
facts to the wrong candidate), restricted to instances of "business"
(wd:Q4830453) or its subclasses — never matching a person, a product, or
an unrelated concept that happens to share the same label text.

Maps only Wikidata properties that exist for the matched item: P452
(industry) -> "industry", P17 (country) -> "country", P571 (inception
date) -> "founded_year" (year only, extracted from the ISO date Wikidata
returns). Employee count (P1128) is deliberately NEVER read here — it is
sparsely populated even for companies that do have Wikidata items, and
using a rarely-present, often-stale field would risk exactly the kind of
unreliable value this codebase's own evidence discipline exists to avoid.

Wikidata sometimes has MULTIPLE values for the same property (e.g. two
industry classifications) — this adapter deterministically takes only the
FIRST bound value per property from the query results, never attempting
to merge/choose between them itself; if a field matters enough to need
every value, a caller can extend the query, this adapter stays simple and
predictable.
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

WIKIDATA_NO_MATCH = "WIKIDATA_NO_MATCH"

_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# wd:Q4830453 = "business" — restricts matches to companies/business
# entities, never a person or unrelated concept sharing the same label.
_QUERY_TEMPLATE = """
SELECT ?itemLabel ?industryLabel ?countryLabel ?inception WHERE {{
  ?item rdfs:label "{escaped_name}"@en.
  ?item wdt:P31/wdt:P279* wd:Q4830453.
  OPTIONAL {{ ?item wdt:P452 ?industry. }}
  OPTIONAL {{ ?item wdt:P17 ?country. }}
  OPTIONAL {{ ?item wdt:P571 ?inception. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 5
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _escape_sparql_string(value: str) -> str:
    """Minimal, deliberate escaping for embedding user-influenced text
    (a discovered company name) inside a SPARQL string literal — escapes
    backslashes and double quotes, the two characters that could otherwise
    break out of the quoted literal. Never attempts broader SPARQL
    injection prevention beyond this string-literal context, since this
    adapter never interpolates the name anywhere else in the query."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _first_value(bindings: list[dict[str, Any]], key: str) -> str | None:
    for binding in bindings:
        entry = binding.get(key)
        if entry and entry.get("value"):
            return entry["value"]
    return None


class WikidataCompanyEnrichmentProvider(ProviderAdapter):
    """Free, no-key, best-effort COMPANY_ENRICHMENT — see module docstring
    for the honest "Wikidata-notable companies only" scope. Registered
    unconditionally (no API key setting to gate it on — see
    app/providers/default_registry.py), exactly like SEC EDGAR."""

    def __init__(
        self,
        endpoint: str = _SPARQL_ENDPOINT,
        user_agent: str = "LeadAgent/1.0 (open-source project; contact via GitHub issues)",
        provider_id: str = "wikidata-company-enrichment-v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Wikidata",
            capabilities={ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._endpoint = endpoint
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        company_name = request.query.get("company_name")
        if not company_name:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=WIKIDATA_NO_MATCH,
                    message="No company_name supplied; Wikidata lookup here matches by exact label only.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        query = _QUERY_TEMPLATE.format(escaped_name=_escape_sparql_string(company_name.strip()))

        try:
            response = httpx.get(
                self._endpoint,
                params={"query": query, "format": "json"},
                headers={"User-Agent": self._user_agent, "Accept": "application/sparql-results+json"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Wikidata SPARQL request failed: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(f"Wikidata SPARQL endpoint returned HTTP {response.status_code}")

        body = response.json()
        bindings: list[dict[str, Any]] = (body.get("results") or {}).get("bindings") or []

        if not bindings:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=WIKIDATA_NO_MATCH,
                    message="No Wikidata item found for this exact company name — expected for most companies without a Wikidata page.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        attributes: dict[str, Any] = {}

        industry = _first_value(bindings, "industryLabel")
        if industry:
            attributes["industry"] = industry

        country = _first_value(bindings, "countryLabel")
        if country:
            attributes["country"] = country

        inception = _first_value(bindings, "inception")
        if inception:
            # Wikidata returns a full ISO datetime (e.g. "2010-01-01T00:00:00Z")
            # for a date-precision fact — only the year is reliable/meaningful
            # here, never a fabricated day/month precision Wikidata didn't
            # actually assert.
            year_text = inception[:4]
            if year_text.isdigit():
                attributes["founded_year"] = int(year_text)

        matched_name = _first_value(bindings, "itemLabel") or company_name

        if not attributes:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=WIKIDATA_NO_MATCH,
                    message="A Wikidata item matched this name but had no usable industry/country/founding-year facts.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        record = NormalizedRecord(external_id=company_name.strip().lower(), name=matched_name, attributes=attributes)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )
