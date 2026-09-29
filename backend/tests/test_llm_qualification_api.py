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
from app.services.llm_providers.mock import MockLLMProvider, build_good_fit_response


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


# --- hard gate through the real API ----------------------------------------


def test_hard_fail_yields_reject_without_calling_the_llm(client):
    icp = _create_icp(client, "Qual API A", min_employees=1, max_employees=1)
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-a.invalid", employee_count=500)
    _import_company_evidence(client, company_id)

    class ExplodingProvider(MockLLMProvider):
        def _call(self, context):
            raise AssertionError("the LLM must never be called for a hard FAIL")

    body = _with_llm_provider(
        client, ExplodingProvider(),
        lambda: client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json(),
    )
    assert body["hard_rule_result"] == "FAIL"
    assert body["status"] == "HARD_REJECTED"
    assert body["decision"] == "REJECT"


def test_hard_hold_never_becomes_accepted_through_the_api(client):
    icp = _create_icp(client, "Qual API B", industry=[], geography=[], company_type=[])
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", 50, "qual-api-b.invalid")])
    _import_company_evidence(client, company_id)  # single sighting -> HOLD on employee_range

    class ExplodingProvider(MockLLMProvider):
        def _call(self, context):
            raise AssertionError("the LLM must never be called for a hard HOLD")

    body = _with_llm_provider(
        client, ExplodingProvider(),
        lambda: client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json(),
    )
    assert body["hard_rule_result"] == "HOLD"
    assert body["status"] == "HARD_HOLD"
    assert body["decision"] == "HOLD"


def test_hard_pass_reaches_the_llm_through_the_api(client):
    icp = _create_icp(client, "Qual API C")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-c.invalid")
    _import_company_evidence(client, company_id)

    def make_provider(context):
        return MockLLMProvider(response_text=build_good_fit_response(context))

    # first call with the default provider to learn real evidence ids, then override deterministically
    default_body = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json()
    assert default_body["hard_rule_result"] == "PASS"
    assert default_body["status"] == "SUCCESS"
    assert default_body["decision"] == "GOOD_FIT"


# --- 404s --------------------------------------------------------------


def test_qualify_unknown_icp_returns_404(client):
    response = client.post("/api/v1/lead-qualifications", json={"icp_id": "does-not-exist", "company_id": "x"})
    assert response.status_code == 404


def test_qualify_unknown_company_returns_404(client):
    icp = _create_icp(client, "Qual API D")
    response = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_qualification_returns_404(client):
    response = client.get("/api/v1/lead-qualifications/does-not-exist")
    assert response.status_code == 404


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/lead-qualifications")
    assert response.status_code == 400


# --- provider failure through the API --------------------------------------


def test_provider_failure_is_persisted_not_hidden(client):
    icp = _create_icp(client, "Qual API E")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-e.invalid")
    _import_company_evidence(client, company_id)

    body = _with_llm_provider(
        client, MockLLMProvider(raise_provider_error=True),
        lambda: client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json(),
    )
    assert body["status"] == "PROVIDER_ERROR"
    assert body["decision"] is None
    assert body["error_message"] is not None

    history = client.get("/api/v1/lead-qualifications", params={"company_id": company_id}).json()
    assert any(h["status"] == "PROVIDER_ERROR" for h in history)


def test_fabricated_evidence_id_is_rejected_through_the_api(client):
    icp = _create_icp(client, "Qual API F")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-f.invalid")
    _import_company_evidence(client, company_id)

    fabricated = json.dumps(
        {
            "decision": "GOOD_FIT", "confidence": 80, "reason_codes": [], "summary": "x",
            "supporting_evidence_ids": ["fabricated-evidence-id"], "risk_evidence_ids": [],
            "missing_evidence": [], "commercial_fit_explanation": "", "hard_rule_acknowledgement": "", "uncertainties": [],
        }
    )
    body = _with_llm_provider(
        client, MockLLMProvider(response_text=fabricated),
        lambda: client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json(),
    )
    assert body["status"] == "INVALID_EVIDENCE_IDS"
    assert body["decision"] is None


def test_malformed_output_is_rejected_through_the_api(client):
    icp = _create_icp(client, "Qual API G")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-g.invalid")
    _import_company_evidence(client, company_id)

    body = _with_llm_provider(
        client, MockLLMProvider(response_text="not json"),
        lambda: client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json(),
    )
    assert body["status"] == "MALFORMED_OUTPUT"


# --- multi-ICP isolation and history ----------------------------------------


def test_same_company_qualified_differently_under_different_icps(client):
    icp_a = _create_icp(client, "Qual API H-A", business_model_preferences=["DTC"])
    icp_b = _create_icp(client, "Qual API H-B", business_model_preferences=["B2B"])
    company_id = _resolve_two_corroborating_runs(client, icp_a["id"], "qual-api-h.invalid")
    _import_company_evidence(client, company_id)

    result_a = client.post("/api/v1/lead-qualifications", json={"icp_id": icp_a["id"], "company_id": company_id}).json()
    result_b = client.post("/api/v1/lead-qualifications", json={"icp_id": icp_b["id"], "company_id": company_id}).json()

    assert result_a["id"] != result_b["id"]
    assert result_a["icp_id"] != result_b["icp_id"]

    by_icp_a = client.get("/api/v1/lead-qualifications", params={"icp_id": icp_a["id"]}).json()
    by_icp_b = client.get("/api/v1/lead-qualifications", params={"icp_id": icp_b["id"]}).json()
    assert len(by_icp_a) == 1
    assert len(by_icp_b) == 1


def test_repeated_qualification_preserves_history_never_overwrites(client):
    icp = _create_icp(client, "Qual API I")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-i.invalid")
    _import_company_evidence(client, company_id)

    first = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json()
    second = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json()

    assert first["id"] != second["id"]
    history = client.get("/api/v1/lead-qualifications", params={"company_id": company_id}).json()
    assert len(history) == 2


def test_qualification_is_retrievable_by_id(client):
    icp = _create_icp(client, "Qual API J")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-j.invalid")
    _import_company_evidence(client, company_id)

    created = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json()
    fetched = client.get(f"/api/v1/lead-qualifications/{created['id']}").json()
    assert fetched == created


# --- no mutation of ICP / score / evidence / manager feedback --------------


def test_qualification_never_mutates_the_icp_or_evidence(client):
    icp = _create_icp(client, "Qual API K")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-k.invalid")
    _import_company_evidence(client, company_id)

    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()

    client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id})

    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()

    assert evidence_before == evidence_after
    assert icp_before == icp_after


def test_no_manager_feedback_field_on_the_response(client):
    icp = _create_icp(client, "Qual API L")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-l.invalid")
    _import_company_evidence(client, company_id)

    body = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json()
    forbidden = {"manager_feedback", "manager_decision"}
    assert forbidden.isdisjoint(body.keys())


# --- score snapshot for audit -----------------------------------------------


def test_score_snapshot_is_persisted_for_audit(client):
    icp = _create_icp(client, "Qual API M")
    company_id = _resolve_two_corroborating_runs(client, icp["id"], "qual-api-m.invalid")
    _import_company_evidence(client, company_id)

    body = client.post("/api/v1/lead-qualifications", json={"icp_id": icp["id"], "company_id": company_id}).json()
    assert "final_score" in body["score_snapshot"]
    assert "icp_score" in body["score_snapshot"]
