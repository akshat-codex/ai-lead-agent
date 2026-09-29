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


def _icp_payload(
    name: str,
    min_employees: int = 10,
    max_employees: int = 200,
    allowed_titles: list[str] | None = None,
    industry: list[str] | None = None,
    geography: list[str] | None = None,
    company_type: list[str] | None = None,
    business_model_preferences: list[str] | None = None,
    commercial_signals: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare"] if industry is None else industry,
            "geography": ["United States"] if geography is None else geography,
            "min_employees": min_employees,
            "max_employees": max_employees,
            "allowed_titles": [] if allowed_titles is None else allowed_titles,
            "company_type": ["D2C"] if company_type is None else company_type,
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [] if business_model_preferences is None else business_model_preferences,
            "commercial_signals": [] if commercial_signals is None else commercial_signals,
            "growth_signals": [],
            "marketing_signals": [],
            "other_preferences": [],
        },
    }


def _create_icp(client, name: str, **overrides) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name, **overrides)).json()


class _FixedCompanyProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _company_provider(provider_id: str, employee_count: int, domain: str) -> _FixedCompanyProvider:
    return _FixedCompanyProvider(
        provider_id, [NormalizedRecord(external_id="ext-co", name="Example Test Co", attributes={"domain": domain, "employee_count": employee_count})]
    )


def _discover_company(client, icp_id: str, providers: list[ProviderAdapter]) -> str:
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _resolve_two_corroborating_runs(client, icp_id: str, domain: str, employee_count: int = 50) -> str:
    company_id = _discover_company(client, icp_id, [_company_provider("p1", employee_count, domain)])
    registry = ProviderRegistry()
    registry.register(_company_provider("p2", employee_count, domain))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run2 = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]})
    return company_id


def _import_company_evidence(client, company_id: str) -> None:
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})


# --- basic flow: FAIL / HOLD / PASS gating through the real API ------------


def test_hard_fail_yields_null_final_score_through_the_api(client):
    icp = _create_icp(client, "Score API A", max_employees=10)
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "score-api-a.invalid", employee_count=500)
    _import_company_evidence(client, company_id)

    response = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id})
    assert response.status_code == 201
    body = response.json()
    assert body["hard_icp_result"] == "FAIL"
    assert body["eligible_for_scoring"] is False
    assert body["final_score"] is None
    # component scores still visible for diagnostics
    assert body["icp_score"] is not None
    assert body["evidence_score"] is not None


def test_hard_hold_yields_null_final_score_through_the_api(client):
    icp = _create_icp(client, "Score API B")
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", 50, "score-api-b.invalid")])
    _import_company_evidence(client, company_id)  # only a single sighting -> HOLD

    response = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id})
    body = response.json()
    assert body["hard_icp_result"] == "HOLD"
    assert body["eligible_for_scoring"] is False
    assert body["final_score"] is None


def test_hard_pass_produces_a_full_score_through_the_api(client):
    icp = _create_icp(client, "Score API C", industry=[], geography=[], company_type=[])
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "score-api-c.invalid")
    _import_company_evidence(client, company_id)

    response = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id})
    body = response.json()
    assert body["hard_icp_result"] == "PASS"
    assert body["eligible_for_scoring"] is True
    assert body["final_score"] is not None
    assert 0.0 <= body["final_score"] <= 100.0


# --- 404s --------------------------------------------------------------


def test_score_unknown_icp_returns_404(client):
    response = client.post("/api/v1/lead-scores", json={"icp_id": "does-not-exist", "company_id": "x"})
    assert response.status_code == 404


def test_score_unknown_company_returns_404(client):
    icp = _create_icp(client, "Score API D")
    response = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_score_returns_404(client):
    response = client.get("/api/v1/lead-scores/does-not-exist")
    assert response.status_code == 404


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/lead-scores")
    assert response.status_code == 400


# --- multi-ICP isolation and history ------------------------------------


def test_same_company_scored_differently_under_different_icps(client):
    icp_a = _create_icp(client, "Score API E-A", business_model_preferences=["DTC"])
    icp_b = _create_icp(client, "Score API E-B", business_model_preferences=["B2B"])
    company_id = _resolve_two_corroborating_runs(client, icp_a["id"], "score-api-e.invalid")
    _import_company_evidence(client, company_id)

    result_a = client.post("/api/v1/lead-scores", json={"icp_id": icp_a["id"], "company_id": company_id}).json()
    result_b = client.post("/api/v1/lead-scores", json={"icp_id": icp_b["id"], "company_id": company_id}).json()

    assert result_a["id"] != result_b["id"]
    assert result_a["icp_id"] != result_b["icp_id"]

    by_icp_a = client.get("/api/v1/lead-scores", params={"icp_id": icp_a["id"]}).json()
    by_icp_b = client.get("/api/v1/lead-scores", params={"icp_id": icp_b["id"]}).json()
    assert len(by_icp_a) == 1
    assert len(by_icp_b) == 1


def test_repeated_scoring_preserves_history_and_never_overwrites(client):
    icp = _create_icp(client, "Score API F")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "score-api-f.invalid")
    _import_company_evidence(client, company_id)

    first = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id}).json()
    second = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id}).json()

    assert first["id"] != second["id"]
    history = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()
    assert len(history) == 2


def test_scored_result_is_retrievable_by_id(client):
    icp = _create_icp(client, "Score API G")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "score-api-g.invalid")
    _import_company_evidence(client, company_id)

    created = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id}).json()
    fetched = client.get(f"/api/v1/lead-scores/{created['id']}").json()
    assert fetched == created


# --- weights persisted for audit -----------------------------------------


def test_weights_configuration_is_persisted_with_the_score(client):
    icp = _create_icp(client, "Score API H")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "score-api-h.invalid")
    _import_company_evidence(client, company_id)

    body = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id}).json()
    assert body["weights_version"] == "default-v1"
    weight_values = {k: v for k, v in body["weights"].items() if k != "version"}
    assert abs(sum(weight_values.values()) - 1.0) < 1e-6


# --- commercial preference sensitivity through the real pipeline ---------


def test_business_model_classification_and_commercial_signals_feed_into_the_real_score(client):
    icp_pref = _create_icp(client, "Score API I-pref", business_model_preferences=["DTC"])
    icp_no_pref = _create_icp(client, "Score API I-nopref")

    company_pref = _resolve_two_corroborating_runs(client, icp_pref["id"], "score-api-i-pref.invalid")
    company_no_pref = _resolve_two_corroborating_runs(client, icp_no_pref["id"], "score-api-i-nopref.invalid")
    _import_company_evidence(client, company_pref)
    _import_company_evidence(client, company_no_pref)

    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY",
            "entity_id": company_pref,
            "field": "business_model",
            "value": "Direct-to-consumer skincare brand",
            "source_type": "search",
            "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY",
            "entity_id": company_no_pref,
            "field": "business_model",
            "value": "Direct-to-consumer skincare brand",
            "source_type": "search",
            "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    client.post(f"/api/v1/companies/{company_pref}/classify-business-model")
    client.post(f"/api/v1/companies/{company_no_pref}/classify-business-model")

    result_pref = client.post("/api/v1/lead-scores", json={"icp_id": icp_pref["id"], "company_id": company_pref}).json()
    result_no_pref = client.post(
        "/api/v1/lead-scores", json={"icp_id": icp_no_pref["id"], "company_id": company_no_pref}
    ).json()

    assert result_pref["commercial_score"] > result_no_pref["commercial_score"]


# --- no LLM / manager feedback surfaced ------------------------------------


def test_no_manager_feedback_or_qualification_fields_on_the_response(client):
    icp = _create_icp(client, "Score API J")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "score-api-j.invalid")
    _import_company_evidence(client, company_id)

    body = client.post("/api/v1/lead-scores", json={"icp_id": icp["id"], "company_id": company_id}).json()
    forbidden_keys = {"manager_feedback", "qualification", "good_fit", "weak_fit", "accepted"}
    assert forbidden_keys.isdisjoint(body.keys())
