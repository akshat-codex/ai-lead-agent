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


# --- basic ranking through the real pipeline --------------------------


def test_ranking_reflects_real_batch_results(client):
    icp = _create_icp(client, "Ranking API A")
    batch = _run_batch(client, icp["id"], target_count=2)

    response = client.get("/api/v1/rankings", params={"icp_id": icp["id"]})
    body = response.json()
    assert response.status_code == 200
    assert body["icp_id"] == icp["id"]
    assert len(body["ranked_leads"]) == 2
    lead_ids_in_batch = {item["lead_id"] for item in batch["items"]}
    ranked_lead_ids = {rl["lead_id"] for rl in body["ranked_leads"]}
    assert lead_ids_in_batch == ranked_lead_ids


def test_ranks_are_contiguous_starting_at_one(client):
    icp = _create_icp(client, "Ranking API B")
    _run_batch(client, icp["id"], target_count=2)

    body = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    ranks = sorted(rl["rank"] for rl in body["ranked_leads"])
    assert ranks == list(range(1, len(ranks) + 1))


# --- unknown ICP ---------------------------------------------------------


def test_ranking_unknown_icp_returns_404(client):
    response = client.get("/api/v1/rankings", params={"icp_id": "does-not-exist"})
    assert response.status_code == 404


def test_ranking_no_leads_yet_returns_empty_list(client):
    icp = _create_icp(client, "Ranking API C")
    body = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    assert body["ranked_leads"] == []


# --- multi-ICP reuse: same underlying leads, independent rankings ---------


def test_same_leads_ranked_independently_under_different_icps(client):
    icp_a = _create_icp(client, "Ranking API D-A", min_employees=1, max_employees=10000)
    icp_b = _create_icp(client, "Ranking API D-B", min_employees=999999, max_employees=9999999)  # guaranteed hard FAIL
    registry = _default_registry()

    batch_a = _run_batch(client, icp_a["id"], target_count=1, registry=registry)
    company_id = batch_a["items"][0]["company_id"]

    # rank the SAME underlying company under icp_b too, by running a batch there
    _run_batch(client, icp_b["id"], target_count=1, registry=registry)

    ranking_a = client.get("/api/v1/rankings", params={"icp_id": icp_a["id"]}).json()
    ranking_b = client.get("/api/v1/rankings", params={"icp_id": icp_b["id"]}).json()

    lead_a = next(rl for rl in ranking_a["ranked_leads"] if rl["company_id"] == company_id)
    lead_b = next(rl for rl in ranking_b["ranked_leads"] if rl["company_id"] == company_id)

    assert lead_a["tier"] != "HARD_FAILED"  # icp_a doesn't constrain employees -> not a hard FAIL
    assert lead_b["tier"] == "HARD_FAILED"  # icp_b's astronomical min_employees guarantees FAIL
    assert lead_a["icp_id"] != lead_b["icp_id"]


# --- batch-scoped ranking --------------------------------------------------


def test_ranking_can_be_scoped_to_one_batch(client):
    icp = _create_icp(client, "Ranking API E")
    registry = _default_registry()
    batch1 = _run_batch(client, icp["id"], target_count=1, registry=registry)

    body = client.get("/api/v1/rankings", params={"icp_id": icp["id"], "batch_id": batch1["id"]}).json()
    assert body["batch_id"] == batch1["id"]
    assert len(body["ranked_leads"]) == 1
    assert body["ranked_leads"][0]["lead_id"] == batch1["items"][0]["lead_id"]


# --- hard-fail protection through the real pipeline ------------------------


def test_hard_fail_leads_ranked_last_through_real_pipeline(client):
    icp = _create_icp(client, "Ranking API F", min_employees=999999, max_employees=9999999)
    registry = _default_registry()
    batch = _run_batch(client, icp["id"], target_count=2, registry=registry)

    # A single discovery run only produces a single, uncorroborated
    # employee_count sighting -> INSUFFICIENT evidence -> HOLD, not a
    # confirmed FAIL. A second, independent discovery run against the
    # same ICP corroborates the same values (the default mock provider is
    # deterministic, so the same companies/domains resolve to the SAME
    # canonical companies via Phase 7's domain match), which is what lets
    # Phase 12 actually confirm the employee-count violation as FAIL.
    second_run = _with_registry(
        client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    )
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": second_run["id"]})

    company_ids = [item["company_id"] for item in batch["items"]]
    for company_id in company_ids:
        client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
        client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id})

    body = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    fail_items = [rl for rl in body["ranked_leads"] if rl["signals"]["hard_rule_result"] == "FAIL"]
    assert fail_items  # this ICP guarantees FAIL once employee count is confirmed
    max_rank_of_others = max(
        (rl["rank"] for rl in body["ranked_leads"] if rl["signals"]["hard_rule_result"] != "FAIL"), default=0
    )
    min_rank_of_fail = min(rl["rank"] for rl in fail_items)
    assert min_rank_of_fail > max_rank_of_others


# --- human review affects ranking ------------------------------------------


def test_human_review_decision_changes_ranking(client):
    icp = _create_icp(client, "Ranking API G")
    batch = _run_batch(client, icp["id"], target_count=1)
    lead_id = batch["items"][0]["lead_id"]

    before = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    before_tier = before["ranked_leads"][0]["tier"]

    client.post(
        "/api/v1/human-reviews",
        json={"lead_id": lead_id, "icp_id": icp["id"], "decision": "ACCEPT", "reviewer_id": "ops-1"},
    )

    after = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    after_tier = after["ranked_leads"][0]["tier"]
    assert after_tier == "ACCEPTED"


# --- repeated ranking is deterministic through the API ----------------------


def test_repeated_ranking_calls_are_identical(client):
    icp = _create_icp(client, "Ranking API H")
    _run_batch(client, icp["id"], target_count=2)

    first = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    second = client.get("/api/v1/rankings", params={"icp_id": icp["id"]}).json()
    assert first == second


# --- snapshots -------------------------------------------------------------


def test_ranking_snapshot_is_persisted_and_retrievable(client):
    icp = _create_icp(client, "Ranking API I")
    _run_batch(client, icp["id"], target_count=1)

    created = client.post("/api/v1/rankings/snapshots", params={"icp_id": icp["id"]}).json()
    fetched = client.get(f"/api/v1/rankings/snapshots/{created['id']}").json()
    assert fetched == created
    assert created["lead_count"] == 1


def test_repeated_snapshots_are_append_only(client):
    icp = _create_icp(client, "Ranking API J")
    _run_batch(client, icp["id"], target_count=1)

    first = client.post("/api/v1/rankings/snapshots", params={"icp_id": icp["id"]}).json()
    second = client.post("/api/v1/rankings/snapshots", params={"icp_id": icp["id"]}).json()
    assert first["id"] != second["id"]

    history = client.get("/api/v1/rankings/snapshots", params={"icp_id": icp["id"]}).json()
    assert len(history) == 2


def test_get_unknown_snapshot_returns_404(client):
    response = client.get("/api/v1/rankings/snapshots/does-not-exist")
    assert response.status_code == 404


def test_list_snapshots_requires_a_filter(client):
    response = client.get("/api/v1/rankings/snapshots")
    assert response.status_code == 400


# --- no mutation of source data ---------------------------------------------


def test_ranking_never_mutates_icp_score_or_qualification(client):
    icp = _create_icp(client, "Ranking API K")
    batch = _run_batch(client, icp["id"], target_count=1)
    company_id = batch["items"][0]["company_id"]

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    scores_before = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()
    qualifications_before = client.get("/api/v1/lead-qualifications", params={"company_id": company_id}).json()

    client.get("/api/v1/rankings", params={"icp_id": icp["id"]})
    client.post("/api/v1/rankings/snapshots", params={"icp_id": icp["id"]})

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    scores_after = client.get("/api/v1/lead-scores", params={"company_id": company_id}).json()
    qualifications_after = client.get("/api/v1/lead-qualifications", params={"company_id": company_id}).json()

    assert icp_before == icp_after
    assert scores_before == scores_after
    assert qualifications_before == qualifications_after


def test_ranking_never_mutates_the_canonical_lead(client):
    icp = _create_icp(client, "Ranking API L")
    batch = _run_batch(client, icp["id"], target_count=1)
    lead_id = batch["items"][0]["lead_id"]

    lead_before = client.get(f"/api/v1/leads/{lead_id}").json()
    client.get("/api/v1/rankings", params={"icp_id": icp["id"]})
    lead_after = client.get(f"/api/v1/leads/{lead_id}").json()
    assert lead_before == lead_after
