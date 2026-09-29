from datetime import datetime, timezone

from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry


def _icp_payload(name: str, min_employees: int = 10, max_employees: int = 200) -> dict:
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


class _FixedCompanyDiscoveryProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


class _FixedEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str, field: str, value, fail: bool = False):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_ENRICHMENT})
        self._field = field
        self._value = value
        self._fail = fail

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if self._fail:
            return ProviderResponse(
                provider_id=self.provider_id, capability=request.capability, success=False,
                error=ProviderError(code="SIMULATED_FAILURE", message="simulated failure", retryable=True),
            )
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True,
            data=(NormalizedRecord(external_id="ext-verify", name="Test Co", attributes={self._field: self._value}),),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _discover_company(client, icp_id: str, employee_count: int, domain: str, provider_id: str = "p1") -> str:
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyDiscoveryProvider(
            provider_id, [NormalizedRecord(external_id="ext-co", name="Example Test Co", attributes={"domain": domain, "employee_count": employee_count})]
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _discover_second_conflicting(client, icp_id: str, employee_count: int, domain: str, provider_id: str) -> None:
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyDiscoveryProvider(
            provider_id, [NormalizedRecord(external_id="ext-co", name="Example Test Co", attributes={"domain": domain, "employee_count": employee_count})]
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]})


def _import_evidence(client, company_id: str) -> None:
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})


def _with_registry(registry, fn):
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        return fn()
    finally:
        del app.dependency_overrides[get_provider_registry]


# --- basic trigger flow through the real API --------------------------


def test_conflicting_employee_count_triggers_and_resolves_through_api(client):
    icp = _create_icp(client, "Verify API A")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-a.invalid", provider_id="disc-p1")
    _discover_second_conflicting(client, icp["id"], 500, "verify-api-a.invalid", provider_id="disc-p2")
    _import_evidence(client, company_id)

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-p3", "employee_count", 25))
    body = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    assert body["trigger"] == "CONFLICT"
    assert body["execution_status"] == "SUCCESS"
    assert body["outcome"] == "HOLD"  # 25 vs 500 vs 25 -> still a genuine 2-value disagreement


def test_verification_persists_new_evidence_rows(client):
    icp = _create_icp(client, "Verify API B")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-b.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)  # single sighting -> INSUFFICIENT

    before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"}).json()
    assert len(before) == 1

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-p2", "employee_count", 25))
    body = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    assert body["outcome"] == "RESOLVED"

    after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"}).json()
    assert len(after) == 2  # original preserved + one new row, never overwritten
    original_ids = {e["id"] for e in before}
    after_ids = {e["id"] for e in after}
    assert original_ids <= after_ids


# --- 404s ----------------------------------------------------------------


def test_verify_unknown_icp_returns_404(client):
    response = client.post(
        "/api/v1/field-verifications", json={"icp_id": "does-not-exist", "entity_type": "COMPANY", "entity_id": "x", "field": "employee_count"}
    )
    assert response.status_code == 404


def test_verify_unknown_company_returns_404(client):
    icp = _create_icp(client, "Verify API C")
    response = client.post(
        "/api/v1/field-verifications",
        json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": "does-not-exist", "field": "employee_count"},
    )
    assert response.status_code == 404


def test_get_unknown_verification_returns_404(client):
    response = client.get("/api/v1/field-verifications/does-not-exist")
    assert response.status_code == 404


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/field-verifications")
    assert response.status_code == 400


# --- same-provider duplicates never count as independent -------------------


def test_only_the_original_provider_available_yields_no_independent_providers(client):
    icp = _create_icp(client, "Verify API D")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-d.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)

    # The only enrichment provider registered shares its id with the
    # discovery provider that already produced the existing employee_count
    # evidence — it must be excluded as non-independent, never consulted.
    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("disc-p1", "employee_count", 999))
    body = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    assert body["execution_status"] == "NO_INDEPENDENT_PROVIDERS"
    assert body["new_evidence_ids"] == []


# --- provider failure through the API ---------------------------------------


def test_provider_failure_recorded_not_hidden(client):
    icp = _create_icp(client, "Verify API E")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-e.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-fail", "employee_count", 25, fail=True))
    body = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    assert body["execution_status"] == "ALL_PROVIDERS_FAILED"
    assert body["providers_consulted"][0]["success"] is False


# --- history append-only ----------------------------------------------------


def test_repeated_verification_preserves_history(client):
    icp = _create_icp(client, "Verify API F")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-f.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-p2", "employee_count", 25))

    first = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    # after resolution, the field is now SUPPORTED, so a second attempt is NOT_TRIGGERED
    second = client.post(
        "/api/v1/field-verifications",
        json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
    ).json()

    assert first["id"] != second["id"]
    history = client.get("/api/v1/field-verifications", params={"entity_id": company_id}).json()
    assert len(history) == 2


def test_verification_is_retrievable_by_id(client):
    icp = _create_icp(client, "Verify API G")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-g.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-p2", "employee_count", 25))
    created = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    fetched = client.get(f"/api/v1/field-verifications/{created['id']}").json()
    assert fetched == created


# --- multi-ICP reuse ------------------------------------------------------


def test_verification_reusable_across_icps_for_same_company(client):
    icp_a = _create_icp(client, "Verify API H-A")
    icp_b = _create_icp(client, "Verify API H-B")
    company_id = _discover_company(client, icp_a["id"], 25, "verify-api-h.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-p2", "employee_count", 25))

    body_a = _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp_a["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ).json(),
    )
    assert body_a["icp_id"] == icp_a["id"]
    assert body_a["outcome"] == "RESOLVED"

    # a second ICP can independently trigger verification history against the SAME evidence pool
    body_b = client.post(
        "/api/v1/field-verifications",
        json={"icp_id": icp_b["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
    ).json()
    assert body_b["icp_id"] == icp_b["id"]
    assert body_b["execution_status"] == "NOT_TRIGGERED"  # already resolved by icp_a's verification


# --- no ICP / hard-rule mutation --------------------------------------------


def test_verification_never_mutates_icp_or_hard_rule_state(client):
    icp = _create_icp(client, "Verify API I")
    company_id = _discover_company(client, icp["id"], 25, "verify-api-i.invalid", provider_id="disc-p1")
    _import_evidence(client, company_id)

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()

    registry = ProviderRegistry()
    registry.register(_FixedEnrichmentProvider("enrich-p2", "employee_count", 25))
    _with_registry(
        registry,
        lambda: client.post(
            "/api/v1/field-verifications",
            json={"icp_id": icp["id"], "entity_type": "COMPANY", "entity_id": company_id, "field": "employee_count"},
        ),
    )

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    assert icp_before == icp_after
