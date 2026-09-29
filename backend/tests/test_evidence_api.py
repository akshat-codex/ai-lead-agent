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
from app.providers.mocks import MockCompanyDataProvider, MockCompanyRegistryProvider
from app.providers.registry import ProviderRegistry


def _icp_payload(name: str) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare"],
            "geography": ["United States"],
            "min_employees": 10,
            "max_employees": 200,
            "allowed_titles": ["CMO"],
            "company_type": ["D2C"],
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [],
            "commercial_signals": [],
            "growth_signals": [],
            "marketing_signals": [],
            "other_preferences": [],
        },
    }


def _create_icp(client, name: str) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name)).json()


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


def _resolve_a_company(client, icp_id: str, domain: str = "example-test.invalid") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider("fixed-company-provider", [NormalizedRecord(external_id="ext-1", name="Example Test Co", attributes={"domain": domain})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _resolve_a_person(client, icp_id: str, company_id: str, name: str = "Jane Testperson", title: str = "CMO") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedPeopleProvider("fixed-people-provider", [NormalizedRecord(external_id="ext-p1", name=name, attributes={"title": title})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_id, "company_id": company_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_person_id"]


def _enrich_with(client, company_id: str, providers: list[ProviderAdapter]) -> None:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        client.post(f"/api/v1/companies/{company_id}/enrich")
    finally:
        del app.dependency_overrides[get_provider_registry]


# --- company evidence import --------------------------------------------


def test_import_company_evidence_from_discovery_and_enrichment(client):
    icp = _create_icp(client, "ICP Evidence A")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider()])

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    body = response.json()
    assert body["skipped_duplicates"] == 0
    fields = {r["field"] for r in body["added"]}
    assert "company_identity" in fields  # from discovery
    assert "domain" in fields  # from discovery AND enrichment
    assert "industry" in fields  # from enrichment


def test_import_company_evidence_reads_industry_and_country_from_discovery(client):
    """A real discovery provider (e.g. Explorium) may already state
    industry/country directly on the candidate — collect_company_evidence
    must surface those into evidence, not just company_identity/domain/
    employee_count, or Phase 12's hard-rule engine could never resolve
    industry/country from discovery data alone."""
    icp = _create_icp(client, "ICP Evidence A2")
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "fixed-company-provider",
            [
                NormalizedRecord(
                    external_id="ext-1",
                    name="Real Discovery Co",
                    attributes={"domain": "real-discovery-co.invalid", "industry": "Healthcare", "country": "United States"},
                )
            ],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert by_field["industry"]["value"] == "Healthcare"
    assert by_field["industry"]["source_provider_id"] == "fixed-company-provider"
    assert by_field["country"]["value"] == "United States"
    assert by_field["country"]["source_provider_id"] == "fixed-company-provider"


def test_import_company_evidence_reads_revenue_range_from_discovery(client):
    """Phase 7G: yearly_revenue_range is a documented Explorium response
    field mapped by the adapter into attributes['revenue_range'] —
    collect_company_evidence must surface it as evidence (field
    'revenue_range'), the same way industry/country already are, instead
    of discarding data the provider already returned at no extra cost."""
    icp = _create_icp(client, "ICP Evidence A3")
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "fixed-company-provider",
            [
                NormalizedRecord(
                    external_id="ext-2",
                    name="Revenue Discovery Co",
                    attributes={"domain": "revenue-discovery-co.invalid", "revenue_range": "1M-10M"},
                )
            ],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert by_field["revenue_range"]["value"] == "1M-10M"
    assert by_field["revenue_range"]["source_provider_id"] == "fixed-company-provider"


def test_import_company_evidence_reads_employee_range_from_discovery(client):
    """Phase 7N: number_of_employees_range is a documented Explorium
    response field mapped by the adapter into attributes['employee_range']
    (a string range, e.g. "11-50") — collect_company_evidence must surface
    it as evidence field 'employee_range', distinct from the precise
    'employee_count' int field, following the exact same pattern already
    used for revenue_range/industry/country. Previously silently discarded
    even when a provider actually supplied it."""
    icp = _create_icp(client, "ICP Evidence A4")
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "fixed-company-provider",
            [
                NormalizedRecord(
                    external_id="ext-3",
                    name="Employee Range Discovery Co",
                    attributes={"domain": "employee-range-discovery-co.invalid", "employee_range": "11-50"},
                )
            ],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert by_field["employee_range"]["value"] == "11-50"
    assert by_field["employee_range"]["source_provider_id"] == "fixed-company-provider"
    # Never coerced into or merged with employee_count — no provider in
    # this test supplied a count, so none must be fabricated.
    assert "employee_count" not in by_field


def test_import_company_evidence_omits_employee_range_when_absent(client):
    """No employee_range attribute on the discovery candidate — no
    employee_range evidence must be created (never fabricated)."""
    icp = _create_icp(client, "ICP Evidence A5")
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "fixed-company-provider",
            [
                NormalizedRecord(
                    external_id="ext-4",
                    name="No Employee Range Co",
                    attributes={"domain": "no-employee-range-co.invalid"},
                )
            ],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert "employee_range" not in by_field


def test_import_company_evidence_reads_linkedin_id_from_discovery(client):
    """Phase 7N: linkedin_profile is a documented Explorium response field
    mapped by the adapter into attributes['linkedin_id'] — collect_company_
    evidence must surface it as evidence field 'linkedin_id' for companies,
    mirroring the person-level extraction already done in
    collect_person_evidence. Previously silently discarded for companies
    even when a provider actually supplied it."""
    icp = _create_icp(client, "ICP Evidence A6")
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "fixed-company-provider",
            [
                NormalizedRecord(
                    external_id="ext-5",
                    name="LinkedIn Discovery Co",
                    attributes={
                        "domain": "linkedin-discovery-co.invalid",
                        "linkedin_id": "https://www.linkedin.com/company/linkedin-discovery-co",
                    },
                )
            ],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert by_field["linkedin_id"]["value"] == "https://www.linkedin.com/company/linkedin-discovery-co"
    assert by_field["linkedin_id"]["source_provider_id"] == "fixed-company-provider"


def test_import_company_evidence_omits_linkedin_id_when_absent(client):
    """No linkedin_id attribute on the discovery candidate — no linkedin_id
    evidence must be created (never fabricated)."""
    icp = _create_icp(client, "ICP Evidence A7")
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "fixed-company-provider",
            [
                NormalizedRecord(
                    external_id="ext-6",
                    name="No LinkedIn Co",
                    attributes={"domain": "no-linkedin-co.invalid"},
                )
            ],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    company_id = resolution[0]["canonical_company_id"]

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert "linkedin_id" not in by_field


def test_import_unknown_company_returns_404(client):
    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": "does-not-exist"})
    assert response.status_code == 404


def test_import_is_idempotent(client):
    icp = _create_icp(client, "ICP Evidence B")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider()])

    first = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id}).json()
    second = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id}).json()

    assert len(first["added"]) > 0
    assert second["added"] == []
    assert second["skipped_duplicates"] == len(first["added"])


# --- person evidence import ---------------------------------------------


def test_import_person_evidence_from_discovery(client):
    icp = _create_icp(client, "ICP Evidence C")
    company_id = _resolve_a_company(client, icp["id"])
    person_id = _resolve_a_person(client, icp["id"], company_id)

    response = client.post("/api/v1/evidence/import", json={"entity_type": "PERSON", "entity_id": person_id})
    assert response.status_code == 200
    fields = {r["field"] for r in response.json()["added"]}
    assert fields == {"person_identity", "current_title", "company_association"}


def test_import_unknown_person_returns_404(client):
    response = client.post("/api/v1/evidence/import", json={"entity_type": "PERSON", "entity_id": "does-not-exist"})
    assert response.status_code == 404


# --- retrieval ----------------------------------------------------------


def test_get_entity_evidence(client):
    icp = _create_icp(client, "ICP Evidence D")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    response = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    assert len(response.json()) > 0


def test_get_field_evidence_filters_correctly(client):
    icp = _create_icp(client, "ICP Evidence E")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    response = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id, "field": "industry"})
    body = response.json()
    assert len(body) == 1
    assert body[0]["field"] == "industry"


def test_missing_evidence_for_an_entity_with_no_activity_is_empty(client):
    icp = _create_icp(client, "ICP Evidence F")
    company_id = _resolve_a_company(client, icp["id"])
    # no enrichment, no import
    response = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.json() == []


# --- conflicting values via the API ----------------------------------


def test_conflicting_values_visible_in_summary(client):
    icp = _create_icp(client, "ICP Evidence G")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider(), MockCompanyRegistryProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    summary = client.get("/api/v1/evidence/summary", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    employee_field = next(f for f in summary["fields"] if f["field"] == "employee_range")
    assert employee_field["status"] == "CONFLICT"
    assert len(employee_field["records"]) == 2


def test_us_vs_united_states_no_longer_falsely_conflicts_via_the_api(client):
    """The exact scenario found live during Phase 8: two mocks reporting
    "US" and "United States" for country must now be recognized as
    agreeing, thanks to Phase 2's geography normalization being reused
    for comparison."""
    icp = _create_icp(client, "ICP Evidence H")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider(), MockCompanyRegistryProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    summary = client.get("/api/v1/evidence/summary", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    country_field = next(f for f in summary["fields"] if f["field"] == "country")
    assert country_field["status"] == "SUPPORTED"
    raw_values = {r["value"] for r in country_field["records"]}
    assert raw_values == {"US", "United States"}  # both preserved verbatim


# --- corroborating values ------------------------------------------------


def test_identical_values_from_two_providers_are_supported(client):
    icp = _create_icp(client, "ICP Evidence I")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider(), MockCompanyRegistryProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    summary = client.get("/api/v1/evidence/summary", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    industry_field = next(f for f in summary["fields"] if f["field"] == "industry")
    assert industry_field["status"] == "SUPPORTED"
    assert len(industry_field["records"]) == 2


# --- completeness ------------------------------------------------------


def test_evidence_completeness_reported_in_summary(client):
    icp = _create_icp(client, "ICP Evidence J")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    summary = client.get("/api/v1/evidence/summary", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    assert 0 < summary["completeness"] <= 1.0


# --- manual evidence recording ------------------------------------------


def test_manually_add_evidence_record(client):
    icp = _create_icp(client, "ICP Evidence K")
    company_id = _resolve_a_company(client, icp["id"])

    payload = {
        "entity_type": "COMPANY",
        "entity_id": company_id,
        "field": "industry",
        "value": "Beauty",
        "source_type": "search",
        "retrieved_at": "2026-01-01T00:00:00Z",
    }
    response = client.post("/api/v1/evidence", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["confidence"] == "UNKNOWN"  # never fabricated when not supplied
    assert body["source_type"] == "search"


def test_manual_evidence_on_unknown_entity_returns_404(client):
    payload = {
        "entity_type": "COMPANY",
        "entity_id": "does-not-exist",
        "field": "industry",
        "value": "Beauty",
        "source_type": "search",
        "retrieved_at": "2026-01-01T00:00:00Z",
    }
    response = client.post("/api/v1/evidence", json=payload)
    assert response.status_code == 404


# --- multi-ICP reuse -----------------------------------------------------


def test_evidence_reusable_across_multiple_icps(client):
    icp_a = _create_icp(client, "ICP Evidence L1")
    icp_b = _create_icp(client, "ICP Evidence L2")
    company_id = _resolve_a_company(client, icp_a["id"], domain="shared-evidence-co.invalid")
    _resolve_a_company(client, icp_b["id"], domain="shared-evidence-co.invalid")  # same company, different ICP

    _enrich_with(client, company_id, [MockCompanyDataProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})

    # evidence is keyed by company_id, not by either ICP
    evidence = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    assert len(evidence) > 0


# --- provider failure / partial data --------------------------------------


class _BrokenEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str = "broken-enrichment"):
        super().__init__(provider_id=provider_id, provider_name="Broken", capabilities={ProviderCapability.COMPANY_ENRICHMENT})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("vendor outage")


def test_partial_enrichment_failure_still_yields_importable_evidence(client):
    icp = _create_icp(client, "ICP Evidence M")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider(), _BrokenEnrichmentProvider()])

    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    fields = {r["field"] for r in response.json()["added"]}
    assert "industry" in fields  # from the healthy provider despite the other failing


# --- immutability / history -----------------------------------------------


def test_evidence_records_are_never_overwritten_by_a_second_import(client):
    icp = _create_icp(client, "ICP Evidence N")
    company_id = _resolve_a_company(client, icp["id"])
    _enrich_with(client, company_id, [MockCompanyDataProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    first_snapshot = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    # a second enrichment run adds NEW facts, which import as NEW evidence
    _enrich_with(client, company_id, [MockCompanyDataProvider()])
    client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    second_snapshot = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()

    first_ids = {r["id"] for r in first_snapshot}
    second_ids = {r["id"] for r in second_snapshot}
    assert first_ids <= second_ids  # nothing from the first snapshot disappeared or changed id
    assert len(second_ids) > len(first_ids)  # new observations were added, not merged in place
