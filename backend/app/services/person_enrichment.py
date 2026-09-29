"""Phase 5 (Contact Enrichment) — Person Enrichment.

Gathers contact facts about an already-resolved canonical person from
whatever providers support PERSON_ENRICHMENT. Mirrors
app/services/company_enrichment.py's structure (pure, DB-free, calls only
provider.run()) with one difference: it returns raw (field, value,
provider_id, external_id, retrieved_at) tuples for the caller to persist as
EvidenceCreate rows, rather than a company-style EnrichmentFact list, since
person facts live in the entity-agnostic evidence table
(app/schemas/evidence.py), not a dedicated fact table.

When no PERSON_ENRICHMENT provider is registered at all — the expected state
whenever APOLLO_API_KEY is unset — this returns UNAVAILABLE, never a fake
success and never an HTTP error.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
from app.schemas.person_enrichment import PersonEnrichmentOutcome, PersonEnrichmentQuery, PersonEnrichmentRunStatus

# Providers whose id starts with this prefix are mock/scaffolding only
# (app/providers/mocks.py). MockPeopleDataProvider declares PERSON_ENRICHMENT
# as a capability for Phase 9 scaffolding purposes but never implements real
# enrichment — treating it as a usable PERSON_ENRICHMENT provider here would
# let the UI show a fake "Enriched" status backed by discovery-shaped mock
# data pretending to be a real contact-enrichment result.
_MOCK_PROVIDER_PREFIX = "mock-"


class PersonEnrichmentFact:
    """One (field, value) pair as reported by exactly one provider, ready to
    become an EvidenceCreate row. Not a pydantic model — purely an internal
    carrier between this service and the API layer that persists it."""

    __slots__ = ("field", "value", "provider_id", "external_id", "retrieved_at")

    def __init__(self, field: str, value: object, provider_id: str, external_id: str | None, retrieved_at: datetime):
        self.field = field
        self.value = value
        self.provider_id = provider_id
        self.external_id = external_id
        self.retrieved_at = retrieved_at


def _real_person_enrichment_providers(registry: ProviderRegistry):
    return tuple(
        p
        for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)
        if not p.provider_id.startswith(_MOCK_PROVIDER_PREFIX)
    )


def is_entity_mock_sourced(source_provider_ids: list[str | None]) -> bool:
    """Phase 4 (AI/UX + live-safety audit) — SAFE SPEND/CALL GUARD.

    True when every evidence record an entity has traces back to a
    mock-prefixed provider (see _MOCK_PROVIDER_PREFIX above) — i.e. this
    person/company was never actually seen by a real discovery source at
    all, only by app/providers/mocks.py's fixed, fabricated payloads. This
    matters independently of whether a REAL PERSON_ENRICHMENT provider is
    currently registered: a live Apollo key configured while
    COMPANY_DISCOVERY/PEOPLE_DISCOVERY still run on mocks (e.g. Unipile
    not fully configured — see app/providers/default_registry.py's own
    "all three required together" gate) would otherwise let a real, paid
    API call run against a fabricated name/company ("Jane Testperson" /
    "example-test.invalid") with zero chance of returning anything
    meaningful.

    Fails closed by design: an entity with NO evidence at all
    (source_provider_ids == []) is also treated as mock-sourced (returns
    True) — there is no real provenance to trust either way, and refusing
    to spend a real call is the safe default, matching this codebase's
    "HOLD/refuse over guess" discipline elsewhere (e.g.
    app/services/hard_rule_engine.py's own HOLD-on-unknown rule). An
    entity with even ONE non-mock evidence record is real-sourced (a real
    discovery provider genuinely observed this entity at least once) and
    is never blocked by this guard.

    Pure and DB-free, like every other function in this module — the
    caller (app/api/people.py) owns loading the evidence rows and passing
    their source_provider_id values in."""
    has_confirmed_real_source = any(
        provider_id is not None and not provider_id.startswith(_MOCK_PROVIDER_PREFIX) for provider_id in source_provider_ids
    )
    return not has_confirmed_real_source


def run_person_enrichment(
    person_id: str,
    query: PersonEnrichmentQuery,
    registry: ProviderRegistry,
) -> tuple[PersonEnrichmentOutcome, list[PersonEnrichmentFact]]:
    """Runs one enrichment pass for `person_id` across every registered
    real (non-mock) PERSON_ENRICHMENT provider.

    Returns the outcome plus the facts to persist — the caller (the API
    handler) owns the DB session and turns each fact into an EvidenceCreate
    row via the existing app/api/evidence.py persistence helpers, so
    idempotency/duplicate-detection stays identical to every other evidence
    writer in the codebase.
    """
    providers = _real_person_enrichment_providers(registry)
    if not providers:
        return (
            PersonEnrichmentOutcome(person_id=person_id, status=PersonEnrichmentRunStatus.UNAVAILABLE),
            [],
        )

    facts: list[PersonEnrichmentFact] = []
    any_success = False
    any_failure = False
    last_provider_id: str | None = None
    last_error = None

    for provider in providers:
        request = ProviderRequest(
            capability=ProviderCapability.PERSON_ENRICHMENT,
            query=query.model_dump(),
        )
        response = provider.run(request)
        last_provider_id = provider.provider_id

        if response.success:
            any_success = True
            retrieved_at = response.source.retrieved_at if response.source else datetime.now(timezone.utc)
            for record in response.data:
                for field, value in record.attributes.items():
                    facts.append(
                        PersonEnrichmentFact(
                            field=field,
                            value=value,
                            provider_id=provider.provider_id,
                            external_id=record.external_id or None,
                            retrieved_at=retrieved_at,
                        )
                    )
        else:
            any_failure = True
            last_error = response.error

    if any_success and not any_failure:
        status = PersonEnrichmentRunStatus.COMPLETED
    elif any_success and any_failure:
        status = PersonEnrichmentRunStatus.PARTIAL_FAILURE
    else:
        status = PersonEnrichmentRunStatus.FAILED

    outcome = PersonEnrichmentOutcome(
        person_id=person_id,
        status=status,
        provider_id=last_provider_id,
        error=last_error if not any_success else None,
        fields_returned=len(facts),
    )
    return outcome, facts
