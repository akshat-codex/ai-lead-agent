import csv
import io
import json

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


def _run_batch(client, icp_id, target_count=2, registry=None):
    registry = registry or _default_registry()
    return _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp_id, "target_count": target_count}).json()
    )


# --- complete lead / basic export ---------------------------------------


def test_json_export_reflects_real_pipeline_data(client):
    icp = _create_icp(client, "Export API A")
    batch = _run_batch(client, icp["id"], target_count=2)

    response = client.get("/api/v1/exports", params={"icp_id": icp["id"], "format": "JSON"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = json.loads(response.content)

    assert body["metadata"]["icp_id"] == icp["id"]
    assert body["metadata"]["lead_count"] == 2
    lead_ids_in_batch = {item["lead_id"] for item in batch["items"]}
    exported_lead_ids = {lead["lead_id"] for lead in body["leads"]}
    assert lead_ids_in_batch == exported_lead_ids

    for lead in body["leads"]:
        assert lead["identity"]["company_id"] is not None
        assert "scores" in lead
        assert "provenance" in lead


def test_csv_export_reflects_real_pipeline_data(client):
    icp = _create_icp(client, "Export API B")
    _run_batch(client, icp["id"], target_count=2)

    response = client.get("/api/v1/exports", params={"icp_id": icp["id"], "format": "CSV"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2
    for row in rows:
        assert row["company_id"]
        assert row["lead_id"]


# --- missing fields --------------------------------------------------


def test_export_before_any_scoring_shows_unknown_not_fabricated(client):
    icp = _create_icp(client, "Export API C")
    # discover + resolve only, no evidence/scoring/qualification yet
    registry = _default_registry()
    run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]
    client.post("/api/v1/lead-deduplications", json={"icp_id": icp["id"], "company_id": company_id})

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    lead = body["leads"][0]
    assert lead["hard_rule_result"] is None
    assert lead["scores"]["final_score"] is None
    assert lead["qualification_decision"] is None
    assert lead["adversarial_result"] is None


# --- multiple ICPs ------------------------------------------------------


def test_same_lead_exported_differently_under_different_icps(client):
    icp_a = _create_icp(client, "Export API D-A", min_employees=1, max_employees=10000)
    icp_b = _create_icp(client, "Export API D-B", min_employees=999999, max_employees=9999999)
    registry = _default_registry()

    batch_a = _run_batch(client, icp_a["id"], target_count=1, registry=registry)
    company_id = batch_a["items"][0]["company_id"]
    _run_batch(client, icp_b["id"], target_count=1, registry=registry)

    export_a = json.loads(client.get("/api/v1/exports", params={"icp_id": icp_a["id"]}).content)
    export_b = json.loads(client.get("/api/v1/exports", params={"icp_id": icp_b["id"]}).content)

    lead_a = next(l for l in export_a["leads"] if l["identity"]["company_id"] == company_id)
    lead_b = next(l for l in export_b["leads"] if l["identity"]["company_id"] == company_id)

    assert lead_a["icp_id"] != lead_b["icp_id"]
    assert lead_a["tier"] != "HARD_FAILED"
    assert lead_b["tier"] == "HARD_FAILED"


# --- batch filtering ------------------------------------------------------


def test_export_can_be_scoped_to_one_batch(client):
    icp = _create_icp(client, "Export API E")
    registry = _default_registry()
    batch1 = _run_batch(client, icp["id"], target_count=1, registry=registry)

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"], "batch_id": batch1["id"]}).content)
    assert body["metadata"]["batch_id"] == batch1["id"]
    assert len(body["leads"]) == 1
    assert body["leads"][0]["batch_id"] == batch1["id"]


# --- ranked output --------------------------------------------------------


def test_export_includes_rank_and_tier(client):
    icp = _create_icp(client, "Export API F")
    _run_batch(client, icp["id"], target_count=2)

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    ranks = [lead["rank"] for lead in body["leads"]]
    assert sorted(ranks) == list(range(1, len(ranks) + 1))
    for lead in body["leads"]:
        assert lead["tier"] is not None


# --- human-reviewed output --------------------------------------------


def test_human_review_decision_appears_in_export(client):
    icp = _create_icp(client, "Export API G")
    batch = _run_batch(client, icp["id"], target_count=1)
    lead_id = batch["items"][0]["lead_id"]

    client.post(
        "/api/v1/human-reviews",
        json={"lead_id": lead_id, "icp_id": icp["id"], "decision": "ACCEPT", "reviewer_id": "ops-1"},
    )

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    lead = next(l for l in body["leads"] if l["lead_id"] == lead_id)
    assert lead["human_review_decision"] == "ACCEPT"
    assert lead["tier"] == "ACCEPTED"


# --- hard-failed lead never exported as accepted/qualified -----------------


def test_hard_failed_lead_never_marked_accepted_in_export(client):
    icp = _create_icp(client, "Export API H", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], target_count=1, registry=registry)

    # corroborate to get a confirmed FAIL (single discovery run alone is only INSUFFICIENT -> HOLD)
    second_run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run["id"]})
    item = batch["items"][0]
    company_id = item["company_id"]
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    client.post(
        "/api/v1/hard-icp-validations",
        json={"icp_id": icp["id"], "company_id": company_id, "person_id": item["person_id"]},
    )

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    lead = next(l for l in body["leads"] if l["identity"]["company_id"] == company_id)
    assert lead["hard_rule_result"] == "FAIL"
    assert lead["tier"] == "HARD_FAILED"
    assert lead["tier"] != "ACCEPTED"
    assert lead["tier"] != "QUALIFIED_STRONG"


# --- duplicate lead handling ------------------------------------------


def test_duplicate_candidate_flagged_in_export(client):
    icp = _create_icp(client, "Export API I")
    registry = _default_registry()
    _run_batch(client, icp["id"], target_count=1, registry=registry)
    second_batch = _run_batch(client, icp["id"], target_count=1, registry=registry)

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    lead = next(l for l in body["leads"] if l["lead_id"] == second_batch["items"][0]["lead_id"])
    assert lead["is_duplicate_occurrence"] is True
    assert lead["tier"] == "DUPLICATE"


# --- deterministic output -----------------------------------------------


def test_repeated_export_calls_produce_identical_leads(client):
    icp = _create_icp(client, "Export API J")
    _run_batch(client, icp["id"], target_count=2)

    first = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    second = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)

    # exported_at/generated_at will differ by wall-clock time between calls;
    # everything else must be identical
    for lead in (first["leads"], second["leads"]):
        for item in lead:
            item.pop("exported_at", None)
    first["metadata"].pop("generated_at", None)
    second["metadata"].pop("generated_at", None)
    assert first == second


# --- CSV / JSON consistency ------------------------------------------------


def test_csv_and_json_agree_on_lead_count_and_ids(client):
    icp = _create_icp(client, "Export API K")
    _run_batch(client, icp["id"], target_count=2)

    json_body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"], "format": "JSON"}).content)
    csv_rows = list(csv.DictReader(io.StringIO(client.get("/api/v1/exports", params={"icp_id": icp["id"], "format": "CSV"}).text)))

    json_ids = {lead["lead_id"] for lead in json_body["leads"]}
    csv_ids = {row["lead_id"] for row in csv_rows}
    assert json_ids == csv_ids


# --- 404 -------------------------------------------------------------


def test_export_unknown_icp_returns_404(client):
    response = client.get("/api/v1/exports", params={"icp_id": "does-not-exist"})
    assert response.status_code == 404


def test_export_with_no_leads_returns_empty(client):
    icp = _create_icp(client, "Export API L")
    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    assert body["leads"] == []
    assert body["metadata"]["lead_count"] == 0


# --- provenance preservation ---------------------------------------------


def test_export_provenance_ids_reference_real_rows(client):
    icp = _create_icp(client, "Export API M")
    batch = _run_batch(client, icp["id"], target_count=1)
    lead_id = batch["items"][0]["lead_id"]

    body = json.loads(client.get("/api/v1/exports", params={"icp_id": icp["id"]}).content)
    lead = next(l for l in body["leads"] if l["lead_id"] == lead_id)

    if lead["provenance"]["score_id"]:
        score = client.get(f"/api/v1/lead-scores/{lead['provenance']['score_id']}")
        assert score.status_code == 200
    if lead["provenance"]["hard_validation_id"]:
        validation = client.get(f"/api/v1/hard-icp-validations/{lead['provenance']['hard_validation_id']}")
        assert validation.status_code == 200


# --- no source-data mutation -----------------------------------------------


def test_export_never_mutates_icp_score_or_evidence(client):
    icp = _create_icp(client, "Export API N")
    batch = _run_batch(client, icp["id"], target_count=1)
    company_id = batch["items"][0]["company_id"]

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    scores_before = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()

    client.get("/api/v1/exports", params={"icp_id": icp["id"], "format": "JSON"})
    client.get("/api/v1/exports", params={"icp_id": icp["id"], "format": "CSV"})

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    scores_after = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()

    assert icp_before == icp_after
    assert evidence_before == evidence_after
    assert scores_before == scores_after


def test_export_never_mutates_canonical_lead(client):
    icp = _create_icp(client, "Export API O")
    batch = _run_batch(client, icp["id"], target_count=1)
    lead_id = batch["items"][0]["lead_id"]

    lead_before = client.get(f"/api/v1/leads/{lead_id}").json()
    client.get("/api/v1/exports", params={"icp_id": icp["id"]})
    lead_after = client.get(f"/api/v1/leads/{lead_id}").json()
    assert lead_before == lead_after
