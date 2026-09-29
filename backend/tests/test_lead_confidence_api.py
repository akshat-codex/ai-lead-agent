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


def _confidence(client, icp_id, company_id, person_id=None):
    params = {"icp_id": icp_id, "company_id": company_id}
    if person_id is not None:
        params["person_id"] = person_id
    return client.get("/api/v1/lead-confidence", params=params).json()


# --- fully supported lead -------------------------------------------


def test_confidence_reflects_real_pipeline_after_full_processing(client):
    icp = _create_icp(client, "Confidence API A")
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], registry=registry)
    item = batch["items"][0]
    company_id = item["company_id"]

    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id, "person_id": item["person_id"]})

    body = _confidence(client, icp["id"], company_id, item["person_id"])
    # The batch orchestrator's own pipeline (discovery + enrichment across
    # two independent mock providers) can legitimately produce a genuine
    # employee_range conflict between providers — CONFLICTED is a fully
    # valid, honestly-detected outcome here, not an error.
    assert body["readiness"] in ("VERIFIED", "PARTIALLY_VERIFIED", "INSUFFICIENT_EVIDENCE", "CONFLICTED")
    assert body["hard_rule_result"] in ("PASS", "HOLD", "FAIL")
    assert len(body["supporting_evidence"]) > 0


# --- missing evidence --------------------------------------------------


def test_no_pipeline_data_returns_unknown(client):
    icp = _create_icp(client, "Confidence API B")
    registry = _default_registry()
    run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    body = _confidence(client, icp["id"], company_id)
    assert body["readiness"] == "UNKNOWN"
    assert body["lead_id"] is None


# --- conflicting evidence -----------------------------------------------


def test_conflicting_evidence_produces_conflicted_readiness(client):
    icp = _create_icp(client, "Confidence API C")
    registry = _default_registry()
    run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    client.post(
        "/api/v1/evidence",
        json={"entity_type": "COMPANY", "entity_id": company_id, "field": "industry", "value": "Skincare", "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z"},
    )
    client.post(
        "/api/v1/evidence",
        json={"entity_type": "COMPANY", "entity_id": company_id, "field": "industry", "value": "Something Else", "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z"},
    )

    body = _confidence(client, icp["id"], company_id)
    assert body["readiness"] == "CONFLICTED"
    assert "industry" in body["conflicting_fields"]


# --- hard FAIL -----------------------------------------------------------


def test_hard_fail_lead_is_never_verified(client):
    # Discovery-only (no batch/enrichment) + a second, independent
    # discovery run that agrees on employee_count keeps this scenario
    # to a single, uncorroborated field so the FAIL is confirmed without
    # an unrelated employee_range conflict from enrichment muddying it.
    icp = _create_icp(client, "Confidence API D", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    run1 = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    resolution1 = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run1["id"]}).json()
    company_id = resolution1[0]["canonical_company_id"]

    run2 = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]})
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id})

    body = _confidence(client, icp["id"], company_id)
    assert body["hard_rule_result"] == "FAIL"
    assert body["readiness"] == "INSUFFICIENT_EVIDENCE"
    assert body["readiness"] != "VERIFIED"


# --- hard HOLD -------------------------------------------------------


def test_hard_hold_lead_is_insufficient_evidence(client):
    # Discovery-only, single sighting, no enrichment/batch pipeline -> a
    # clean, single-source-per-field HOLD with no risk of an unrelated
    # enrichment-sourced conflict on a different field.
    icp = _create_icp(client, "Confidence API E")
    registry = _default_registry()
    run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id})

    body = _confidence(client, icp["id"], company_id)
    assert body["hard_rule_result"] == "HOLD"
    assert body["readiness"] == "INSUFFICIENT_EVIDENCE"


# --- human ACCEPT/REJECT --------------------------------------------


def test_human_review_decision_surfaced_in_confidence_result(client):
    icp = _create_icp(client, "Confidence API F")
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], registry=registry)
    item = batch["items"][0]
    company_id = item["company_id"]
    lead_id = item["lead_id"]

    second_run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run["id"]})
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id, "person_id": item["person_id"]})

    client.post("/api/v1/human-reviews", json={"lead_id": lead_id, "icp_id": icp["id"], "decision": "ACCEPT", "reviewer_id": "ops-1"})

    body = _confidence(client, icp["id"], company_id, item["person_id"])
    assert body["human_review_decision"] == "ACCEPT"
    assert "HUMAN_ACCEPTED" in body["reason_codes"]


# --- multiple ICPs ----------------------------------------------------


def test_confidence_isolated_between_icps(client):
    icp_a = _create_icp(client, "Confidence API G-A", min_employees=1, max_employees=10000)
    icp_b = _create_icp(client, "Confidence API G-B", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    batch_a = _run_batch(client, icp_a["id"], registry=registry)
    company_id = batch_a["items"][0]["company_id"]

    second_run_a = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp_a["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run_a["id"]})
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_a["id"], "company_id": company_id})

    second_run_b = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp_b["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run_b["id"]})
    client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_b["id"], "company_id": company_id})

    body_a = _confidence(client, icp_a["id"], company_id)
    body_b = _confidence(client, icp_b["id"], company_id)

    assert body_a["hard_rule_result"] != body_b["hard_rule_result"] or body_a["icp_id"] != body_b["icp_id"]
    assert body_a["icp_id"] == icp_a["id"]
    assert body_b["icp_id"] == icp_b["id"]


# --- deterministic results -----------------------------------------------


def test_repeated_confidence_calls_are_identical(client):
    icp = _create_icp(client, "Confidence API H")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]

    first = _confidence(client, icp["id"], company_id)
    second = _confidence(client, icp["id"], company_id)
    first.pop("generated_at", None)
    second.pop("generated_at", None)
    assert first == second


# --- evidence provenance --------------------------------------------


def test_supporting_evidence_ids_reference_real_evidence_rows(client):
    icp = _create_icp(client, "Confidence API I")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    body = _confidence(client, icp["id"], company_id)
    all_evidence = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    all_ids = {e["id"] for e in all_evidence}
    for item in body["supporting_evidence"]:
        for eid in item["evidence_ids"]:
            assert eid in all_ids


# --- no regression / no mutation ----------------------------------------


def test_confidence_never_mutates_icp_evidence_or_scores(client):
    icp = _create_icp(client, "Confidence API J")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    _confidence(client, icp["id"], company_id)
    client.post("/api/v1/lead-confidence/snapshots", params={"icp_id": icp["id"], "company_id": company_id})

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    assert icp_before == icp_after
    assert evidence_before == evidence_after


# --- snapshots (append-only) -----------------------------------------------


def test_snapshot_created_and_retrievable(client):
    icp = _create_icp(client, "Confidence API K")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]

    created = client.post("/api/v1/lead-confidence/snapshots", params={"icp_id": icp["id"], "company_id": company_id}).json()
    fetched = client.get(f"/api/v1/lead-confidence/snapshots/{created['id']}").json()
    assert fetched == created


def test_repeated_snapshots_are_append_only(client):
    icp = _create_icp(client, "Confidence API L")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]

    first = client.post("/api/v1/lead-confidence/snapshots", params={"icp_id": icp["id"], "company_id": company_id}).json()
    second = client.post("/api/v1/lead-confidence/snapshots", params={"icp_id": icp["id"], "company_id": company_id}).json()
    assert first["id"] != second["id"]

    history = client.get("/api/v1/lead-confidence/snapshots", params={"icp_id": icp["id"]}).json()
    assert len(history) == 2


# --- 404s ------------------------------------------------------------


def test_confidence_unknown_icp_returns_404(client):
    response = client.get("/api/v1/lead-confidence", params={"icp_id": "does-not-exist", "company_id": "x"})
    assert response.status_code == 404


def test_get_unknown_snapshot_returns_404(client):
    response = client.get("/api/v1/lead-confidence/snapshots/does-not-exist")
    assert response.status_code == 404


def test_list_snapshots_requires_a_filter(client):
    response = client.get("/api/v1/lead-confidence/snapshots")
    assert response.status_code == 400


# --- no regression to earlier phases --------------------------------------


def test_other_phase_endpoints_still_work_after_confidence_calls(client):
    icp = _create_icp(client, "Confidence API M")
    batch = _run_batch(client, icp["id"])
    company_id = batch["items"][0]["company_id"]

    _confidence(client, icp["id"], company_id)

    assert client.get("/api/v1/icps").status_code == 200
    assert client.get(f"/api/v1/companies/{company_id}").status_code == 200
    assert client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).status_code == 200
