from datetime import datetime, timezone

from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.default_registry import get_provider_registry
from app.providers.mocks import MockCompanyDataProvider, MockCompanyRegistryProvider
from app.providers.registry import ProviderRegistry


def _icp_payload(name: str) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare"],
            "geography": ["United States"],
            "min_employees": 10,
            "max_employees": 200,
            "allowed_titles": ["CMO"],
            "company_type": ["D2C"],
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [],
            "commercial_signals": [],
            "growth_signals": [],
            "marketing_signals": [],
            "other_preferences": [],
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
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(self._records),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc), is_mock=True,
            ),
        )


def _discover_and_resolve_one_company(client, icp_id: str, domain: str = "example-test.invalid") -> str:
    """Helper: gets a real, persisted canonical company id via the actual
    discovery -> resolution pipeline, so enrichment tests operate on a
    genuinely resolved company rather than a hand-inserted row."""
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider("fixed-provider", [NormalizedRecord(external_id="ext-1", name="Example Test Co", attributes={"domain": domain})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]

    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


class _BrokenEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str = "broken-enrichment"):
        super().__init__(provider_id=provider_id, provider_name="Broken", capabilities={ProviderCapability.COMPANY_ENRICHMENT})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("vendor outage")


# --- basic enrichment flow -------------------------------------------


def test_enrich_company_returns_structured_facts(client):
    icp = _create_icp(client, "ICP Enrich A")
    company_id = _discover_and_resolve_one_company(client, icp["id"])

    response = client.post(f"/api/v1/companies/{company_id}/enrich")
    assert response.status_code == 201
    body = response.json()
    assert body["company_id"] == company_id
    assert body["status"] == "COMPLETED"
    assert any(o["provider_id"] == "mock-company-data-v1" for o in body["provider_outcomes"])


def test_enrich_unknown_company_returns_404(client):
    response = client.post("/api/v1/companies/does-not-exist/enrich")
    assert response.status_code == 404


def test_get_enrichment_run_retrieves_the_persisted_run(client):
    icp = _create_icp(client, "ICP Enrich B")
    company_id = _discover_and_resolve_one_company(client, icp["id"])
    run = client.post(f"/api/v1/companies/{company_id}/enrich").json()

    fetched = client.get(f"/api/v1/companies/{company_id}/enrichment-runs/{run['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == run


def test_get_unknown_enrichment_run_returns_404(client):
    icp = _create_icp(client, "ICP Enrich C")
    company_id = _discover_and_resolve_one_company(client, icp["id"])
    response = client.get(f"/api/v1/companies/{company_id}/enrichment-runs/does-not-exist")
    assert response.status_code == 404


# --- facts retrieval -----------------------------------------------------


def test_get_company_facts_returns_grouped_fields(client):
    icp = _create_icp(client, "ICP Facts A")
    company_id = _discover_and_resolve_one_company(client, icp["id"])
    client.post(f"/api/v1/companies/{company_id}/enrich")

    response = client.get(f"/api/v1/companies/{company_id}/facts")
    assert response.status_code == 200
    body = response.json()
    assert body["company_id"] == company_id
    field_names = {f["field"] for f in body["fields"]}
    assert "industry" in field_names
    assert "employee_range" in field_names


def test_facts_unknown_company_returns_404(client):
    response = client.get("/api/v1/companies/does-not-exist/facts")
    assert response.status_code == 404


def test_facts_accumulate_across_multiple_enrichment_runs(client):
    icp = _create_icp(client, "ICP Facts B")
    company_id = _discover_and_resolve_one_company(client, icp["id"])

    # The default registry has two enrichment-capable mocks, so each run
    # contributes one "industry" fact per provider.
    client.post(f"/api/v1/companies/{company_id}/enrich")
    client.post(f"/api/v1/companies/{company_id}/enrich")

    facts = client.get(f"/api/v1/companies/{company_id}/facts").json()
    industry_field = next(f for f in facts["fields"] if f["field"] == "industry")
    assert len(industry_field["facts"]) == 4  # both runs' facts retained, not deduplicated


# --- conflicting values via the API ----------------------------------


def test_conflicting_values_across_providers_are_visible_via_the_api(client):
    icp = _create_icp(client, "ICP Conflict")
    company_id = _discover_and_resolve_one_company(client, icp["id"])

    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        client.post(f"/api/v1/companies/{company_id}/enrich")
    finally:
        del app.dependency_overrides[get_provider_registry]

    facts = client.get(f"/api/v1/companies/{company_id}/facts").json()
    employee_field = next(f for f in facts["fields"] if f["field"] == "employee_range")
    assert employee_field["conflict"] is True
    assert len(employee_field["facts"]) == 2


# --- provider failure handling through the API -----------------------


def test_partial_provider_failure_is_reported_cleanly_not_as_a_500(client):
    icp = _create_icp(client, "ICP Enrich Failure")
    company_id = _discover_and_resolve_one_company(client, icp["id"])

    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(_BrokenEnrichmentProvider())
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post(f"/api/v1/companies/{company_id}/enrich")
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PARTIAL_FAILURE"
    outcomes = {o["provider_id"]: o for o in body["provider_outcomes"]}
    assert outcomes["broken-enrichment"]["success"] is False
    assert outcomes["broken-enrichment"]["error_message"]


# --- no duplicate company / multi-ICP reuse --------------------------------


def test_enrichment_does_not_create_a_new_company(client):
    icp = _create_icp(client, "ICP No Duplicate")
    company_id = _discover_and_resolve_one_company(client, icp["id"])

    before = len(client.get("/api/v1/companies").json())
    client.post(f"/api/v1/companies/{company_id}/enrich")
    after = len(client.get("/api/v1/companies").json())

    assert before == after


def test_same_company_can_be_enriched_and_used_by_a_second_icp(client):
    icp_a = _create_icp(client, "ICP Multi A")
    icp_b = _create_icp(client, "ICP Multi B")

    company_id_a = _discover_and_resolve_one_company(client, icp_a["id"], domain="shared-co.invalid")
    company_id_b = _discover_and_resolve_one_company(client, icp_b["id"], domain="shared-co.invalid")

    assert company_id_a == company_id_b  # Phase 7 already guarantees this; sanity check

    client.post(f"/api/v1/companies/{company_id_a}/enrich")
    facts = client.get(f"/api/v1/companies/{company_id_a}/facts").json()
    assert len(facts["fields"]) > 0

    companies = client.get("/api/v1/companies").json()
    assert len(companies) == 1


# --- discovery/resolution history untouched --------------------------


def test_enrichment_does_not_modify_resolution_records(client):
    icp = _create_icp(client, "ICP Enrich History")
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider("fixed-provider", [NormalizedRecord(external_id="ext-1", name="Example Test Co", attributes={"domain": "history-co.invalid"})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution_before = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()

    company_id = resolution_before[0]["canonical_company_id"]
    client.post(f"/api/v1/companies/{company_id}/enrich")

    resolution_after = client.get("/api/v1/companies/resolutions", params={"discovery_run_id": run["id"]}).json()
    assert resolution_after == resolution_before
