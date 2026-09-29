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


class _FixedCompanyProvider(ProviderAdapter):
    def __init__(self, provider_id: str, employee_count: int, domain: str):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._employee_count = employee_count
        self._domain = domain

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True,
            data=(NormalizedRecord(external_id="ext-co", name="Example Test Co", attributes={"domain": self._domain, "employee_count": self._employee_count}),),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _discover_and_resolve(client, icp_id: str, domain: str, employee_count: int = 50, provider_id: str = "p1") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider(provider_id, employee_count, domain))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _discover_second_corroborating(client, icp_id: str, domain: str, employee_count: int, provider_id: str) -> None:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider(provider_id, employee_count, domain))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]})


def _make_lead(client, icp_id: str, domain: str, employee_count: int = 50) -> tuple[str, str]:
    company_id = _discover_and_resolve(client, icp_id, domain, employee_count, provider_id="disc-p1")
    _discover_second_corroborating(client, icp_id, domain, employee_count, provider_id="disc-p2")
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    dedup = client.post("/api/v1/lead-deduplications", json={"icp_id": icp_id, "company_id": company_id}).json()
    return dedup["lead_id"], company_id


def _validate_hard_icp(client, icp_id: str, company_id: str) -> dict:
    return client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_id, "company_id": company_id}).json()


def _review(client, lead_id, icp_id, decision, **kwargs):
    payload = {"lead_id": lead_id, "icp_id": icp_id, "decision": decision, "reviewer_id": "reviewer-1"}
    payload.update(kwargs)
    return client.post("/api/v1/human-reviews", json=payload)


# --- ACCEPT/REJECT/HOLD/DUPLICATE through the real API ----------------------


def test_accept_on_a_passing_lead(client):
    icp = _create_icp(client, "Review API A")
    lead_id, company_id = _make_lead(client, icp["id"], "review-api-a.invalid")
    validation = _validate_hard_icp(client, icp["id"], company_id)
    assert validation["overall_result"] == "PASS"

    response = _review(client, lead_id, icp["id"], "ACCEPT")
    body = response.json()
    assert response.status_code == 201
    assert body["effective_decision"] == "ACCEPT"
    assert body["status"] == "RECORDED"
    assert body["forwarded_feedback_id"] is not None

    feedback = client.get(f"/api/v1/feedback/{body['forwarded_feedback_id']}").json()
    assert feedback["decision"] == "GOOD_FIT"
    assert feedback["lead_ref"] == lead_id


def test_reject_with_reason_codes(client):
    icp = _create_icp(client, "Review API B")
    lead_id, company_id = _make_lead(client, icp["id"], "review-api-b.invalid")

    response = _review(client, lead_id, icp["id"], "REJECT", reason_codes=["POOR_COMMERCIAL_FIT"])
    body = response.json()
    assert body["effective_decision"] == "REJECT"
    feedback = client.get(f"/api/v1/feedback/{body['forwarded_feedback_id']}").json()
    assert feedback["decision"] == "NOT_FIT"
    assert feedback["reason_codes"] == ["POOR_COMMERCIAL_FIT"]


def test_hold_decision(client):
    icp = _create_icp(client, "Review API C")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-c.invalid")

    response = _review(client, lead_id, icp["id"], "HOLD", reason_codes=["AWAITING_MORE_EVIDENCE"])
    body = response.json()
    assert body["effective_decision"] == "HOLD"
    feedback = client.get(f"/api/v1/feedback/{body['forwarded_feedback_id']}").json()
    assert feedback["decision"] == "HOLD"


def test_duplicate_decision_references_another_lead(client):
    icp = _create_icp(client, "Review API D")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-d.invalid")
    other_lead_id, _ = _make_lead(client, icp["id"], "review-api-d-other.invalid")

    response = _review(client, lead_id, icp["id"], "DUPLICATE", duplicate_of_lead_id=other_lead_id)
    body = response.json()
    assert body["effective_decision"] == "DUPLICATE"
    assert body["duplicate_of_lead_id"] == other_lead_id
    assert body["forwarded_feedback_id"] is None  # DUPLICATE has no Phase 4 feedback equivalent


def test_duplicate_referencing_unknown_lead_returns_404(client):
    icp = _create_icp(client, "Review API E")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-e.invalid")

    response = _review(client, lead_id, icp["id"], "DUPLICATE", duplicate_of_lead_id="does-not-exist")
    assert response.status_code == 404


# --- hard-rule failure protection -------------------------------------


def test_accept_is_blocked_on_hard_fail_through_the_api(client):
    icp = _create_icp(client, "Review API F", min_employees=1, max_employees=1)
    lead_id, company_id = _make_lead(client, icp["id"], "review-api-f.invalid", employee_count=500)
    validation = _validate_hard_icp(client, icp["id"], company_id)
    assert validation["overall_result"] == "FAIL"

    response = _review(client, lead_id, icp["id"], "ACCEPT")
    body = response.json()
    assert body["effective_decision"] == "REJECT"
    assert body["status"] == "BLOCKED_BY_HARD_FAIL"
    assert body["requested_decision"] == "ACCEPT"

    feedback = client.get(f"/api/v1/feedback/{body['forwarded_feedback_id']}").json()
    assert feedback["decision"] == "NOT_FIT"  # forwarded feedback reflects the EFFECTIVE decision, not the request
    assert "HARD_ICP_FAILURE" in body["reason_codes"]
    assert "HARD_ICP_FAILURE" in feedback["reason_codes"]


def test_reject_and_hold_not_blocked_on_hard_fail(client):
    icp = _create_icp(client, "Review API G", min_employees=1, max_employees=1)
    lead_id, company_id = _make_lead(client, icp["id"], "review-api-g.invalid", employee_count=500)
    _validate_hard_icp(client, icp["id"], company_id)

    reject_body = _review(client, lead_id, icp["id"], "REJECT", reason_codes=["CONFIRMED_TOO_LARGE"]).json()
    assert reject_body["status"] == "RECORDED"
    hold_body = _review(client, lead_id, icp["id"], "HOLD", reason_codes=["AWAITING_MORE_EVIDENCE"]).json()
    assert hold_body["status"] == "RECORDED"


# --- missing/conflicting evidence visible in the snapshot -------------------


def test_snapshot_shows_conflicting_evidence(client):
    icp = _create_icp(client, "Review API H")
    lead_id, company_id = _make_lead(client, icp["id"], "review-api-h.invalid")
    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY", "entity_id": company_id, "field": "industry", "value": "Skincare",
            "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY", "entity_id": company_id, "field": "industry", "value": "Something Else",
            "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )

    body = _review(client, lead_id, icp["id"], "HOLD", reason_codes=["AWAITING_MORE_EVIDENCE"]).json()
    assert "industry" in body["snapshot"]["evidence_conflicts"]


# --- 404s --------------------------------------------------------------


def test_review_unknown_lead_returns_404(client):
    icp = _create_icp(client, "Review API I")
    response = _review(client, "does-not-exist", icp["id"], "ACCEPT")
    assert response.status_code == 404


def test_review_unknown_icp_returns_404(client):
    icp = _create_icp(client, "Review API J")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-j.invalid")
    response = _review(client, lead_id, "does-not-exist", "ACCEPT")
    assert response.status_code == 404


def test_get_unknown_review_returns_404(client):
    response = client.get("/api/v1/human-reviews/does-not-exist")
    assert response.status_code == 404


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/human-reviews")
    assert response.status_code == 400


def test_queue_unknown_icp_returns_404(client):
    response = client.get("/api/v1/human-reviews/queue/does-not-exist")
    assert response.status_code == 404


# --- repeated review history / append-only ----------------------------


def test_repeated_review_preserves_history(client):
    icp = _create_icp(client, "Review API K")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-k.invalid")

    first = _review(client, lead_id, icp["id"], "HOLD", reason_codes=["AWAITING_MORE_EVIDENCE"]).json()
    second = _review(client, lead_id, icp["id"], "ACCEPT").json()

    assert first["id"] != second["id"]
    history = client.get("/api/v1/human-reviews", params={"lead_id": lead_id}).json()
    assert len(history) == 2
    assert history[0]["effective_decision"] == "HOLD"
    assert history[1]["effective_decision"] == "ACCEPT"


# --- multi-ICP isolation ---------------------------------------------------


def test_same_lead_reviewed_separately_under_different_icps(client):
    icp_a = _create_icp(client, "Review API L-A")
    icp_b = _create_icp(client, "Review API L-B")
    lead_id, company_id = _make_lead(client, icp_a["id"], "review-api-l.invalid")
    client.post("/api/v1/lead-deduplications", json={"icp_id": icp_b["id"], "company_id": company_id})

    review_a = _review(client, lead_id, icp_a["id"], "ACCEPT").json()
    review_b = _review(client, lead_id, icp_b["id"], "REJECT", reason_codes=["WRONG_ICP"]).json()

    assert review_a["id"] != review_b["id"]
    assert review_a["icp_id"] != review_b["icp_id"]

    by_icp_a = client.get("/api/v1/human-reviews", params={"icp_id": icp_a["id"]}).json()
    by_icp_b = client.get("/api/v1/human-reviews", params={"icp_id": icp_b["id"]}).json()
    assert len(by_icp_a) == 1
    assert len(by_icp_b) == 1


# --- review queue read model -----------------------------------------------


def test_review_queue_lists_leads_and_review_state(client):
    icp = _create_icp(client, "Review API M")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-m.invalid")

    queue_before = client.get(f"/api/v1/human-reviews/queue/{icp['id']}").json()
    entry = next(item for item in queue_before if item["lead_id"] == lead_id)
    assert entry["already_reviewed"] is False

    _review(client, lead_id, icp["id"], "ACCEPT")

    queue_after = client.get(f"/api/v1/human-reviews/queue/{icp['id']}").json()
    entry_after = next(item for item in queue_after if item["lead_id"] == lead_id)
    assert entry_after["already_reviewed"] is True
    assert entry_after["latest_review_decision"] == "ACCEPT"


# --- no ICP / evidence mutation ---------------------------------------------


def test_review_never_mutates_icp_or_evidence(client):
    icp = _create_icp(client, "Review API N")
    lead_id, company_id = _make_lead(client, icp["id"], "review-api-n.invalid")

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    _review(client, lead_id, icp["id"], "ACCEPT")

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    assert icp_before == icp_after
    assert evidence_before == evidence_after


def test_review_is_retrievable_by_id(client):
    icp = _create_icp(client, "Review API O")
    lead_id, _ = _make_lead(client, icp["id"], "review-api-o.invalid")

    created = _review(client, lead_id, icp["id"], "HOLD", reason_codes=["AWAITING_MORE_EVIDENCE"]).json()
    fetched = client.get(f"/api/v1/human-reviews/{created['id']}").json()
    assert fetched == created
