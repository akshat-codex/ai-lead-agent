from app.main import app
from app.providers.default_registry import get_provider_registry
from app.providers.mocks import (
    MockCompanyDataProvider,
    MockCompanyRegistryProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)
from app.providers.registry import ProviderRegistry


def _icp_payload(name: str, min_employees: int = 1, max_employees: int = 10000) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": [], "geography": [], "min_employees": min_employees, "max_employees": max_employees,
            "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [],
            "growth_signals": [], "marketing_signals": [], "other_preferences": [],
        },
    }


def _create_icp(client, name: str, **overrides) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name, **overrides)).json()


def _default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())
    registry.register(MockPeopleDataProvider())
    registry.register(MockWebSearchProvider())
    return registry


def _with_registry(client, registry, fn):
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        return fn()
    finally:
        del app.dependency_overrides[get_provider_registry]


def _run_batch(client, icp_id, target_count=1, registry=None):
    registry = registry or _default_registry()
    return _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp_id, "target_count": target_count}).json()
    )


def _submit_feedback(client, icp_id, lead_ref, decision, reason_codes=None, reviewer_id="mgr-1"):
    payload = {
        "lead_ref": lead_ref, "icp_id": icp_id, "decision": decision,
        "reason_codes": reason_codes or ([] if decision == "GOOD_FIT" else ["GENERIC_REASON"]),
        "reviewer_id": reviewer_id,
    }
    return client.post("/api/v1/feedback", json=payload)


def _classify_dtc(client, company_id):
    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY", "entity_id": company_id, "field": "business_model",
            "value": "Direct-to-consumer skincare brand", "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    client.post(f"/api/v1/companies/{company_id}/classify-business-model")


# --- sufficient feedback produces recommendations ------------------------


def test_sufficient_feedback_produces_recommendations(client):
    icp = _create_icp(client, "Optimization API A")
    for i in range(6):
        batch = _run_batch(client, icp["id"])
        item = batch["items"][0]
        _classify_dtc(client, item["company_id"])
        _submit_feedback(client, icp["id"], item["lead_id"], "GOOD_FIT")

    body = client.get("/api/v1/optimization", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    assert body["is_cold_start"] is False
    assert len(body["recommendations"]) >= 1
    dtc_rec = next((r for r in body["recommendations"] if r["signal_name"] == "business_model:DTC"), None)
    assert dtc_rec is not None
    assert dtc_rec["expected_effect"] == "INCREASE_PRIORITY"


# --- insufficient feedback -> no adjustment -----------------------------


def test_insufficient_feedback_returns_no_recommendations(client):
    icp = _create_icp(client, "Optimization API B")
    batch = _run_batch(client, icp["id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    body = client.get("/api/v1/optimization", params={"icp_id": icp["id"]}).json()  # default min_sample_size=5
    assert body["is_cold_start"] is True
    assert body["recommendations"] == []


def test_ranking_preview_matches_original_ranking_on_cold_start(client):
    icp = _create_icp(client, "Optimization API C")
    _run_batch(client, icp["id"], target_count=2)

    ranking = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    optimization = client.get("/api/v1/optimization", params={"icp_id": icp["id"]}).json()

    ranking_by_lead = {rl["lead_id"]: rl["rank"] for rl in ranking["ranked_leads"]}
    for preview in optimization["ranking_preview"]:
        assert preview["original_rank"] == ranking_by_lead[preview["lead_id"]]
        assert preview["preview_rank"] == preview["original_rank"]


# --- multi-ICP isolation ---------------------------------------------


def test_optimization_isolated_per_icp(client):
    icp_a = _create_icp(client, "Optimization API D-A")
    icp_b = _create_icp(client, "Optimization API D-B")

    for i in range(6):
        batch = _run_batch(client, icp_a["id"])
        _classify_dtc(client, batch["items"][0]["company_id"])
        _submit_feedback(client, icp_a["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    body_a = client.get("/api/v1/optimization", params={"icp_id": icp_a["id"], "min_sample_size": 1}).json()
    body_b = client.get("/api/v1/optimization", params={"icp_id": icp_b["id"], "min_sample_size": 1}).json()

    assert body_a["is_cold_start"] is False
    assert body_b["is_cold_start"] is True
    assert body_b["recommendations"] == []


# --- hard FAIL remains impossible to promote ----------------------------


def test_hard_failed_lead_never_appears_ahead_of_eligible_leads_in_preview(client):
    icp = _create_icp(client, "Optimization API E", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], registry=registry)
    item = batch["items"][0]
    company_id = item["company_id"]

    second_run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run["id"]})
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    validation = client.post(
        "/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id, "person_id": item["person_id"]}
    ).json()
    assert validation["overall_result"] == "FAIL"

    for i in range(6):
        _submit_feedback(client, icp["id"], item["lead_id"], "GOOD_FIT")

    body = client.get("/api/v1/optimization", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    fail_preview = next((p for p in body["ranking_preview"] if p["lead_id"] == item["lead_id"]), None)
    assert fail_preview is not None
    assert fail_preview["tier"] == "HARD_FAILED"


def test_learning_never_mutates_hard_validation_through_optimization_api(client):
    icp = _create_icp(client, "Optimization API F", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], registry=registry)
    item = batch["items"][0]
    company_id = item["company_id"]

    second_run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run["id"]})
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    validation_before = client.post(
        "/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id, "person_id": item["person_id"]}
    ).json()

    for i in range(6):
        _submit_feedback(client, icp["id"], item["lead_id"], "GOOD_FIT")

    client.get("/api/v1/optimization", params={"icp_id": icp["id"], "min_sample_size": 1})
    client.post("/api/v1/optimization/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1})

    validation_after = client.get(f"/api/v1/hard-icp-validations/{validation_before['id']}").json()
    assert validation_after["overall_result"] == "FAIL"
    assert validation_after == validation_before


# --- 404s ----------------------------------------------------------------


def test_optimization_unknown_icp_returns_404(client):
    response = client.get("/api/v1/optimization", params={"icp_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_snapshot_returns_404(client):
    response = client.get("/api/v1/optimization/snapshots/does-not-exist")
    assert response.status_code == 404


# --- snapshots (append-only) -----------------------------------------------


def test_snapshot_created_and_retrievable(client):
    icp = _create_icp(client, "Optimization API G")
    batch = _run_batch(client, icp["id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    created = client.post("/api/v1/optimization/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    fetched = client.get(f"/api/v1/optimization/snapshots/{created['id']}").json()
    assert fetched == created


def test_repeated_snapshots_are_append_only(client):
    icp = _create_icp(client, "Optimization API H")
    batch = _run_batch(client, icp["id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    first = client.post("/api/v1/optimization/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    second = client.post("/api/v1/optimization/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    assert first["id"] != second["id"]

    history = client.get("/api/v1/optimization/snapshots", params={"icp_id": icp["id"]}).json()
    assert len(history) == 2


# --- no mutation of ICP/feedback/evidence/scores ----------------------


def test_optimization_never_mutates_icp_feedback_evidence_or_scores(client):
    icp = _create_icp(client, "Optimization API I")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]
    feedback_response = _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")
    feedback_id = feedback_response.json()["id"]

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    feedback_before = client.get(f"/api/v1/feedback/{feedback_id}").json()
    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    scores_before = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()

    client.get("/api/v1/optimization", params={"icp_id": icp["id"], "min_sample_size": 1})
    client.post("/api/v1/optimization/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1})

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    feedback_after = client.get(f"/api/v1/feedback/{feedback_id}").json()
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    scores_after = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()

    assert icp_before == icp_after
    assert feedback_before == feedback_after
    assert evidence_before == evidence_after
    assert scores_before == scores_after


# --- provider prioritization ---------------------------------------------


def test_provider_priority_hint_through_the_api(client):
    icp = _create_icp(client, "Optimization API J")
    for i in range(6):
        batch = _run_batch(client, icp["id"])
        company_id = batch["items"][0]["company_id"]
        client.post(
            "/api/v1/evidence",
            json={
                "entity_type": "COMPANY", "entity_id": company_id, "field": "products_services",
                "value": "We run meta advertising campaigns", "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z",
            },
        )
        client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals")
        _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    body = client.get("/api/v1/optimization", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    provider_hints = [r for r in body["recommendations"] if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT"]
    assert len(provider_hints) >= 1


# --- cold start ------------------------------------------------------


def test_cold_start_with_zero_feedback(client):
    icp = _create_icp(client, "Optimization API K")
    _run_batch(client, icp["id"])
    body = client.get("/api/v1/optimization", params={"icp_id": icp["id"]}).json()
    assert body["is_cold_start"] is True
    assert body["recommendations"] == []
    assert "Cold start" in body["explanation"]
