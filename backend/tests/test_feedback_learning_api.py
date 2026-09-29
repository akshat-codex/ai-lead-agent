from datetime import datetime, timezone

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


# --- GOOD_FIT vs NOT_FIT through the real API -------------------------


def test_learning_reflects_real_feedback_counts(client):
    icp = _create_icp(client, "Learning API A")
    batch = _run_batch(client, icp["id"])
    lead_id = batch["items"][0]["lead_id"]

    _submit_feedback(client, icp["id"], lead_id, "GOOD_FIT")
    _submit_feedback(client, icp["id"], lead_id, "GOOD_FIT")

    body = client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    assert body["decision_counts"]["good_fit"] == 2
    assert body["decision_counts"]["total"] == 2
    assert body["is_cold_start"] is False


def test_signal_correlation_derived_from_real_business_model(client):
    icp = _create_icp(client, "Learning API B")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]
    lead_id = batch["items"][0]["lead_id"]

    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY", "entity_id": company_id, "field": "business_model",
            "value": "Direct-to-consumer skincare brand", "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    client.post(f"/api/v1/companies/{company_id}/classify-business-model")

    _submit_feedback(client, icp["id"], lead_id, "GOOD_FIT")

    body = client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    signal_names = {c["signal_name"] for c in body["signal_correlations"]}
    assert any(name.startswith("business_model:") for name in signal_names)


# --- small sample / cold start ------------------------------------------


def test_cold_start_with_no_feedback_at_all(client):
    icp = _create_icp(client, "Learning API C")
    body = client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"]}).json()
    assert body["is_cold_start"] is True
    assert body["decision_counts"]["total"] == 0
    assert body["overall_confidence"] == "INSUFFICIENT_DATA"


def test_cold_start_with_default_min_sample_size(client):
    icp = _create_icp(client, "Learning API D")
    batch = _run_batch(client, icp["id"])
    lead_id = batch["items"][0]["lead_id"]
    _submit_feedback(client, icp["id"], lead_id, "GOOD_FIT")

    body = client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"]}).json()  # default min_sample_size=5
    assert body["is_cold_start"] is True


# --- multi-ICP isolation --------------------------------------------------


def test_feedback_learning_isolated_per_icp(client):
    icp_a = _create_icp(client, "Learning API E-A")
    icp_b = _create_icp(client, "Learning API E-B")
    batch_a = _run_batch(client, icp_a["id"])
    batch_b = _run_batch(client, icp_b["id"])

    _submit_feedback(client, icp_a["id"], batch_a["items"][0]["lead_id"], "GOOD_FIT")
    _submit_feedback(client, icp_b["id"], batch_b["items"][0]["lead_id"], "NOT_FIT", reason_codes=["WRONG_SIZE"])

    body_a = client.get("/api/v1/feedback-learning", params={"icp_id": icp_a["id"], "min_sample_size": 1}).json()
    body_b = client.get("/api/v1/feedback-learning", params={"icp_id": icp_b["id"], "min_sample_size": 1}).json()

    assert body_a["decision_counts"]["good_fit"] == 1
    assert body_a["decision_counts"]["not_fit"] == 0
    assert body_b["decision_counts"]["not_fit"] == 1
    assert body_b["decision_counts"]["good_fit"] == 0


def test_global_learning_across_icps_when_no_icp_filter_given(client):
    icp_a = _create_icp(client, "Learning API F-A")
    icp_b = _create_icp(client, "Learning API F-B")
    batch_a = _run_batch(client, icp_a["id"])
    batch_b = _run_batch(client, icp_b["id"])

    _submit_feedback(client, icp_a["id"], batch_a["items"][0]["lead_id"], "GOOD_FIT")
    _submit_feedback(client, icp_b["id"], batch_b["items"][0]["lead_id"], "GOOD_FIT")

    body = client.get("/api/v1/feedback-learning", params={"min_sample_size": 1}).json()
    assert body["scope"]["icp_id"] is None
    assert body["decision_counts"]["good_fit"] >= 2


# --- same lead under multiple ICPs -----------------------------------------


def test_same_lead_feedback_under_different_icps_counted_separately(client):
    icp_a = _create_icp(client, "Learning API G-A")
    icp_b = _create_icp(client, "Learning API G-B")
    registry = _default_registry()
    batch_a = _run_batch(client, icp_a["id"], registry=registry)
    item = batch_a["items"][0]
    company_id = item["company_id"]
    person_id = item["person_id"]
    lead_id = item["lead_id"]

    dedup_payload = {"icp_id": icp_b["id"], "company_id": company_id}
    if person_id is not None:
        dedup_payload["person_id"] = person_id
    dedup_b = client.post("/api/v1/lead-deduplications", json=dedup_payload).json()
    assert dedup_b["lead_id"] == lead_id  # same underlying canonical lead, reused

    _submit_feedback(client, icp_a["id"], lead_id, "GOOD_FIT")
    _submit_feedback(client, icp_b["id"], lead_id, "NOT_FIT", reason_codes=["WRONG_ICP"])

    body_a = client.get("/api/v1/feedback-learning", params={"icp_id": icp_a["id"], "min_sample_size": 1}).json()
    body_b = client.get("/api/v1/feedback-learning", params={"icp_id": icp_b["id"], "min_sample_size": 1}).json()
    assert body_a["decision_counts"]["good_fit"] == 1
    assert body_b["decision_counts"]["not_fit"] == 1


# --- hard-rule immunity through the API -------------------------------


def test_learning_never_changes_hard_validation_result(client):
    icp = _create_icp(client, "Learning API H", min_employees=999999, max_employees=9999999)
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
    assert validation_before["overall_result"] == "FAIL"

    _submit_feedback(client, icp["id"], item["lead_id"], "GOOD_FIT")  # manager likes it anyway
    client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"], "min_sample_size": 1})

    validation_after = client.get(f"/api/v1/hard-icp-validations/{validation_before['id']}").json()
    assert validation_after["overall_result"] == "FAIL"  # unchanged - learning has no power over this


# --- 404s ----------------------------------------------------------------


def test_learning_unknown_icp_returns_404(client):
    response = client.get("/api/v1/feedback-learning", params={"icp_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_snapshot_returns_404(client):
    response = client.get("/api/v1/feedback-learning/snapshots/does-not-exist")
    assert response.status_code == 404


# --- snapshots (append-only) -----------------------------------------------


def test_snapshot_created_and_retrievable(client):
    icp = _create_icp(client, "Learning API I")
    batch = _run_batch(client, icp["id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    created = client.post("/api/v1/feedback-learning/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    fetched = client.get(f"/api/v1/feedback-learning/snapshots/{created['id']}").json()
    assert fetched == created
    assert created["total_feedback_count"] == 1


def test_repeated_snapshots_are_append_only(client):
    icp = _create_icp(client, "Learning API J")
    batch = _run_batch(client, icp["id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    first = client.post("/api/v1/feedback-learning/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    second = client.post("/api/v1/feedback-learning/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    assert first["id"] != second["id"]

    history = client.get("/api/v1/feedback-learning/snapshots", params={"icp_id": icp["id"]}).json()
    assert len(history) == 2


# --- historical feedback immutability --------------------------------------


def test_learning_never_mutates_feedback_evidence_or_icp(client):
    icp = _create_icp(client, "Learning API K")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]
    feedback_response = _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")
    feedback_id = feedback_response.json()["id"]

    feedback_before = client.get(f"/api/v1/feedback/{feedback_id}").json()
    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"], "min_sample_size": 1})
    client.post("/api/v1/feedback-learning/snapshots", params={"icp_id": icp["id"], "min_sample_size": 1})

    feedback_after = client.get(f"/api/v1/feedback/{feedback_id}").json()
    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    assert feedback_before == feedback_after
    assert icp_before == icp_after
    assert evidence_before == evidence_after


# --- deterministic results -----------------------------------------------


def test_repeated_analysis_calls_are_identical(client):
    icp = _create_icp(client, "Learning API L")
    batch = _run_batch(client, icp["id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    first = client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    second = client.get("/api/v1/feedback-learning", params={"icp_id": icp["id"], "min_sample_size": 1}).json()
    assert first == second
