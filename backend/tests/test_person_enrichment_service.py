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


# --- email carry-forward within one enrichment pass -------------------


class _EmailEchoProvider(ProviderAdapter):
    """Records the email it actually received in its query, so a test can
    assert whether an earlier provider's newly-found email was carried
    forward to it — mirrors AbstractEmailVerificationProvider's own real
    "verify, never discover" contract without needing real HTTP."""

    def __init__(self, provider_id: str = "abstract-email-verification-v1"):
        super().__init__(provider_id=provider_id, provider_name="Echo", capabilities={ProviderCapability.PERSON_ENRICHMENT})
        self.received_emails: list[str | None] = []

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        email = request.query.get("email")
        self.received_emails.append(email)
        if not email:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(code="NO_EMAIL", message="no email", retryable=False),
            )
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(NormalizedRecord(external_id=email, name=email, attributes={"email_deliverability": "DELIVERABLE"}),),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc), is_mock=False,
            ),
        )


def test_a_later_providers_query_receives_an_earlier_providers_newly_found_email():
    registry = ProviderRegistry()
    registry.register(_StubPersonEnrichmentProvider("apollo-person-enrichment-v1", {"email": "jane@x.invalid"}))
    echo = _EmailEchoProvider()
    registry.register(echo)

    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert echo.received_emails == ["jane@x.invalid"]
    assert outcome.status == PersonEnrichmentRunStatus.COMPLETED
    fields = {f.field: f.value for f in facts}
    assert fields["email_deliverability"] == "DELIVERABLE"


def test_carry_forward_never_overwrites_an_email_already_supplied_by_the_caller():
    """A caller-supplied query.email (from a prior run's own evidence — see
    app/api/people.py's own _latest_evidence_value use) must win over
    whatever an earlier provider in THIS pass separately returns — the
    already-known value is not replaced mid-pass."""
    registry = ProviderRegistry()
    registry.register(_StubPersonEnrichmentProvider("apollo-person-enrichment-v1", {"email": "new@x.invalid"}))
    echo = _EmailEchoProvider()
    registry.register(echo)

    query = PersonEnrichmentQuery(full_name="Jane Testperson", email="already-known@x.invalid")
    run_person_enrichment("person-1", query, registry)

    assert echo.received_emails == ["already-known@x.invalid"]


def test_no_email_ever_produced_means_a_later_provider_still_runs_with_none():
    registry = ProviderRegistry()
    registry.register(_StubPersonEnrichmentProvider("apollo-person-enrichment-v1", {"title": "CMO"}))
    echo = _EmailEchoProvider()
    registry.register(echo)

    outcome, facts = run_person_enrichment("person-1", _query(), registry)

    assert echo.received_emails == [None]
    assert outcome.status == PersonEnrichmentRunStatus.PARTIAL_FAILURE  # echo failed with NO_EMAIL
    fields = {f.field for f in facts}
    assert fields == {"title"}
