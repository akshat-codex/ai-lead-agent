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
from app.providers.registry import ProviderRegistry


def _icp_payload(name: str, allowed_titles: list[str]) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare"],
            "geography": ["United States"],
            "min_employees": 10,
            "max_employees": 200,
            "allowed_titles": allowed_titles,
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


def _create_icp(client, name: str, allowed_titles: list[str]) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name, allowed_titles)).json()


class _FixedCompanyProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


class _FixedPeopleProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord], should_fail: bool = False):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.PEOPLE_DISCOVERY})
        self._records = records
        self._should_fail = should_fail

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if self._should_fail:
            raise RuntimeError("simulated outage")
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _resolve_a_company(client, icp_id: str, domain: str = "example-test.invalid") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider("fixed-company-provider", [NormalizedRecord(external_id="ext-1", name="Example Test Co", attributes={"domain": domain})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


# --- basic flow ------------------------------------------------------


def test_start_people_discovery_returns_candidates(client):
    icp = _create_icp(client, "ICP People A", ["CMO"])
    company_id = _resolve_a_company(client, icp["id"])

    response = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id})
    assert response.status_code == 201
    body = response.json()
    assert body["icp_id"] == icp["id"]
    assert body["company_id"] == company_id
    assert body["status"] == "COMPLETED"
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["title"] == "CMO"
    assert body["candidates"][0]["company_id"] == company_id


def test_different_icps_return_different_title_filtered_candidates(client):
    icp_growth = _create_icp(client, "ICP People Growth", ["Head of Growth"])
    icp_cmo = _create_icp(client, "ICP People CMO", ["CMO"])
    company_id = _resolve_a_company(client, icp_growth["id"])

    growth_result = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_growth["id"], "company_id": company_id}).json()
    cmo_result = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_cmo["id"], "company_id": company_id}).json()

    assert [c["name"] for c in growth_result["candidates"]] == ["Jane Testperson"]
    assert [c["name"] for c in cmo_result["candidates"]] == ["Alex Sampleuser"]


def test_get_run_retrieves_persisted_results(client):
    icp = _create_icp(client, "ICP People B", [])
    company_id = _resolve_a_company(client, icp["id"])
    created = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id}).json()

    fetched = client.get(f"/api/v1/people-discovery/runs/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_unknown_icp_returns_404(client):
    icp = _create_icp(client, "ICP People C", [])
    company_id = _resolve_a_company(client, icp["id"])
    response = client.post("/api/v1/people-discovery/runs", json={"icp_id": "does-not-exist", "company_id": company_id})
    assert response.status_code == 404


def test_unknown_company_returns_404(client):
    icp = _create_icp(client, "ICP People D", [])
    response = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_run_returns_404(client):
    response = client.get("/api/v1/people-discovery/runs/does-not-exist")
    assert response.status_code == 404


# --- multiple legitimate people for one company -----------------------


def test_multiple_legitimate_people_for_one_company(client):
    icp = _create_icp(client, "ICP People Multi", ["Founder", "CMO"])
    company_id = _resolve_a_company(client, icp["id"])

    registry = ProviderRegistry()
    registry.register(_FixedPeopleProvider("provider-a", [
        NormalizedRecord(external_id="p1", name="Founder Person", attributes={"title": "Founder"}),
        NormalizedRecord(external_id="p2", name="Marketing Person", attributes={"title": "CMO"}),
    ]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id})
    finally:
        del app.dependency_overrides[get_provider_registry]

    body = response.json()
    assert len(body["candidates"]) == 2
    assert {c["title"] for c in body["candidates"]} == {"Founder", "CMO"}


# --- failure handling via the API --------------------------------------


def test_partial_provider_failure_reported_cleanly_not_as_a_500(client):
    icp = _create_icp(client, "ICP People Failure", [])
    company_id = _resolve_a_company(client, icp["id"])

    registry = ProviderRegistry()
    registry.register(_FixedPeopleProvider("healthy", [NormalizedRecord(external_id="1", name="Healthy Person", attributes={"title": "CEO"})]))
    registry.register(_FixedPeopleProvider("broken", [], should_fail=True))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id})
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PARTIAL_FAILURE"
    assert len(body["candidates"]) == 1
    outcomes = {o["provider_id"]: o for o in body["provider_outcomes"]}
    assert outcomes["broken"]["success"] is False
    assert outcomes["broken"]["error_message"]


def test_all_providers_failing_returns_201_with_failed_status(client):
    icp = _create_icp(client, "ICP People All Fail", [])
    company_id = _resolve_a_company(client, icp["id"])

    registry = ProviderRegistry()
    registry.register(_FixedPeopleProvider("broken", [], should_fail=True))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id})
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["candidates"] == []


# --- no duplicate company creation / company entity resolution ------------


def test_people_discovery_does_not_create_or_re_resolve_companies(client):
    icp = _create_icp(client, "ICP People No Duplicate", [])
    company_id = _resolve_a_company(client, icp["id"])

    before = client.get("/api/v1/companies").json()
    client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id})
    after = client.get("/api/v1/companies").json()

    assert before == after


# --- history preserved ------------------------------------------------


def test_multiple_runs_for_the_same_company_are_all_preserved(client):
    icp = _create_icp(client, "ICP People History", [])
    company_id = _resolve_a_company(client, icp["id"])

    first = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id}).json()
    second = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id}).json()

    assert first["id"] != second["id"]
    assert client.get(f"/api/v1/people-discovery/runs/{first['id']}").status_code == 200
    assert client.get(f"/api/v1/people-discovery/runs/{second['id']}").status_code == 200


def test_invalid_limit_rejected(client):
    icp = _create_icp(client, "ICP People Invalid Limit", [])
    company_id = _resolve_a_company(client, icp["id"])
    response = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp["id"], "company_id": company_id, "limit": 0})
    assert response.status_code == 422
