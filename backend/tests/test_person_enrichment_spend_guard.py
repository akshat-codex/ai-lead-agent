"""Phase 4 (AI/UX + live-safety audit) — SAFE SPEND/CALL GUARD regression
tests for app/services/person_enrichment.py::is_entity_mock_sourced.

Root cause: a real, paid PERSON_ENRICHMENT provider (Apollo) could be
invoked against a person whose entire identity was fabricated by
MockPeopleDataProvider (e.g. when APOLLO_API_KEY is set but
UNIPILE_DSN/UNIPILE_ACCOUNT_ID are not, so people-discovery still runs on
the mock) — spending a real API call on a name like "Jane Testperson" that
was never real to begin with. See app/api/people.py::enrich_person for the
call-site guard.
"""
from datetime import datetime, timezone
from uuid import uuid4

from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import NormalizedRecord, ProviderCapability, ProviderRequest, ProviderResponse, SourceMetadata
from app.providers.default_registry import get_provider_registry
from app.providers.mocks import MockPeopleDataProvider
from app.providers.registry import ProviderRegistry
from app.services.person_enrichment import is_entity_mock_sourced


# --- unit level: is_entity_mock_sourced ------------------------------------


def test_all_mock_provider_ids_is_mock_sourced():
    assert is_entity_mock_sourced(["mock-people-data-v1", "mock-people-data-v1"]) is True


def test_one_real_provider_id_is_not_mock_sourced():
    assert is_entity_mock_sourced(["mock-people-data-v1", "fixed-people"]) is False


def test_all_real_provider_ids_is_not_mock_sourced():
    assert is_entity_mock_sourced(["explorium-company-discovery-v1", "apollo-person-enrichment-v1"]) is False


def test_no_evidence_at_all_fails_closed_as_mock_sourced():
    """No provenance to trust either way — refusing to spend a real call
    is the safe default, matching this codebase's HOLD/refuse-over-guess
    discipline elsewhere."""
    assert is_entity_mock_sourced([]) is True


def test_none_provider_id_counts_as_untrustworthy():
    """A record with no source_provider_id at all (e.g. a manually-added
    evidence row) must never accidentally satisfy the "real" bar."""
    assert is_entity_mock_sourced([None]) is True
    assert is_entity_mock_sourced([None, "apollo-person-enrichment-v1"]) is False


# --- API level: enrich_person guard -----------------------------------------


def _icp_payload(name: str) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": [], "geography": [], "min_employees": 1, "max_employees": 10000,
            "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [], "growth_signals": [],
            "marketing_signals": [], "other_preferences": [],
        },
    }


def _create_icp(client, name: str) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name)).json()


class _FixedCompanyProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


class _FixedPersonEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.PERSON_ENRICHMENT})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True,
            data=(NormalizedRecord(external_id="apollo-ext-1", name="Someone", attributes={"email": "someone@x.invalid"}),),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=False),
        )


def _discover_company_and_resolve(client, icp_id: str, provider_id="fixed-company", domain="example-test.invalid") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider(provider_id, [NormalizedRecord(external_id="c-1", name="Example Test Co", attributes={"domain": domain})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _discover_person_via_mock_and_resolve(client, icp_id: str, company_id: str) -> str:
    """Mirrors real production wiring: MockPeopleDataProvider is exactly
    what app/providers/default_registry.py registers for PEOPLE_DISCOVERY
    whenever Unipile isn't fully configured — the realistic path to a
    mock-sourced person, not a hand-built test double."""
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        people_run = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_id, "company_id": company_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": people_run["id"]}).json()
    return resolution[0]["canonical_person_id"]


def test_enrich_skips_a_mock_sourced_person_even_when_a_real_provider_is_registered(client):
    icp = _create_icp(client, "Spend Guard A")
    company_id = _discover_company_and_resolve(client, icp["id"])
    person_id = _discover_person_via_mock_and_resolve(client, icp["id"], company_id)

    # A REAL enrichment provider IS registered for this call — the guard
    # must still refuse, since the person's own identity is mock-sourced.
    registry = ProviderRegistry()
    registry.register(_FixedPersonEnrichmentProvider("apollo-person-enrichment-v1"))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post(f"/api/v1/people/{person_id}/enrich")
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "MOCK_SOURCED_SKIPPED"
    assert body["provider_id"] is None

    # No email evidence was ever written — the real provider's execute()
    # was never called.
    evidence = client.get("/api/v1/evidence", params={"entity_type": "PERSON", "entity_id": person_id}).json()
    assert not any(e["field"] == "email" for e in evidence)


def test_enrich_proceeds_normally_for_a_real_sourced_person(client):
    """The guard must never block a genuinely real-sourced person — this
    is the exact scenario test_people_enrich_api.py's own tests already
    cover end-to-end; this test isolates the guard's own pass-through
    behavior specifically."""
    icp = _create_icp(client, "Spend Guard B")
    company_id = _discover_company_and_resolve(client, icp["id"])

    class _FixedPeopleProvider(ProviderAdapter):
        def __init__(self, provider_id: str):
            super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.PEOPLE_DISCOVERY})

        def execute(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                provider_id=self.provider_id, capability=request.capability, success=True,
                data=(NormalizedRecord(external_id="p-1", name="Real Person", attributes={"title": "CMO"}),),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=False),
            )

    registry = ProviderRegistry()
    registry.register(_FixedPeopleProvider("fixed-people"))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        people_run = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": people_run["id"]}).json()
    person_id = resolution[0]["canonical_person_id"]

    registry = ProviderRegistry()
    registry.register(_FixedPersonEnrichmentProvider("apollo-person-enrichment-v1"))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post(f"/api/v1/people/{person_id}/enrich")
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["provider_id"] == "apollo-person-enrichment-v1"
