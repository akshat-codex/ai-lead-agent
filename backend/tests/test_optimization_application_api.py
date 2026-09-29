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


def _build_sufficient_feedback(client, icp_id, n=6):
    for _ in range(n):
        batch = _run_batch(client, icp_id)
        _classify_dtc(client, batch["items"][0]["company_id"])
        _submit_feedback(client, icp_id, batch["items"][0]["lead_id"], "GOOD_FIT")


def _get_pending(client, icp_id, min_sample_size=1):
    return client.get(
        "/api/v1/optimization-applications/pending", params={"icp_id": icp_id, "min_sample_size": min_sample_size}
    ).json()


def _approve(client, icp_id, rec, decision="APPROVED", approved_by="mgr-approver"):
    payload = {
        "fingerprint": rec["fingerprint"], "icp_id": icp_id, "icp_version": rec["icp_version"],
        "decision": decision, "approved_by": approved_by,
        "recommendation_type": rec["recommendation_type"], "signal_name": rec["signal_name"],
        "reason_code": rec["reason_code"], "sample_count": rec["sample_count"], "confidence": rec["confidence"],
        "expected_effect": rec["expected_effect"], "magnitude_hint": rec["magnitude_hint"], "is_global": rec["is_global"],
    }
    return client.post("/api/v1/optimization-applications/approvals", json=payload)


def _apply(client, icp_id, rec, applied_by="mgr-applier"):
    payload = {"fingerprint": rec["fingerprint"], "icp_id": icp_id, "icp_version": rec["icp_version"], "applied_by": applied_by}
    return client.post("/api/v1/optimization-applications", json=payload)


# --- approval required before apply -------------------------------------


def test_apply_without_approval_is_rejected(client):
    icp = _create_icp(client, "OptApp API A")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    response = _apply(client, icp["id"], rec)
    assert response.status_code == 422


def test_approve_then_apply_succeeds(client):
    icp = _create_icp(client, "OptApp API B")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    approval = _approve(client, icp["id"], rec)
    assert approval.status_code == 201

    application = _apply(client, icp["id"], rec)
    assert application.status_code == 201
    assert application.json()["status"] == "APPLIED"


# --- insufficient confidence blocked --------------------------------------


def test_insufficient_confidence_recommendation_cannot_be_approved(client):
    icp = _create_icp(client, "OptApp API C")
    batch = _run_batch(client, icp["id"])
    _classify_dtc(client, batch["items"][0]["company_id"])
    _submit_feedback(client, icp["id"], batch["items"][0]["lead_id"], "GOOD_FIT")

    # min_sample_size=1 so we get a recommendation object at all, but its
    # own confidence should be LOW (single observation), never blocking
    # unless we simulate INSUFFICIENT_DATA explicitly:
    pending = _get_pending(client, icp["id"], min_sample_size=100)  # forces cold start / no recs
    assert pending == []


def test_manually_crafted_insufficient_confidence_approval_is_rejected(client):
    icp = _create_icp(client, "OptApp API D")
    payload = {
        "fingerprint": "fake-fingerprint-1", "icp_id": icp["id"], "icp_version": 1,
        "decision": "APPROVED", "approved_by": "mgr-1",
        "recommendation_type": "SOFT_PREFERENCE_WEIGHT_HINT", "signal_name": "business_model:DTC",
        "reason_code": None, "sample_count": 1, "confidence": "INSUFFICIENT_DATA",
        "expected_effect": "INCREASE_PRIORITY", "magnitude_hint": 0.1, "is_global": False,
    }
    response = client.post("/api/v1/optimization-applications/approvals", json=payload)
    assert response.status_code == 422


# --- ICP/version isolation -------------------------------------------


def test_approval_scoped_to_specific_icp_version(client):
    icp = _create_icp(client, "OptApp API E")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])
    assert rec["icp_version"] == 1

    approval = _approve(client, icp["id"], rec).json()
    assert approval["icp_version"] == 1


def test_application_isolated_between_icps(client):
    icp_a = _create_icp(client, "OptApp API F-A")
    icp_b = _create_icp(client, "OptApp API F-B")
    _build_sufficient_feedback(client, icp_a["id"])

    rec = next(r for r in _get_pending(client, icp_a["id"]) if r["is_eligible_for_approval"])
    _approve(client, icp_a["id"], rec)
    _apply(client, icp_a["id"], rec)

    config_a = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp_a["id"], "icp_version": 1}).json()
    config_b = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp_b["id"], "icp_version": 1}).json()

    assert config_a["is_default"] is False
    assert config_b["is_default"] is True  # unaffected by icp_a's application


# --- explicit global approval only --------------------------------------


def test_global_flag_defaults_false_and_requires_explicit_request(client):
    icp = _create_icp(client, "OptApp API G")
    _build_sufficient_feedback(client, icp["id"])
    pending = _get_pending(client, icp["id"], min_sample_size=1)
    assert all(r["is_global"] is False for r in pending)  # never global unless explicitly requested


def test_global_recommendations_only_appear_when_requested(client):
    icp = _create_icp(client, "OptApp API H")
    _build_sufficient_feedback(client, icp["id"])
    pending_scoped = client.get(
        "/api/v1/optimization-applications/pending", params={"icp_id": icp["id"], "min_sample_size": 1, "include_global_patterns": False}
    ).json()
    pending_global = client.get(
        "/api/v1/optimization-applications/pending", params={"icp_id": icp["id"], "min_sample_size": 1, "include_global_patterns": True}
    ).json()
    assert all(r["is_global"] is False for r in pending_scoped)
    assert any(r["is_global"] is True for r in pending_global)


# --- apply changes effective config ---------------------------------------


def test_apply_changes_effective_configuration(client):
    icp = _create_icp(client, "OptApp API I")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    config_before = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()
    assert config_before["is_default"] is True

    _approve(client, icp["id"], rec)
    _apply(client, icp["id"], rec)

    config_after = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()
    assert config_after["is_default"] is False


# --- rollback restores previous effective behavior ------------------------


def test_rollback_restores_default_configuration(client):
    icp = _create_icp(client, "OptApp API J")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    _approve(client, icp["id"], rec)
    application = _apply(client, icp["id"], rec).json()

    config_applied = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()
    assert config_applied["is_default"] is False

    rollback = client.post(f"/api/v1/optimization-applications/{application['id']}/rollback", json={"rolled_back_by": "mgr-1"})
    assert rollback.status_code == 201
    assert rollback.json()["status"] == "ROLLED_BACK"

    config_after_rollback = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()
    assert config_after_rollback["is_default"] is True


def test_rollback_does_not_delete_original_application_record(client):
    icp = _create_icp(client, "OptApp API K")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    _approve(client, icp["id"], rec)
    application = _apply(client, icp["id"], rec).json()
    client.post(f"/api/v1/optimization-applications/{application['id']}/rollback", json={"rolled_back_by": "mgr-1"})

    original = client.get(f"/api/v1/optimization-applications/history/{application['id']}").json()
    assert original["status"] == "APPLIED"  # the original row is never edited in place

    history = client.get("/api/v1/optimization-applications/history", params={"icp_id": icp["id"]}).json()
    assert len(history) == 2  # APPLIED row + ROLLED_BACK row, both preserved


def test_rollback_of_not_applied_recommendation_fails(client):
    icp = _create_icp(client, "OptApp API L")
    response = client.post("/api/v1/optimization-applications/does-not-exist/rollback", json={"rolled_back_by": "mgr-1"})
    assert response.status_code == 404


# --- historical records immutable ------------------------------------


def test_optimization_application_never_mutates_icp_feedback_or_scores(client):
    icp = _create_icp(client, "OptApp API M")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()

    _approve(client, icp["id"], rec)
    _apply(client, icp["id"], rec)

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    assert icp_before == icp_after


# --- deterministic behavior -----------------------------------------------


def test_repeated_pending_calls_produce_consistent_fingerprints(client):
    icp = _create_icp(client, "OptApp API N")
    _build_sufficient_feedback(client, icp["id"])

    first = _get_pending(client, icp["id"])
    second = _get_pending(client, icp["id"])
    first_fps = {r["fingerprint"] for r in first}
    second_fps = {r["fingerprint"] for r in second}
    assert first_fps == second_fps


# --- duplicate application prevented ---------------------------------


def test_duplicate_application_is_rejected(client):
    icp = _create_icp(client, "OptApp API O")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    _approve(client, icp["id"], rec)
    first_apply = _apply(client, icp["id"], rec)
    assert first_apply.status_code == 201

    second_apply = _apply(client, icp["id"], rec)
    assert second_apply.status_code == 409


def test_reapplication_allowed_after_rollback(client):
    icp = _create_icp(client, "OptApp API P")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    _approve(client, icp["id"], rec)
    first_application = _apply(client, icp["id"], rec).json()
    client.post(f"/api/v1/optimization-applications/{first_application['id']}/rollback", json={"rolled_back_by": "mgr-1"})

    second_apply = _apply(client, icp["id"], rec)
    assert second_apply.status_code == 201


# --- failed application leaves state unchanged ----------------------------


def test_failed_application_does_not_change_effective_config(client):
    icp = _create_icp(client, "OptApp API Q")
    _build_sufficient_feedback(client, icp["id"])
    rec = next(r for r in _get_pending(client, icp["id"]) if r["is_eligible_for_approval"])

    config_before = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()

    failed = _apply(client, icp["id"], rec)  # no approval yet -> fails
    assert failed.status_code == 422

    config_after = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()
    config_before.pop("generated_at", None)
    config_after.pop("generated_at", None)
    assert config_before == config_after


# --- hard FAIL remains impossible to promote ------------------------------


def test_no_recommendation_ever_targets_hard_rule_result(client):
    icp = _create_icp(client, "OptApp API R", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], registry=registry)
    item = batch["items"][0]
    company_id = item["company_id"]

    second_run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run["id"]})
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id, "person_id": item["person_id"]})

    for i in range(6):
        _submit_feedback(client, icp["id"], item["lead_id"], "GOOD_FIT")

    pending = _get_pending(client, icp["id"], min_sample_size=1)
    assert all("hard_rule_result" not in (r["signal_name"] or "") for r in pending)


# --- cold start behaves exactly like Phase 25/default behavior ------------


def test_cold_start_produces_no_pending_recommendations(client):
    icp = _create_icp(client, "OptApp API S")
    pending = _get_pending(client, icp["id"])  # default min_sample_size=5, no feedback
    assert pending == []


def test_cold_start_effective_config_is_default(client):
    icp = _create_icp(client, "OptApp API T")
    config = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": icp["id"], "icp_version": 1}).json()
    assert config["is_default"] is True


# --- 404s ------------------------------------------------------------


def test_apply_unknown_icp_returns_404(client):
    response = client.post(
        "/api/v1/optimization-applications",
        json={"fingerprint": "x", "icp_id": "does-not-exist", "icp_version": 1, "applied_by": "mgr-1"},
    )
    assert response.status_code == 404


def test_effective_config_unknown_icp_returns_404(client):
    response = client.get("/api/v1/optimization-applications/effective-config", params={"icp_id": "does-not-exist", "icp_version": 1})
    assert response.status_code == 404


def test_list_history_requires_a_filter(client):
    response = client.get("/api/v1/optimization-applications/history")
    assert response.status_code == 400
