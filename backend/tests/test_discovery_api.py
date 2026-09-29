from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import ProviderCapability, ProviderRequest, ProviderResponse
from app.providers.default_registry import get_provider_registry
from app.providers.mocks import MockCompanyDataProvider
from app.providers.registry import ProviderRegistry


def _icp_payload(name: str = "D2C Skincare") -> dict:
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


def _create_icp(client, name: str = "D2C Skincare") -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name)).json()


class _BrokenCompanyProvider(ProviderAdapter):
    def __init__(self, provider_id: str = "broken-company-provider"):
        super().__init__(provider_id=provider_id, provider_name="Broken", capabilities={ProviderCapability.COMPANY_DISCOVERY})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("simulated outage")


def test_start_discovery_run_returns_candidates(client):
    icp = _create_icp(client)
    response = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]})
    assert response.status_code == 201

    body = response.json()
    assert body["icp_id"] == icp["id"]
    assert body["icp_version"] == icp["version"]
    assert body["status"] == "COMPLETED"
    assert len(body["candidates"]) == 2
    assert body["candidates"][0]["provider_id"] == "mock-company-data-v1"
    assert body["total_returned"] == 2


def test_get_discovery_run_retrieves_persisted_results(client):
    icp = _create_icp(client)
    created = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()

    fetched = client.get(f"/api/v1/discovery/runs/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_discovery_run_unknown_icp_returns_404(client):
    response = client.post("/api/v1/discovery/runs", json={"icp_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_discovery_run_returns_404(client):
    response = client.get("/api/v1/discovery/runs/does-not-exist")
    assert response.status_code == 404


def test_requested_limit_is_recorded(client):
    icp = _create_icp(client)
    response = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"], "limit": 5})
    body = response.json()
    assert body["requested_limit"] == 5
    assert body["provider_outcomes"][0]["requested"] == 5


def test_discovery_does_not_modify_the_source_icp(client):
    icp = _create_icp(client)
    canonical_before = client.get(f"/api/v1/icps/{icp['id']}/canonical").json()

    client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]})

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    canonical_after = client.get(f"/api/v1/icps/{icp['id']}/canonical").json()
    assert icp_after == icp
    assert canonical_after == canonical_before


def test_multiple_discovery_runs_for_the_same_icp_are_all_preserved(client):
    icp = _create_icp(client)
    first = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    second = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()

    assert first["id"] != second["id"]
    assert client.get(f"/api/v1/discovery/runs/{first['id']}").status_code == 200
    assert client.get(f"/api/v1/discovery/runs/{second['id']}").status_code == 200


def test_partial_failure_is_reported_cleanly_through_the_api(client):
    icp = _create_icp(client)

    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider("healthy-mock"))
    registry.register(_BrokenCompanyProvider())

    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]})
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PARTIAL_FAILURE"
    assert len(body["candidates"]) == 2  # from the healthy mock only
    outcomes = {o["provider_id"]: o for o in body["provider_outcomes"]}
    assert outcomes["healthy-mock"]["success"] is True
    assert outcomes["broken-company-provider"]["success"] is False
    assert outcomes["broken-company-provider"]["error_message"]


def test_all_providers_failing_returns_201_with_failed_status_not_a_500(client):
    icp = _create_icp(client)

    registry = ProviderRegistry()
    registry.register(_BrokenCompanyProvider())

    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]})
    finally:
        del app.dependency_overrides[get_provider_registry]

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["candidates"] == []


def test_invalid_limit_rejected(client):
    icp = _create_icp(client)
    response = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"], "limit": 0})
    assert response.status_code == 422
