import json
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
from app.services.llm_providers.default_registry import get_llm_provider
from app.services.llm_providers.mock import MockLLMProvider


def _icp_payload(
    name: str,
    min_employees: int = 10,
    max_employees: int = 200,
    industry: list[str] | None = None,
    geography: list[str] | None = None,
    company_type: list[str] | None = None,
    business_model_preferences: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": [] if industry is None else industry,
            "geography": [] if geography is None else geography,
            "min_employees": min_employees,
            "max_employees": max_employees,
            "allowed_titles": [],
            "company_type": [] if company_type is None else company_type,
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [] if business_model_preferences is None else business_model_preferences,
            "commercial_signals": [],
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


def _with_llm_provider(client, provider, fn):
    app.dependency_overrides[get_llm_provider] = lambda: provider
    try:
        return fn()
    finally:
        del app.dependency_overrides[get_llm_provider]


def _qualify(client, icp_id, company_id):
    return client.post("/api/v1/lead-qualifications", json={"icp_id": icp_id, "company_id": company_id}).json()


def _survives_response(evidence_ids):
    return json.dumps(
        {
            "adversarial_result": "SURVIVES",
            "confidence": 75,
            "contradictions": [],
            "risk_codes": [],
            "supporting_evidence_ids": evidence_ids,
            "contradicting_evidence_ids": [],
            "unsupported_claims": [],
            "missing_evidence": [],
            "reasoning_summary": "No contradiction found.",
            "recommendation": "Proceed.",
        }
    )


# --- hard gate through the real API ----------------------------------------


def test_hard_fail_first_pass_blocks_adversarial_without_calling_llm(client):
    icp = _create_icp(client, "Adv API A", min_employees=1, max_employees=1)
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-a.invalid", employee_count=500)
    _import_company_evidence(client, company_id)

    qualification = _qualify(client, icp["id"], company_id)
    assert qualification["hard_rule_result"] == "FAIL"

    class ExplodingProvider(MockLLMProvider):
        def _call(self, ctx):
            raise AssertionError("adversarial LLM must never be called for a hard FAIL")

    body = _with_llm_provider(
        client, ExplodingProvider(),
        lambda: client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json(),
    )
    assert body["hard_rule_result"] == "FAIL"
    assert body["status"] == "HARD_BLOCKED"
    assert body["adversarial_result"] == "NOT_EXECUTED"


def test_hard_hold_first_pass_blocks_adversarial(client):
    icp = _create_icp(client, "Adv API B", industry=[], geography=[], company_type=[])
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", 50, "adv-api-b.invalid")])
    _import_company_evidence(client, company_id)  # single sighting -> HOLD

    qualification = _qualify(client, icp["id"], company_id)
    assert qualification["hard_rule_result"] == "HOLD"

    class ExplodingProvider(MockLLMProvider):
        def _call(self, ctx):
            raise AssertionError("adversarial LLM must never be called for a hard HOLD")

    body = _with_llm_provider(
        client, ExplodingProvider(),
        lambda: client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json(),
    )
    assert body["status"] == "HARD_BLOCKED"
    assert body["adversarial_result"] == "NOT_EXECUTED"


def test_hard_pass_reaches_the_adversarial_llm_through_the_api(client):
    icp = _create_icp(client, "Adv API C")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-c.invalid")
    _import_company_evidence(client, company_id)

    qualification = _qualify(client, icp["id"], company_id)
    assert qualification["hard_rule_result"] == "PASS"

    body = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json()
    assert body["status"] == "SUCCESS"
    assert body["adversarial_result"] in {"SURVIVES", "WEAKENED", "DISPROVED", "HOLD"}


# --- 404s --------------------------------------------------------------


def test_review_unknown_qualification_returns_404(client):
    response = client.post("/api/v1/adversarial-reviews", json={"qualification_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_review_returns_404(client):
    response = client.get("/api/v1/adversarial-reviews/does-not-exist")
    assert response.status_code == 404


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/adversarial-reviews")
    assert response.status_code == 400


# --- provider failure through the API --------------------------------------


def test_provider_failure_is_persisted_not_hidden(client):
    icp = _create_icp(client, "Adv API D")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-d.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    body = _with_llm_provider(
        client, MockLLMProvider(raise_provider_error=True),
        lambda: client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json(),
    )
    assert body["status"] == "PROVIDER_ERROR"
    assert body["adversarial_result"] is None
    assert body["error_message"] is not None

    history = client.get("/api/v1/adversarial-reviews", params={"qualification_id": qualification["id"]}).json()
    assert any(h["status"] == "PROVIDER_ERROR" for h in history)


def test_fabricated_evidence_id_is_rejected_through_the_api(client):
    icp = _create_icp(client, "Adv API E")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-e.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    body = _with_llm_provider(
        client, MockLLMProvider(response_text=_survives_response(["fabricated-evidence-id"])),
        lambda: client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json(),
    )
    assert body["status"] == "INVALID_EVIDENCE_IDS"
    assert body["adversarial_result"] is None


def test_malformed_output_rejected_through_the_api(client):
    icp = _create_icp(client, "Adv API F")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-f.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    body = _with_llm_provider(
        client, MockLLMProvider(response_text="not json"),
        lambda: client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json(),
    )
    assert body["status"] == "MALFORMED_OUTPUT"


# --- multi-ICP isolation and history ----------------------------------------


def test_same_company_reviewed_differently_under_different_icps(client):
    icp_a = _create_icp(client, "Adv API G-A", business_model_preferences=["DTC"])
    icp_b = _create_icp(client, "Adv API G-B", business_model_preferences=["B2B"])
    company_id = _resolve_two_corroborating_runs(client, icp_a["id"], "adv-api-g.invalid")
    _import_company_evidence(client, company_id)

    qual_a = _qualify(client, icp_a["id"], company_id)
    qual_b = _qualify(client, icp_b["id"], company_id)

    review_a = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qual_a["id"]}).json()
    review_b = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qual_b["id"]}).json()

    assert review_a["id"] != review_b["id"]
    assert review_a["icp_id"] != review_b["icp_id"]

    by_icp_a = client.get("/api/v1/adversarial-reviews", params={"icp_id": icp_a["id"]}).json()
    by_icp_b = client.get("/api/v1/adversarial-reviews", params={"icp_id": icp_b["id"]}).json()
    assert len(by_icp_a) == 1
    assert len(by_icp_b) == 1


def test_repeated_review_preserves_history_never_overwrites(client):
    icp = _create_icp(client, "Adv API H")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-h.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    first = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json()
    second = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json()

    assert first["id"] != second["id"]
    history = client.get("/api/v1/adversarial-reviews", params={"qualification_id": qualification["id"]}).json()
    assert len(history) == 2


def test_review_is_retrievable_by_id(client):
    icp = _create_icp(client, "Adv API I")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-i.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    created = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json()
    fetched = client.get(f"/api/v1/adversarial-reviews/{created['id']}").json()
    assert fetched == created


# --- no mutation of ICP / evidence / first qualification / scores ----------


def test_review_never_mutates_icp_evidence_or_first_qualification(client):
    icp = _create_icp(client, "Adv API J")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-j.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    qualification_before = client.get(f"/api/v1/lead-qualifications/{qualification['id']}").json()

    client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]})

    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    qualification_after = client.get(f"/api/v1/lead-qualifications/{qualification['id']}").json()

    assert evidence_before == evidence_after
    assert icp_before == icp_after
    assert qualification_before == qualification_after


def test_no_manager_feedback_field_on_the_response(client):
    icp = _create_icp(client, "Adv API K")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "adv-api-k.invalid")
    _import_company_evidence(client, company_id)
    qualification = _qualify(client, icp["id"], company_id)

    body = client.post("/api/v1/adversarial-reviews", json={"qualification_id": qualification["id"]}).json()
    forbidden = {"manager_feedback", "manager_decision"}
    assert forbidden.isdisjoint(body.keys())
