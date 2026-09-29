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
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _resolve_company(client, icp_id: str, domain: str = "example-test.invalid") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider("fixed-provider", [NormalizedRecord(external_id="ext-1", name="Example Test Co", attributes={"domain": domain})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _add_evidence(client, company_id: str, field: str, value, source_type: str = "search") -> dict:
    return client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY",
            "entity_id": company_id,
            "field": field,
            "value": value,
            "source_type": source_type,
            "retrieved_at": "2026-01-01T00:00:00Z",
        },
    ).json()


def test_extract_signals_from_evidence(client):
    icp = _create_icp(client, "ICP Signal A")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns")

    response = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")
    assert response.status_code == 201
    body = response.json()
    signal = next(s for s in body if s["signal_type"] == "META_ADVERTISING")
    assert signal["status"] == "INSUFFICIENT"
    assert signal["company_id"] == company_id


def test_extract_unknown_company_returns_404(client):
    response = client.post("/api/v1/companies/does-not-exist/extract-commercial-signals")
    assert response.status_code == 404


def test_list_signals_unknown_company_returns_404(client):
    response = client.get("/api/v1/companies/does-not-exist/commercial-signals")
    assert response.status_code == 404


def test_no_evidence_produces_no_signals(client):
    icp = _create_icp(client, "ICP Signal B")
    company_id = _resolve_company(client, icp["id"])
    response = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")
    assert response.json() == []


def test_signal_history_is_append_only(client):
    icp = _create_icp(client, "ICP Signal C")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns")

    first = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals").json()
    second = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals").json()

    first_ids = {s["id"] for s in first}
    second_ids = {s["id"] for s in second}
    assert first_ids.isdisjoint(second_ids)  # every extraction pass inserts fresh rows

    history = client.get(f"/api/v1/companies/{company_id}/commercial-signals").json()
    assert len(history) == len(first) + len(second)


def test_new_evidence_adds_new_signal_without_deleting_old_ones(client):
    icp = _create_icp(client, "ICP Signal D")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns")
    client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")

    _add_evidence(client, company_id, "products_services", "Also expanding into retail")
    client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")

    history = client.get(f"/api/v1/companies/{company_id}/commercial-signals").json()
    signal_types = {s["signal_type"] for s in history}
    assert {"META_ADVERTISING", "RETAIL_EXPANSION"} <= signal_types


def test_filter_by_signal_type(client):
    icp = _create_icp(client, "ICP Signal E")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns and a subscription model")
    client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")

    response = client.get(f"/api/v1/companies/{company_id}/commercial-signals", params={"signal_type": "SUBSCRIPTION"})
    body = response.json()
    assert len(body) == 1
    assert body[0]["signal_type"] == "SUBSCRIPTION"


def test_conflicting_evidence_reported_through_the_api(client):
    icp = _create_icp(client, "ICP Signal F")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "business_model", "Our subscription model drives revenue")
    _add_evidence(client, company_id, "business_model", "We have no subscription — one-time purchase only")

    response = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")
    signal = next(s for s in response.json() if s["signal_type"] == "SUBSCRIPTION")
    assert signal["status"] == "CONFLICT"
    assert len(signal["evidence_ids"]) == 2


def test_reusable_across_multiple_icps(client):
    icp_a = _create_icp(client, "ICP Signal G-A")
    icp_b = _create_icp(client, "ICP Signal G-B")
    company_id = _resolve_company(client, icp_a["id"], domain="shared-signal-co.invalid")
    _resolve_company(client, icp_b["id"], domain="shared-signal-co.invalid")  # resolves to the same company

    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns")
    client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")

    history = client.get(f"/api/v1/companies/{company_id}/commercial-signals").json()
    assert len(history) == 1  # one shared signal history, not per-ICP


def test_no_scoring_or_qualification_fields_on_the_response(client):
    icp = _create_icp(client, "ICP Signal H")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns")
    body = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals").json()

    assert set(body[0].keys()) == {
        "id",
        "company_id",
        "signal_type",
        "status",
        "confidence",
        "value",
        "provider_ids",
        "evidence_ids",
        "first_seen",
        "last_seen",
        "explanation",
        "created_at",
    }


def test_evidence_ids_reference_real_evidence(client):
    icp = _create_icp(client, "ICP Signal I")
    company_id = _resolve_company(client, icp["id"])
    _add_evidence(client, company_id, "products_services", "We run meta advertising campaigns")
    body = client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals").json()

    all_evidence = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    all_ids = {e["id"] for e in all_evidence}
    for signal in body:
        assert set(signal["evidence_ids"]) <= all_ids
