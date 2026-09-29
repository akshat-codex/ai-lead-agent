"""Phase 8 — Company Enrichment.

Gathers structured facts about an already-resolved canonical company (Phase
7) from whatever providers support COMPANY_ENRICHMENT. This module never
decides company identity — that's Phase 7's job, already done before this
ever runs — and never qualifies, scores, or classifies fit, which belong to
later phases. It only collects facts and their provenance.

When providers disagree on a field's value, every value is kept and the
field is flagged as a conflict; nothing here silently picks a winner (see
docs and the task's own employee-range example). A field no provider
returned is simply absent from the result — "unknown" is represented by
absence, never a guessed value.

Pure and DB-free, like Phase 6's run_company_discovery: this function only
ever calls `provider.run(request)`, so swapping a mock for a real
enrichment API is a change to the registry's contents
(app/providers/default_registry.py), never to this file.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
from app.schemas.company_enrichment import (
    CompanyEnrichmentQuery,
    EnrichmentFact,
    EnrichmentField,
    EnrichmentResult,
    EnrichmentRunStatus,
    ProviderEnrichmentOutcome,
)
from app.schemas.company_resolution import ExistingCompanyIdentity


def build_enrichment_query(company: ExistingCompanyIdentity, provider_id: str) -> CompanyEnrichmentQuery:
    """Builds the per-provider query for one company.

    external_id is populated only if *this specific* provider already has a
    recorded identity for the company (Phase 7's provider_identities) —
    never borrowed from a different provider's id, which would be
    meaningless.
    """
    return CompanyEnrichmentQuery(
        domain=company.canonical_domain,
        company_name=company.canonical_name,
        external_id=company.provider_identities.get(provider_id),
    )


def _compute_status(outcomes: list[ProviderEnrichmentOutcome]) -> EnrichmentRunStatus:
    if not outcomes:
        return EnrichmentRunStatus.FAILED
    if all(outcome.success for outcome in outcomes):
        return EnrichmentRunStatus.COMPLETED
    if any(outcome.success for outcome in outcomes):
        return EnrichmentRunStatus.PARTIAL_FAILURE
    return EnrichmentRunStatus.FAILED


def _value_key(value: object) -> str:
    """A stable, order-independent key for comparing fact values (which may
    be dicts/lists, and so not directly hashable)."""
    return json.dumps(value, sort_keys=True, default=str)


def group_facts_by_field(facts: list[EnrichmentFact]) -> tuple[EnrichmentField, ...]:
    by_field: dict[str, list[EnrichmentFact]] = defaultdict(list)
    for fact in facts:
        by_field[fact.field].append(fact)

    fields = []
    for field_name, field_facts in by_field.items():
        distinct_values = {_value_key(fact.value) for fact in field_facts}
        fields.append(EnrichmentField(field=field_name, facts=tuple(field_facts), conflict=len(distinct_values) > 1))
    return tuple(fields)


def run_company_enrichment(
    company: ExistingCompanyIdentity,
    registry: ProviderRegistry,
    provider_order: tuple[str, ...] | None = None,
) -> EnrichmentResult:
    """Runs one enrichment pass for `company` across every registered
    COMPANY_ENRICHMENT provider.

    Never raises because of a provider failure: adapter.run() already
    guarantees a clean ProviderResponse even when a provider misbehaves
    (see app/providers/base.py) — one bad provider is captured and the rest
    still run.

    `provider_order` is an OPTIONAL, purely additive hook for Phase 27's
    routing layer — see run_company_discovery's own docstring for the
    exact contract (every registered provider is still called exactly
    once; only calling order changes; None preserves prior behavior).
    """
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)

    providers = registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)
    if provider_order is not None:
        by_id = {p.provider_id: p for p in providers}
        providers = tuple(by_id[pid] for pid in provider_order if pid in by_id)

    outcomes: list[ProviderEnrichmentOutcome] = []
    all_facts: list[EnrichmentFact] = []

    for provider in providers:
        query = build_enrichment_query(company, provider.provider_id)
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_ENRICHMENT,
            query=query.model_dump(),
            request_id=run_id,
        )
        response = provider.run(request)

        fields_returned = 0
        if response.success:
            retrieved_at = response.source.retrieved_at if response.source else datetime.now(timezone.utc)
            for record in response.data:
                for field, value in record.attributes.items():
                    all_facts.append(
                        EnrichmentFact(
                            field=field,
                            value=value,
                            provider_id=provider.provider_id,
                            external_id=record.external_id,
                            retrieved_at=retrieved_at,
                            confidence=None,  # mocks don't supply one — never invented here
                        )
                    )
                    fields_returned += 1

        outcomes.append(
            ProviderEnrichmentOutcome(
                provider_id=provider.provider_id,
                success=response.success,
                fields_returned=fields_returned,
                latency_ms=response.latency_ms,
                error=response.error,
            )
        )

    return EnrichmentResult(
        run_id=run_id,
        company_id=company.id,
        started_at=started_at,
        status=_compute_status(outcomes),
        provider_outcomes=tuple(outcomes),
        fields=group_facts_by_field(all_facts),
    )
