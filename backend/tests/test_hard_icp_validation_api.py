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
) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare"] if industry is None else industry,
            "geography": ["United States"] if geography is None else geography,
            "min_employees": min_employees,
            "max_employees": max_employees,
            "allowed_titles": ["CMO"] if allowed_titles is None else allowed_titles,
            "company_type": ["D2C"] if company_type is None else company_type,
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": ["Subscription"],
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


class _FixedPeopleProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.PEOPLE_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _discover_company(client, icp_id: str, providers: list[ProviderAdapter], domain: str = "example-test.invalid") -> str:
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


def _discover_person(client, icp_id: str, company_id: str, providers: list[ProviderAdapter]) -> str:
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_id, "company_id": company_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_person_id"]


def _company_provider(provider_id: str, employee_count: int, domain: str = "example-test.invalid") -> _FixedCompanyProvider:
    return _FixedCompanyProvider(provider_id, [NormalizedRecord(external_id="ext-co", name="Example Test Co", attributes={"domain": domain, "employee_count": employee_count})])


def _people_provider(provider_id: str, title: str = "CMO") -> _FixedPeopleProvider:
    return _FixedPeopleProvider(provider_id, [NormalizedRecord(external_id="ext-person", name="Jane Testperson", attributes={"title": title})])


def _import_company_evidence(client, company_id: str) -> None:
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})


def _import_person_evidence(client, person_id: str) -> None:
    client.post("/api/v1/evidence/import", json={"entity_type": "PERSON", "entity_id": person_id})


# --- basic flow -----------------------------------------------------


def test_validate_holds_with_only_a_single_unconfirmed_source(client):
    """One discovery sighting per field is realistic and honest — it
    should HOLD, not guess a PASS."""
    icp = _create_icp(client, "ICP Validate A")
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=50)])
    _import_company_evidence(client, company_id)

    response = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id})
    assert response.status_code == 201
    body = response.json()
    assert body["overall_result"] == "HOLD"
    assert body["icp_id"] == icp["id"]
    assert body["company_id"] == company_id


def test_validate_passes_when_two_independent_sources_corroborate(client):
    # Only the employee-range rule is configured — every other hard rule
    # (title/industry/geography/company type) has no matching evidence in
    # this test and would otherwise HOLD, which isn't what this test is
    # about; it isolates the company-only corroboration -> PASS path.
    icp = _create_icp(client, "ICP Validate B", allowed_titles=[], industry=[], geography=[], company_type=[])
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=50)])
    # a second, independent discovery run + provider corroborates the same facts
    registry = ProviderRegistry()
    registry.register(_company_provider("p2", employee_count=50))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run2 = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]})
    _import_company_evidence(client, company_id)

    response = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id})
    body = response.json()
    assert body["overall_result"] == "PASS"


def test_validate_fails_on_confirmed_employee_violation(client):
    icp = _create_icp(client, "ICP Validate C", max_employees=200)
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=500)])
    registry = ProviderRegistry()
    registry.register(_company_provider("p2", employee_count=500))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run2 = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]})
    _import_company_evidence(client, company_id)

    response = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id})
    body = response.json()
    assert body["overall_result"] == "FAIL"
    assert "EMPLOYEE_TOO_LARGE" in body["reason_codes"]


# --- 404s -----------------------------------------------------------


def test_validate_unknown_icp_returns_404(client):
    response = client.post("/api/v1/hard-icp-validations", json={"icp_id": "does-not-exist", "company_id": "x"})
    assert response.status_code == 404


def test_validate_unknown_company_returns_404(client):
    icp = _create_icp(client, "ICP Validate D")
    response = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": "does-not-exist"})
    assert response.status_code == 404


def test_validate_unknown_person_returns_404(client):
    icp = _create_icp(client, "ICP Validate E")
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=50)])
    response = client.post(
        "/api/v1/hard-icp-validations",
        json={"icp_id": icp["id"], "company_id": company_id, "person_id": "does-not-exist"},
    )
    assert response.status_code == 404


def test_get_unknown_validation_returns_404(client):
    response = client.get("/api/v1/hard-icp-validations/does-not-exist")
    assert response.status_code == 404


# --- person validation -------------------------------------------------


def test_validate_holds_on_unconfirmed_title(client):
    icp = _create_icp(client, "ICP Validate F")
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=50)])
    person_id = _discover_person(client, icp["id"], company_id, [_people_provider("pp1", title="CMO")])
    _import_person_evidence(client, person_id)

    response = client.post(
        "/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id, "person_id": person_id}
    )
    body = response.json()
    title_rule = next(r for r in body["rule_results"] if r["rule"] == "allowed_titles")
    assert title_rule["status"] == "HOLD"  # single sighting is not verified employment


# --- multi-ICP / history -------------------------------------------------


def test_same_company_validated_against_multiple_icps_preserves_both_results(client):
    icp_a = _create_icp(client, "ICP Validate G-A", max_employees=1000, allowed_titles=[], industry=[], geography=[], company_type=[])
    icp_b = _create_icp(client, "ICP Validate G-B", max_employees=100, allowed_titles=[], industry=[], geography=[], company_type=[])
    company_id = _discover_company(client, icp_a["id"], [_company_provider("p1", employee_count=500)])
    registry = ProviderRegistry()
    registry.register(_company_provider("p2", employee_count=500))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run2 = client.post("/api/v1/discovery/runs", json={"icp_id": icp_a["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]})
    _import_company_evidence(client, company_id)

    result_a = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_a["id"], "company_id": company_id}).json()
    result_b = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_b["id"], "company_id": company_id}).json()

    assert result_a["overall_result"] == "PASS"
    assert result_b["overall_result"] == "FAIL"
    assert result_a["id"] != result_b["id"]

    by_icp_a = client.get("/api/v1/hard-icp-validations", params={"icp_id": icp_a["id"]}).json()
    by_icp_b = client.get("/api/v1/hard-icp-validations", params={"icp_id": icp_b["id"]}).json()
    assert len(by_icp_a) == 1
    assert len(by_icp_b) == 1


def test_repeated_validation_does_not_destroy_history(client):
    icp = _create_icp(client, "ICP Validate H")
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=50)])
    _import_company_evidence(client, company_id)

    first = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id}).json()
    second = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id}).json()

    assert first["id"] != second["id"]  # both preserved, neither overwritten
    by_company = client.get("/api/v1/hard-icp-validations", params={"company_id": company_id}).json()
    assert len(by_company) == 2


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/hard-icp-validations")
    assert response.status_code == 400


# --- evidence provenance -------------------------------------------------


def test_evidence_ids_are_persisted_and_retrievable(client):
    icp = _create_icp(client, "ICP Validate I")
    company_id = _discover_company(client, icp["id"], [_company_provider("p1", employee_count=50)])
    registry = ProviderRegistry()
    registry.register(_company_provider("p2", employee_count=50))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run2 = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]})
    _import_company_evidence(client, company_id)

    created = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp["id"], "company_id": company_id}).json()
    assert len(created["evidence_ids"]["employee_range"]) == 2

    fetched = client.get(f"/api/v1/hard-icp-validations/{created['id']}").json()
    assert fetched == created


# --- soft preferences have zero effect (through the real API) -----------


def test_soft_preferences_have_zero_effect_through_the_api(client):
    payload_a = _icp_payload("ICP Validate J-A")
    payload_a["soft_preferences"]["growth_signals"] = ["Hiring for growth roles"]
    icp_a = client.post("/api/v1/icps", json=payload_a).json()

    payload_b = _icp_payload("ICP Validate J-B")
    icp_b = client.post("/api/v1/icps", json=payload_b).json()

    company_id_a = _discover_company(client, icp_a["id"], [_company_provider("p1", employee_count=50)])
    company_id_b = _discover_company(client, icp_b["id"], [_company_provider("p1b", employee_count=50)])
    _import_company_evidence(client, company_id_a)
    _import_company_evidence(client, company_id_b)

    result_a = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_a["id"], "company_id": company_id_a}).json()
    result_b = client.post("/api/v1/hard-icp-validations", json={"icp_id": icp_b["id"], "company_id": company_id_b}).json()

    assert result_a["overall_result"] == result_b["overall_result"] == "HOLD"
    assert result_a["reason_codes"] == result_b["reason_codes"]
