"""Unit tests for app/services/person_enrichment.py's run_person_enrichment.

Follows test_provider_registry.py's convention of injecting fake
ProviderAdapter instances rather than mocking HTTP — this module never
imports httpx itself, it only calls provider.run().
"""
from datetime import datetime, timezone

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.registry import ProviderRegistry
from app.schemas.person_enrichment import PersonEnrichmentQuery, PersonEnrichmentRunStatus
from app.services.person_enrichment import run_person_enrichment


class _StubPersonEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str, attributes: dict, external_id: str = "ext-1"):
        super().__init__(provider_id=provider_id, provider_name="Stub", capabilities={ProviderCapability.PERSON_ENRICHMENT})
        self._attributes = attributes
        self._external_id = external_id

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(NormalizedRecord(external_id=self._external_id, name="Someone", attributes=self._attributes),),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc), is_mock=False,
            ),
        )


class _BrokenPersonEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str = "broken-enrichment"):
        super().__init__(provider_id=provider_id, provider_name="Broken", capabilities={ProviderCapability.PERSON_ENRICHMENT})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("vendor outage")


def _query() -> PersonEnrichmentQuery:
    return PersonEnrichmentQuery(full_name="Jane Testperson")


def test_no_provider_registered_returns_unavailable_not_failure():
    registry = ProviderRegistry()
    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert outcome.status == PersonEnrichmentRunStatus.UNAVAILABLE
    assert facts == []
    assert outcome.provider_id is None


def test_mock_only_registry_is_also_unavailable_never_a_fake_success():
    registry = ProviderRegistry()
    registry.register(_StubPersonEnrichmentProvider("mock-people-data-v1", {"title": "CMO"}))

    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert outcome.status == PersonEnrichmentRunStatus.UNAVAILABLE
    assert facts == []


def test_successful_real_provider_produces_expected_facts():
    registry = ProviderRegistry()
    registry.register(
        _StubPersonEnrichmentProvider(
            "apollo-person-enrichment-v1",
            {"title": "Head of Growth", "email": "jane@x.invalid"},
        )
    )

    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert outcome.status == PersonEnrichmentRunStatus.COMPLETED
    assert outcome.provider_id == "apollo-person-enrichment-v1"
    fields = {f.field: f.value for f in facts}
    assert fields == {"title": "Head of Growth", "email": "jane@x.invalid"}
    assert all(f.provider_id == "apollo-person-enrichment-v1" for f in facts)


def test_provider_failure_produces_failed_status_and_no_fabricated_facts():
    registry = ProviderRegistry()
    registry.register(_BrokenPersonEnrichmentProvider("apollo-person-enrichment-v1"))

    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert outcome.status == PersonEnrichmentRunStatus.FAILED
    assert facts == []
    assert outcome.error is not None


def test_one_provider_failing_does_not_block_a_second_working_provider():
    registry = ProviderRegistry()
    registry.register(_BrokenPersonEnrichmentProvider("broken-provider"))
    registry.register(
        _StubPersonEnrichmentProvider("apollo-person-enrichment-v1", {"title": "CEO"}, external_id="ext-2")
    )

    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert outcome.status == PersonEnrichmentRunStatus.PARTIAL_FAILURE
    assert len(facts) == 1
    assert facts[0].field == "title"
    assert facts[0].value == "CEO"


def test_facts_carry_external_id_and_retrieved_at_for_provenance():
    registry = ProviderRegistry()
    registry.register(
        _StubPersonEnrichmentProvider("apollo-person-enrichment-v1", {"email": "jane@x.invalid"}, external_id="apollo-42")
    )

    _, facts = run_person_enrichment("person-1", _query(), registry)

    assert facts[0].external_id == "apollo-42"
    assert facts[0].retrieved_at is not None
