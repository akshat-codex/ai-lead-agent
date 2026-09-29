"""Phase 6 — Candidate Company Discovery.

Finds candidate companies for a CanonicalICP using whatever providers the
registry has registered for COMPANY_DISCOVERY. This module knows nothing
about qualification: it never imports the Phase 3 Hard ICP Rule Engine, and
a discovered candidate's presence here means only "a provider returned it,"
never "it matches the ICP." Per docs/architecture.md's pipeline, discovery
happens strictly before hard-rule validation, business-model
classification, scoring, or any other qualification step.

Also deliberately provider-agnostic: this function only ever calls
`provider.run(request)` — the exact call it would make against a real
provider later. Swapping the mock for a real company-data API is a change
to the registry's contents (see app/providers/default_registry.py), never
to this file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
from app.schemas.candidate_company import CandidateCompany, CompanyDiscoveryQuery
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.discovery import DiscoveryRunResult, DiscoveryStatus, ProviderRunOutcome


def build_company_discovery_query(icp: CanonicalICP, limit: int = 20) -> CompanyDiscoveryQuery:
    """Translates an arbitrary CanonicalICP's hard rules into the
    COMPANY_DISCOVERY input shape. No ICP-specific branching — the same
    mapping runs for every ICP, however its rules are populated."""
    hard = icp.hard_rules
    return CompanyDiscoveryQuery(
        industries=hard.industries,
        geography_codes=tuple(entry.code for entry in hard.geography.countries),
        geography_unrecognized=hard.geography.unrecognized,
        company_types=hard.company_types,
        allowed_titles=hard.allowed_titles,
        min_employees=hard.employee_range.min,
        max_employees=hard.employee_range.max,
        limit=limit,
        combination_terms=hard.industry_combination_terms,
    )


def _compute_status(outcomes: list[ProviderRunOutcome]) -> DiscoveryStatus:
    if not outcomes:
        return DiscoveryStatus.FAILED
    if all(outcome.success for outcome in outcomes):
        return DiscoveryStatus.COMPLETED
    if any(outcome.success for outcome in outcomes):
        return DiscoveryStatus.PARTIAL_FAILURE
    return DiscoveryStatus.FAILED


def run_company_discovery(
    icp: CanonicalICP,
    registry: ProviderRegistry,
    limit: int = 20,
    provider_order: tuple[str, ...] | None = None,
    cursors: dict[str, str] | None = None,
    term_origin: dict[str, str] | None = None,
) -> DiscoveryRunResult:
    """Runs one discovery pass for `icp` across every registered
    COMPANY_DISCOVERY provider.

    Pure and DB-free — the caller is responsible for persisting the result.
    Never raises because of a provider failure: adapter.run() already
    guarantees a clean ProviderResponse even when a provider misbehaves
    (see app/providers/base.py), so one bad provider can never abort the
    run; its failure is captured and the rest continue.

    `provider_order` is an OPTIONAL, purely additive hook for Phase 27's
    routing layer: when given, it's a tuple of provider_ids (normally
    produced by app/services/provider_routing.route_providers()) that
    re-orders which registered COMPANY_DISCOVERY provider is called
    first — every provider the registry returned is still called exactly
    once, nothing is skipped or added, only the calling order changes.
    Defaulting to None preserves this function's exact prior behavior for
    every existing caller.

    `cursors` is an OPTIONAL, purely additive continuation map keyed by
    provider_id (see app/providers/contracts.py::ProviderRequest.cursor) —
    when a provider_id is present, that provider's ProviderRequest carries
    its stored cursor so it continues a prior query instead of restarting
    from page 1; a provider with no entry gets `cursor=None`, i.e. "first
    page," exactly as before this parameter existed. Every provider's
    returned cursor/exhausted signal is collected into the result's
    `next_cursors`/`exhausted_providers` regardless of whether `cursors`
    was passed in, so a caller can always read back fresh continuation
    state after any call, first-page or not.

    `term_origin` is an OPTIONAL, purely additive provenance map (see
    app/services/discovery_strategy.py::build_term_origin_map) — the SAME
    "optional hook, default preserves exact prior behavior" shape as
    provider_order/cursors above. Passed straight through into
    ProviderRequest.query["term_origin"]; this function never reads or
    interprets it itself, and CompanyDiscoveryQuery's own schema is
    untouched by it (it is added to the request dict alongside the
    query's own fields, never becoming one of them) — see
    app/providers/explorium.py::execute()'s own term_origin comment for
    how a provider may optionally use it.
    """
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)

    query = build_company_discovery_query(icp, limit=limit)

    providers = registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
    if provider_order is not None:
        by_id = {p.provider_id: p for p in providers}
        providers = tuple(by_id[pid] for pid in provider_order if pid in by_id)

    outcomes: list[ProviderRunOutcome] = []
    candidates: list[CandidateCompany] = []
    next_cursors: dict[str, str] = {}
    exhausted_providers: list[str] = []

    for provider in providers:
        request_query = query.model_dump()
        if term_origin:
            request_query["term_origin"] = term_origin
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_DISCOVERY,
            query=request_query,
            request_id=run_id,
            cursor=(cursors or {}).get(provider.provider_id),
        )
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

        if response.cursor:
            next_cursors[provider.provider_id] = response.cursor
        if response.exhausted:
            exhausted_providers.append(provider.provider_id)

        seen_external_ids: set[str] = set()
        for record in response.data:
            if record.external_id in seen_external_ids:
                # Exact-duplicate protection within this one provider
                # response only — cross-provider/cross-run identity
                # resolution is Phase 7's job, not this one's.
                continue
            seen_external_ids.add(record.external_id)
            candidates.append(
                CandidateCompany(
                    id=str(uuid4()),
                    icp_id=icp.icp_id,
                    icp_version=icp.version,
                    provider_id=provider.provider_id,
                    external_id=record.external_id,
                    name=record.name,
                    domain=record.attributes.get("domain"),
                    attributes=dict(record.attributes),
                    discovered_at=datetime.now(timezone.utc),
                )
            )

    return DiscoveryRunResult(
        run_id=run_id,
        icp_id=icp.icp_id,
        icp_version=icp.version,
        started_at=started_at,
        status=_compute_status(outcomes),
        provider_outcomes=tuple(outcomes),
        candidates=tuple(candidates),
        next_cursors=next_cursors,
        exhausted_providers=tuple(exhausted_providers),
    )
