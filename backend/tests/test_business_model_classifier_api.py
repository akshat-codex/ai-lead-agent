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
from app.providers.mocks import MockCompanyDataProvider
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
            "business_model_preferences": ["Subscription"],
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
    registry.register(
        _FixedCompanyProvider(
            "fixed-provider",
            [NormalizedRecord(external_id="ext-1", name="Example Test Co", attributes={"domain": domain})],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _enrich(client, company_id: str) -> None:
    """The default mock's enrichment path reliably returns
    company_type="D2C" — used to give the classifier a real, existing
    evidence source rather than inventing one just for this test."""
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        client.post(f"/api/v1/companies/{company_id}/enrich")
    finally:
        del app.dependency_overrides[get_provider_registry]


def test_classify_from_enrichment_evidence(client):
    icp = _create_icp(client, "ICP BM A")
    company_id = _resolve_company(client, icp["id"])
    _enrich(client, company_id)
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    response = client.post(f"/api/v1/companies/{company_id}/classify-business-model")
    assert response.status_code == 201
    body = response.json()
    assert body["company_id"] == company_id
    assert body["primary_model"] == "DTC"
    assert body["status"] == "CLASSIFIED"


def test_classify_unknown_company_returns_404(client):
    response = client.post("/api/v1/companies/does-not-exist/classify-business-model")
    assert response.status_code == 404


def test_list_classifications_unknown_company_returns_404(client):
    response = client.get("/api/v1/companies/does-not-exist/business-model-classifications")
    assert response.status_code == 404


def test_classification_with_no_evidence_is_unknown(client):
    icp = _create_icp(client, "ICP BM B")
    company_id = _resolve_company(client, icp["id"])
    # no evidence imported at all
    response = client.post(f"/api/v1/companies/{company_id}/classify-business-model")
    body = response.json()
    assert body["primary_model"] == "UNKNOWN"
    assert body["status"] == "INSUFFICIENT_EVIDENCE"


def test_classification_history_is_preserved_across_calls(client):
    icp = _create_icp(client, "ICP BM C")
    company_id = _resolve_company(client, icp["id"])
    _enrich(client, company_id)
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    first = client.post(f"/api/v1/companies/{company_id}/classify-business-model").json()
    second = client.post(f"/api/v1/companies/{company_id}/classify-business-model").json()

    assert first["id"] != second["id"]  # both preserved, neither overwritten
    history = client.get(f"/api/v1/companies/{company_id}/business-model-classifications").json()
    assert len(history) == 2
    assert {r["id"] for r in history} == {first["id"], second["id"]}


def test_new_evidence_changes_classification_without_deleting_the_old_one(client):
    icp = _create_icp(client, "ICP BM D")
    company_id = _resolve_company(client, icp["id"])  # no company_type evidence yet

    first = client.post(f"/api/v1/companies/{company_id}/classify-business-model").json()
    assert first["primary_model"] == "UNKNOWN"

    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY",
            "entity_id": company_id,
            "field": "company_type",
            "value": "Marketplace",
            "source_type": "search",
            "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    second = client.post(f"/api/v1/companies/{company_id}/classify-business-model").json()
    assert second["primary_model"] == "MARKETPLACE"

    history = client.get(f"/api/v1/companies/{company_id}/business-model-classifications").json()
    assert len(history) == 2
    assert history[0]["primary_model"] == "UNKNOWN"
    assert history[1]["primary_model"] == "MARKETPLACE"


def test_reusable_across_multiple_icps(client):
    """The same canonical company's classification is keyed only by
    company_id — validating it belongs to two different ICPs must not
    produce two different classification histories or require the
    classifier to know about either ICP."""
    icp_a = _create_icp(client, "ICP BM E-A")
    icp_b = _create_icp(client, "ICP BM E-B")
    company_id = _resolve_company(client, icp_a["id"], domain="shared-bm-co.invalid")
    _resolve_company(client, icp_b["id"], domain="shared-bm-co.invalid")  # resolves to the same company

    _enrich(client, company_id)
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    response = client.post(f"/api/v1/companies/{company_id}/classify-business-model")
    assert response.json()["primary_model"] == "DTC"

    history = client.get(f"/api/v1/companies/{company_id}/business-model-classifications").json()
    assert len(history) == 1  # one shared classification history, not per-ICP


def test_no_qualification_fields_on_the_response(client):
    icp = _create_icp(client, "ICP BM F")
    company_id = _resolve_company(client, icp["id"])
    _enrich(client, company_id)
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    body = client.post(f"/api/v1/companies/{company_id}/classify-business-model").json()

    assert set(body.keys()) == {
        "id",
        "company_id",
        "primary_model",
        "secondary_models",
        "status",
        "confidence",
        "supporting_evidence_ids",
        "conflicting_evidence_ids",
        "explanation",
        "classified_at",
    }


def test_supporting_evidence_ids_are_real_evidence_records(client):
    icp = _create_icp(client, "ICP BM G")
    company_id = _resolve_company(client, icp["id"])
    _enrich(client, company_id)
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    body = client.post(f"/api/v1/companies/{company_id}/classify-business-model").json()

    assert len(body["supporting_evidence_ids"]) > 0
    all_evidence = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    all_ids = {e["id"] for e in all_evidence}
    assert set(body["supporting_evidence_ids"]) <= all_ids
