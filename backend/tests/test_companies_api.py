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
from datetime import datetime, timezone


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
    """A configurable stand-in for the mock so tests control exactly which
    (name, domain, external_id) triples a discovery run produces."""

    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(self._records),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc), is_mock=True,
            ),
        )


def _run_discovery_with(client, icp_id: str, records: list[NormalizedRecord], provider_id: str = "fixed-provider") -> dict:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider(provider_id, records))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        response = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id})
    finally:
        del app.dependency_overrides[get_provider_registry]
    assert response.status_code == 201
    return response.json()


def _record(external_id: str, name: str, domain: str | None = None) -> NormalizedRecord:
    attrs = {"domain": domain} if domain else {}
    return NormalizedRecord(external_id=external_id, name=name, attributes=attrs)


# --- basic resolve flow -----------------------------------------------


def test_resolve_creates_new_companies_for_fresh_candidates(client):
    icp = _create_icp(client, "D2C Skincare A")
    run = _run_discovery_with(client, icp["id"], [_record("ext-1", "Example Test Co", "example-test.invalid")])

    response = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "NEW"
    assert body[0]["canonical_company_id"]

    company = client.get(f"/api/v1/companies/{body[0]['canonical_company_id']}").json()
    assert company["canonical_domain"] == "example-test.invalid"


def test_resolve_unknown_discovery_run_returns_404(client):
    response = client.post("/api/v1/companies/resolve", json={"discovery_run_id": "does-not-exist"})
    assert response.status_code == 404


# --- repeated discovery resolves to existing company -----------------------


def test_repeated_discovery_resolves_to_the_same_company(client):
    icp = _create_icp(client, "D2C Skincare B")

    run1 = _run_discovery_with(client, icp["id"], [_record("ext-1", "Example Test Co", "example-test.invalid")], provider_id="provider-x")
    result1 = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run1["id"]}).json()
    company_id = result1[0]["canonical_company_id"]

    # A second, later discovery run finds the same company via the same provider+external id.
    run2 = _run_discovery_with(client, icp["id"], [_record("ext-1", "Example Test Co", "example-test.invalid")], provider_id="provider-x")
    result2 = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]}).json()

    assert result2[0]["status"] == "MATCH"
    assert result2[0]["canonical_company_id"] == company_id

    companies = client.get("/api/v1/companies").json()
    assert len(companies) == 1  # not duplicated


# --- multi-ICP requirement --------------------------------------------


def test_same_company_across_different_icps_resolves_to_one_entity(client):
    icp_a = _create_icp(client, "ICP A")
    icp_b = _create_icp(client, "ICP B")

    run_a = _run_discovery_with(client, icp_a["id"], [_record("ext-1", "Shared Co", "shared-co.invalid")])
    run_b = _run_discovery_with(client, icp_b["id"], [_record("ext-2", "Shared Co", "shared-co.invalid")])

    result_a = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_a["id"]}).json()
    result_b = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_b["id"]}).json()

    assert result_a[0]["status"] == "NEW"
    assert result_b[0]["status"] == "MATCH"
    assert result_a[0]["canonical_company_id"] == result_b[0]["canonical_company_id"]

    companies = client.get("/api/v1/companies").json()
    assert len(companies) == 1


# --- provider identities / aliases preserved ------------------------------


def test_provider_identities_from_different_providers_are_both_preserved(client):
    icp = _create_icp(client, "ICP Providers")
    run_1 = _run_discovery_with(client, icp["id"], [_record("ext-1", "Multi Source Co", "multi-source.invalid")], provider_id="provider-x")
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_1["id"]})

    run_2 = _run_discovery_with(client, icp["id"], [_record("ext-9", "Multi Source Co", "multi-source.invalid")], provider_id="provider-y")
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_2["id"]})

    companies = client.get("/api/v1/companies").json()
    assert len(companies) == 1
    provider_identities = companies[0]["provider_identities"]
    assert provider_identities == {"provider-x": "ext-1", "provider-y": "ext-9"}


def test_one_providers_id_never_overwrites_anothers(client):
    icp = _create_icp(client, "ICP No Overwrite")
    run_1 = _run_discovery_with(client, icp["id"], [_record("ext-1", "Stable Co", "stable-co.invalid")], provider_id="provider-x")
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_1["id"]})

    # A different provider matches by domain; must be added, not replace provider-x's id.
    run_2 = _run_discovery_with(client, icp["id"], [_record("different-ext-id", "Stable Co", "stable-co.invalid")], provider_id="provider-z")
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_2["id"]})

    companies = client.get("/api/v1/companies").json()
    assert companies[0]["provider_identities"]["provider-x"] == "ext-1"
    assert companies[0]["provider_identities"]["provider-z"] == "different-ext-id"


def test_alternate_name_is_preserved_as_an_alias(client):
    icp = _create_icp(client, "ICP Alias")
    run_1 = _run_discovery_with(client, icp["id"], [_record("ext-1", "Original Name Co", "same-domain.invalid")])
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_1["id"]})

    run_2 = _run_discovery_with(client, icp["id"], [_record("ext-2", "Renamed Co", "same-domain.invalid")], provider_id="provider-2")
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_2["id"]})

    companies = client.get("/api/v1/companies").json()
    assert companies[0]["canonical_name"] == "Original Name Co"
    assert "Renamed Co" in companies[0]["aliases"]


# --- discovery history intact ---------------------------------------------


def test_discovery_history_is_unchanged_after_resolution(client):
    icp = _create_icp(client, "ICP History")
    run = _run_discovery_with(client, icp["id"], [_record("ext-1", "Example Test Co", "example-test.invalid")])
    before = client.get(f"/api/v1/discovery/runs/{run['id']}").json()

    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]})

    after = client.get(f"/api/v1/discovery/runs/{run['id']}").json()
    assert after == before


# --- idempotency -------------------------------------------------------


def test_resolving_the_same_run_twice_does_not_duplicate_companies_or_resolutions(client):
    icp = _create_icp(client, "ICP Idempotent")
    run = _run_discovery_with(client, icp["id"], [_record("ext-1", "Example Test Co", "example-test.invalid")])

    first = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    second = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()

    assert first == second
    assert len(client.get("/api/v1/companies").json()) == 1
    resolutions = client.get("/api/v1/companies/resolutions", params={"discovery_run_id": run["id"]}).json()
    assert len(resolutions) == 1


# --- no accidental cross-company merge ------------------------------------


def test_distinct_companies_in_one_run_stay_distinct(client):
    icp = _create_icp(client, "ICP Distinct")
    run = _run_discovery_with(
        client,
        icp["id"],
        [
            _record("ext-1", "Alpha Co", "alpha.invalid"),
            _record("ext-2", "Beta Co", "beta.invalid"),
            _record("ext-3", "Gamma Co", "gamma.invalid"),
        ],
    )
    results = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()

    assert all(r["status"] == "NEW" for r in results)
    ids = {r["canonical_company_id"] for r in results}
    assert len(ids) == 3

    companies = client.get("/api/v1/companies").json()
    assert len(companies) == 3


# --- UNRESOLVED persists without a canonical company -----------------------


def test_unresolved_candidate_is_persisted_without_a_canonical_company(client):
    icp = _create_icp(client, "ICP Unresolved")
    run_1 = _run_discovery_with(client, icp["id"], [_record("ext-1", "Ambiguous Co", None)])
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_1["id"]})

    # Same name, no domain, different provider entirely -> not enough to merge.
    run_2 = _run_discovery_with(client, icp["id"], [_record("ext-2", "Ambiguous Co", None)], provider_id="another-provider")
    result_2 = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run_2["id"]}).json()

    assert result_2[0]["status"] == "UNRESOLVED"
    assert result_2[0]["canonical_company_id"] is None
    assert result_2[0]["matched_company_id"] is not None  # hint preserved for audit


# --- no enrichment/qualification fields -----------------------------------


def test_canonical_company_has_no_enrichment_or_qualification_fields(client):
    icp = _create_icp(client, "ICP Shape Check")
    run = _run_discovery_with(client, icp["id"], [_record("ext-1", "Example Test Co", "example-test.invalid")])
    client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]})

    company = client.get("/api/v1/companies").json()[0]
    assert set(company.keys()) == {
        "id",
        "canonical_name",
        "canonical_domain",
        "aliases",
        "provider_identities",
        "created_at",
        "updated_at",
    }
