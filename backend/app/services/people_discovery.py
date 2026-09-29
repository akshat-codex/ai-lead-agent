"""Phase 9 — Decision-Maker Discovery.

Finds candidate people for an already-resolved canonical company (Phase 7),
using the allowed_titles from a CanonicalICP (Phase 2) to build the search.
Titles are never hard-coded here — the same function works for any ICP's
title list, however it's populated, and for zero titles (no constraint).

This module knows nothing about identity verification or qualification: it
never imports the Phase 3 Hard ICP Rule Engine and never resolves person
identity (Phase 10). A discovered candidate's presence here means only "a
provider returned it," never "this person is confirmed to hold this title
at this company" — and discovery never second-guesses or filters what a
provider hands back, even if a returned title doesn't exactly match what
was requested (a provider's own judgment about a near-miss is its
business, not something this module overrides).

Deliberately provider-agnostic, the same pattern as
app/services/company_discovery.py: this function only ever calls
provider.run(request) — swapping the mock for a real people-data API is a
change to the registry's contents (app/providers/default_registry.py),
never to this file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
from app.schemas.candidate_person import CandidatePerson, PeopleDiscoveryQuery
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.company_resolution import ExistingCompanyIdentity
from app.schemas.people_discovery import PeopleDiscoveryRunResult, PeopleDiscoveryStatus, ProviderRunOutcome


def build_people_discovery_query(
    icp: CanonicalICP,
    company: ExistingCompanyIdentity,
    limit: int = 20,
) -> PeopleDiscoveryQuery:
    """Translates an arbitrary CanonicalICP's allowed titles + an already-
    resolved company into the PEOPLE_DISCOVERY input shape. No ICP-specific
    branching, and no assumption about which titles exist — whatever the
    ICP's allowed_titles happen to be is passed through unchanged."""
    return PeopleDiscoveryQuery(
        titles=icp.hard_rules.allowed_titles,
        company_domain=company.canonical_domain,
        company_name=company.canonical_name,
        limit=limit,
    )


def _compute_status(outcomes: list[ProviderRunOutcome]) -> PeopleDiscoveryStatus:
    if not outcomes:
        return PeopleDiscoveryStatus.FAILED
    if all(outcome.success for outcome in outcomes):
        return PeopleDiscoveryStatus.COMPLETED
    if any(outcome.success for outcome in outcomes):
        return PeopleDiscoveryStatus.PARTIAL_FAILURE
    return PeopleDiscoveryStatus.FAILED


def run_people_discovery(
    icp: CanonicalICP,
    company: ExistingCompanyIdentity,
    registry: ProviderRegistry,
    limit: int = 20,
    provider_order: tuple[str, ...] | None = None,
) -> PeopleDiscoveryRunResult:
    """Runs one people-discovery pass for `company` under `icp`'s allowed
    titles, across every registered PEOPLE_DISCOVERY provider.

    Pure and DB-free — the caller persists the result. Never raises because
    of a provider failure: adapter.run() already guarantees a clean
    ProviderResponse even when a provider misbehaves (see
    app/providers/base.py), so one bad provider is captured and the rest
    still run.

    `provider_order` is an OPTIONAL, purely additive hook for Phase 27's
    routing layer — see run_company_discovery's own docstring for the
    exact contract (every registered provider is still called exactly
    once; only calling order changes; None preserves prior behavior).
    """
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)

    query = build_people_discovery_query(icp, company, limit=limit)
    request = ProviderRequest(
        capability=ProviderCapability.PEOPLE_DISCOVERY,
        query=query.model_dump(),
        request_id=run_id,
    )

    providers = registry.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)
    if provider_order is not None:
        by_id = {p.provider_id: p for p in providers}
        providers = tuple(by_id[pid] for pid in provider_order if pid in by_id)

    outcomes: list[ProviderRunOutcome] = []
    candidates: list[CandidatePerson] = []

    for provider in providers:
        response = provider.run(request)
        outcomes.append(
            ProviderRunOutcome(
                provider_id=provider.provider_id,
                success=response.success,
                requested=limit,
                returned=len(response.data),
                latency_ms=response.latency_ms,
                error=response.error,
            )
        )
        if not response.success:
            continue  # captured above — never fabricate candidates for a failed provider

        seen_external_ids: set[str] = set()
        for record in response.data:
            if record.external_id in seen_external_ids:
                continue  # exact-duplicate protection within this one provider response only
            seen_external_ids.add(record.external_id)
            candidates.append(
                CandidatePerson(
                    id=str(uuid4()),
                    company_id=company.id,
                    icp_id=icp.icp_id,
                    icp_version=icp.version,
                    provider_id=provider.provider_id,
                    external_id=record.external_id,
                    name=record.name,
                    title=record.attributes.get("title"),
                    attributes=dict(record.attributes),
                    discovered_at=datetime.now(timezone.utc),
                )
            )

    return PeopleDiscoveryRunResult(
        run_id=run_id,
        icp_id=icp.icp_id,
        icp_version=icp.version,
        company_id=company.id,
        started_at=started_at,
        status=_compute_status(outcomes),
        provider_outcomes=tuple(outcomes),
        candidates=tuple(candidates),
    )
