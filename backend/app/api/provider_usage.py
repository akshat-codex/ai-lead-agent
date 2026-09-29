"""Real, account-level provider usage/balance visibility — deliberately
NOT a per-batch cost estimate (see app/schemas/batch.py::BatchDetailRead.
provider_call_counts for that, a simple real-call-count, no fabricated
dollar amount).

Researched directly against each vendor's own documentation before
writing this: Tavily is the ONLY one of the four discovery-mode providers
(Explorium, Gemini, Tavily, Serper) that exposes a genuine, documented
remaining-usage endpoint — see app/providers/tavily.py::
TavilyCompanyDiscoveryProvider.get_usage's own docstring. Explorium's
credit balance, Gemini's quota, and Serper's credits are all dashboard-
only; there is no endpoint for this backend to poll for those three, so
this module reports them as honestly UNAVAILABLE rather than guessing or
omitting them silently (a caller asking "what's my Serper balance" must
get "we can't check that programmatically," never a stale/fake number).
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.providers.tavily import TavilyCompanyDiscoveryProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/provider-usage", tags=["provider-usage"])


class ProviderUsageEntry(BaseModel):
    provider_id: str
    provider_name: str
    available: bool
    # Only populated when available=True — Tavily's raw {"key": {...},
    # "account": {...}} response body, unmodified (see get_usage's own
    # docstring for why this is never reshaped/reinterpreted here).
    usage: dict | None = None
    unavailable_reason: str | None = None


@router.get("", response_model=list[ProviderUsageEntry])
def list_provider_usage() -> list[ProviderUsageEntry]:
    """One entry per discovery-mode provider that COULD exist, regardless
    of whether it's currently configured — so the frontend can show a
    consistent 4-row table ("configure a key to see usage" / "no
    programmatic balance check exists for this vendor" / real numbers)
    rather than a list that silently shrinks based on what's in .env."""
    settings = get_settings()
    entries: list[ProviderUsageEntry] = []

    if not settings.tavily_api_key:
        entries.append(
            ProviderUsageEntry(
                provider_id="tavily-company-discovery-v1",
                provider_name="Tavily",
                available=False,
                unavailable_reason="TAVILY_API_KEY is not configured.",
            )
        )
    else:
        provider = TavilyCompanyDiscoveryProvider(api_key=settings.tavily_api_key, base_url=settings.tavily_base_url)
        try:
            usage = provider.get_usage()
            entries.append(
                ProviderUsageEntry(provider_id="tavily-company-discovery-v1", provider_name="Tavily", available=True, usage=usage)
            )
        except httpx.HTTPError as exc:
            logger.warning("tavily_usage_check_failed error=%s", exc)
            entries.append(
                ProviderUsageEntry(
                    provider_id="tavily-company-discovery-v1",
                    provider_name="Tavily",
                    available=False,
                    unavailable_reason=f"Tavily's usage endpoint could not be reached: {exc}",
                )
            )

    # Explorium, Gemini, and Serper genuinely have no programmatic
    # balance/quota-check endpoint — confirmed against each vendor's own
    # documentation, not an oversight. Reported honestly rather than
    # omitted, so the frontend never has to guess why only Tavily shows a
    # number.
    entries.append(
        ProviderUsageEntry(
            provider_id="explorium-company-discovery-v1",
            provider_name="Explorium",
            available=False,
            unavailable_reason="Explorium exposes no API endpoint for remaining credit balance — check the Explorium Admin Portal dashboard.",
        )
    )
    entries.append(
        ProviderUsageEntry(
            provider_id="gemini-llm-v1",
            provider_name="Gemini",
            available=False,
            unavailable_reason="Gemini exposes no API endpoint for remaining quota/balance — check Google AI Studio.",
        )
    )
    entries.append(
        ProviderUsageEntry(
            provider_id="serper-company-discovery-v1",
            provider_name="Serper",
            available=False,
            unavailable_reason="Serper exposes no documented API endpoint for remaining credit balance — check the Serper dashboard.",
        )
    )

    return entries
