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


def _icp_payload(name: str) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": [], "geography": [], "min_employees": 1, "max_employees": 10000,
            "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [],
            "growth_signals": [], "marketing_signals": [], "other_preferences": [],
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
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


class _FixedPeopleProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.PEOPLE_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _discover_company(client, icp_id: str, domain: str, provider_id: str = "p1") -> str:
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider(provider_id, [NormalizedRecord(external_id="ext-co", name="Example Test Co", attributes={"domain": domain})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


def _discover_person(client, icp_id: str, company_id: str, external_id: str = "ext-person", name: str = "Jane Testperson", provider_id: str = "pp1") -> str:
    registry = ProviderRegistry()
    registry.register(
        _FixedPeopleProvider(provider_id, [NormalizedRecord(external_id=external_id, name=name, attributes={"title": "CMO"})])
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_id, "company_id": company_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_person_id"]


def _dedupe(client, icp_id, company_id=None, person_id=None):
    payload = {"icp_id": icp_id}
    if company_id is not None:
        payload["company_id"] = company_id
    if person_id is not None:
        payload["person_id"] = person_id
    return client.post("/api/v1/lead-deduplications", json=payload)


# --- exact same lead across repeated runs / idempotency ---------------------


def test_repeated_dedup_of_the_same_pair_matches_the_same_lead(client):
    icp = _create_icp(client, "Dedup API A")
    company_id = _discover_company(client, icp["id"], "dedup-api-a.invalid")
    person_id = _discover_person(client, icp["id"], company_id)

    first = _dedupe(client, icp["id"], company_id, person_id).json()
    second = _dedupe(client, icp["id"], company_id, person_id).json()

    assert first["decision"] == "NEW_LEAD"
    assert second["decision"] == "MATCHED_EXISTING_LEAD"
    assert first["lead_id"] == second["lead_id"]

    # exactly one canonical lead row exists, never duplicated
    lead = client.get(f"/api/v1/leads/{first['lead_id']}").json()
    assert lead["company_id"] == company_id
    assert lead["person_id"] == person_id


def test_reprocessing_does_not_duplicate_icp_membership(client):
    icp = _create_icp(client, "Dedup API B")
    company_id = _discover_company(client, icp["id"], "dedup-api-b.invalid")
    person_id = _discover_person(client, icp["id"], company_id)

    lead_id = _dedupe(client, icp["id"], company_id, person_id).json()["lead_id"]
    _dedupe(client, icp["id"], company_id, person_id)  # reprocess under the exact same ICP/version

    memberships = client.get(f"/api/v1/leads/{lead_id}/icp-memberships").json()
    assert len(memberships) == 1


# --- same lead across different ICPs ----------------------------------------


def test_same_lead_reused_across_different_icps(client):
    icp_a = _create_icp(client, "Dedup API C-A")
    icp_b = _create_icp(client, "Dedup API C-B")
    company_id = _discover_company(client, icp_a["id"], "dedup-api-c.invalid")
    person_id = _discover_person(client, icp_a["id"], company_id)

    result_a = _dedupe(client, icp_a["id"], company_id, person_id).json()
    result_b = _dedupe(client, icp_b["id"], company_id, person_id).json()

    assert result_a["lead_id"] == result_b["lead_id"]
    assert result_b["decision"] == "MATCHED_EXISTING_LEAD"

    memberships = client.get(f"/api/v1/leads/{result_a['lead_id']}/icp-memberships").json()
    icp_ids = {m["icp_id"] for m in memberships}
    assert icp_ids == {icp_a["id"], icp_b["id"]}


# --- same lead from different providers -------------------------------------


def test_same_lead_from_different_discovery_providers(client):
    icp = _create_icp(client, "Dedup API D")
    # two independent discovery runs/providers resolving to the SAME canonical company via shared domain
    company_id_1 = _discover_company(client, icp["id"], "dedup-api-d.invalid", provider_id="prov-x")
    registry = ProviderRegistry()
    registry.register(_FixedCompanyProvider("prov-y", [NormalizedRecord(external_id="ext-co-2", name="Example Test Co", attributes={"domain": "dedup-api-d.invalid"})]))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run2 = client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution2 = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run2["id"]}).json()
    company_id_2 = resolution2[0]["canonical_company_id"]

    assert company_id_1 == company_id_2  # same domain -> Phase 7 already merged these

    result = _dedupe(client, icp["id"], company_id_1).json()
    assert result["decision"] == "NEW_LEAD"


# --- same company / different person; same person / different company -----


def test_same_company_different_person_is_different_lead(client):
    icp = _create_icp(client, "Dedup API E")
    company_id = _discover_company(client, icp["id"], "dedup-api-e.invalid")
    person_1 = _discover_person(client, icp["id"], company_id, external_id="ext-p1", name="Jane Testperson")
    person_2 = _discover_person(client, icp["id"], company_id, external_id="ext-p2", name="Alex Sampleuser", provider_id="pp2")

    result_1 = _dedupe(client, icp["id"], company_id, person_1).json()
    result_2 = _dedupe(client, icp["id"], company_id, person_2).json()

    assert result_1["decision"] == "NEW_LEAD"
    assert result_2["decision"] == "NEW_LEAD"
    assert result_1["lead_id"] != result_2["lead_id"]


def test_similar_names_at_different_companies_do_not_merge(client):
    icp = _create_icp(client, "Dedup API F")
    company_a = _discover_company(client, icp["id"], "dedup-api-f-a.invalid", provider_id="p-a")
    company_b = _discover_company(client, icp["id"], "dedup-api-f-b.invalid", provider_id="p-b")
    # two DIFFERENT canonical people who happen to share the exact same name string
    person_a = _discover_person(client, icp["id"], company_a, external_id="ext-shared-name-a", name="Jane Testperson", provider_id="pp-a")
    person_b = _discover_person(client, icp["id"], company_b, external_id="ext-shared-name-b", name="Jane Testperson", provider_id="pp-b")

    assert person_a != person_b  # Phase 10 correctly treated them as different people (different company context)

    result_a = _dedupe(client, icp["id"], company_a, person_a).json()
    result_b = _dedupe(client, icp["id"], company_b, person_b).json()

    assert result_a["lead_id"] != result_b["lead_id"]


# --- unresolved identity -----------------------------------------------


def test_dedup_without_company_id_is_unresolved(client):
    icp = _create_icp(client, "Dedup API G")
    company_id = _discover_company(client, icp["id"], "dedup-api-g.invalid")
    person_id = _discover_person(client, icp["id"], company_id)

    response = _dedupe(client, icp["id"], person_id=person_id)
    body = response.json()
    assert body["decision"] == "UNRESOLVED"
    assert body["lead_id"] is None


# --- 404s ----------------------------------------------------------------


def test_dedup_unknown_icp_returns_404(client):
    response = _dedupe(client, "does-not-exist", company_id="whatever")
    assert response.status_code == 404


def test_dedup_unknown_company_returns_404(client):
    icp = _create_icp(client, "Dedup API H")
    response = _dedupe(client, icp["id"], company_id="does-not-exist")
    assert response.status_code == 404


def test_get_unknown_lead_returns_404(client):
    response = client.get("/api/v1/leads/does-not-exist")
    assert response.status_code == 404


def test_get_unknown_deduplication_returns_404(client):
    response = client.get("/api/v1/lead-deduplications/does-not-exist")
    assert response.status_code == 404


def test_list_requires_at_least_one_filter(client):
    response = client.get("/api/v1/lead-deduplications")
    assert response.status_code == 400


# --- history append-only / provenance ---------------------------------------


def test_deduplication_history_is_append_only(client):
    icp = _create_icp(client, "Dedup API I")
    company_id = _discover_company(client, icp["id"], "dedup-api-i.invalid")
    person_id = _discover_person(client, icp["id"], company_id)

    first = _dedupe(client, icp["id"], company_id, person_id).json()
    second = _dedupe(client, icp["id"], company_id, person_id).json()

    assert first["id"] != second["id"]
    history = client.get("/api/v1/lead-deduplications", params={"lead_id": first["lead_id"]}).json()
    assert len(history) == 2


def test_provenance_fields_are_persisted(client):
    icp = _create_icp(client, "Dedup API J")
    company_id = _discover_company(client, icp["id"], "dedup-api-j.invalid")

    response = client.post(
        "/api/v1/lead-deduplications",
        json={
            "icp_id": icp["id"],
            "company_id": company_id,
            "source": {"company_candidate_id": "cand-123", "company_resolution_id": "res-456"},
        },
    )
    body = response.json()
    assert body["company_candidate_id"] == "cand-123"
    assert body["company_resolution_id"] == "res-456"


# --- no ICP / evidence mutation ---------------------------------------------


def test_dedup_never_mutates_icp_or_company_or_person(client):
    icp = _create_icp(client, "Dedup API K")
    company_id = _discover_company(client, icp["id"], "dedup-api-k.invalid")
    person_id = _discover_person(client, icp["id"], company_id)

    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    company_before = client.get(f"/api/v1/companies/{company_id}").json()

    _dedupe(client, icp["id"], company_id, person_id)

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    company_after = client.get(f"/api/v1/companies/{company_id}").json()

    assert icp_before == icp_after
    assert company_before == company_after
