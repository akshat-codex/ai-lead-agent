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


def _run_people_discovery_with(client, icp_id: str, company_id: str, records: list[NormalizedRecord], provider_id: str = "fixed-people-provider") -> dict:
    registry = ProviderRegistry()
    registry.register(_FixedPeopleProvider(provider_id, records))
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/people-discovery/runs", json={"icp_id": icp_id, "company_id": company_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    return run


def _person_record(external_id: str, name: str, title: str | None = None, linkedin_id: str | None = None) -> NormalizedRecord:
    attrs = {}
    if title:
        attrs["title"] = title
    if linkedin_id:
        attrs["linkedin_id"] = linkedin_id
    return NormalizedRecord(external_id=external_id, name=name, attributes=attrs)


# --- basic resolve flow -----------------------------------------------


def test_resolve_creates_new_person(client):
    icp = _create_icp(client, "ICP Person A")
    company_id = _resolve_a_company(client, icp["id"])
    run = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")])

    response = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "NEW"
    assert body[0]["canonical_person_id"]

    person = client.get(f"/api/v1/people/{body[0]['canonical_person_id']}").json()
    assert person["canonical_name"] == "Jane Testperson"
    assert person["canonical_company_id"] == company_id


def test_resolve_unknown_run_returns_404(client):
    response = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": "does-not-exist"})
    assert response.status_code == 404


def test_get_unknown_person_returns_404(client):
    response = client.get("/api/v1/people/does-not-exist")
    assert response.status_code == 404


# --- repeated discovery / multi-ICP reuse -------------------------------


def test_repeated_discovery_resolves_to_the_same_person(client):
    icp = _create_icp(client, "ICP Person B")
    company_id = _resolve_a_company(client, icp["id"])

    run1 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")], provider_id="provider-x")
    result1 = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run1["id"]}).json()
    person_id = result1[0]["canonical_person_id"]

    run2 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")], provider_id="provider-x")
    result2 = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run2["id"]}).json()

    assert result2[0]["status"] == "MATCH"
    assert result2[0]["canonical_person_id"] == person_id
    assert len(client.get("/api/v1/people").json()) == 1


def test_same_person_across_different_icps_resolves_to_one_entity(client):
    icp_a = _create_icp(client, "ICP Person C1")
    icp_b = _create_icp(client, "ICP Person C2")
    company_id = _resolve_a_company(client, icp_a["id"])
    # both ICPs discover people at the SAME already-resolved company
    _resolve_a_company(client, icp_b["id"])  # ensures icp_b's own company row also exists; irrelevant here

    run_a = _run_people_discovery_with(client, icp_a["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")], provider_id="provider-shared")
    run_b = _run_people_discovery_with(client, icp_b["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")], provider_id="provider-shared")

    result_a = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run_a["id"]}).json()
    result_b = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run_b["id"]}).json()

    assert result_a[0]["status"] == "NEW"
    assert result_b[0]["status"] == "MATCH"
    assert result_a[0]["canonical_person_id"] == result_b[0]["canonical_person_id"]


# --- provider identities / aliases preserved ------------------------------


def test_provider_identities_from_different_providers_preserved_via_linkedin(client):
    icp = _create_icp(client, "ICP Person D")
    company_id = _resolve_a_company(client, icp["id"])

    run1 = _run_people_discovery_with(
        client, icp["id"], company_id,
        [_person_record("ext-1", "Jane Testperson", "CMO", linkedin_id="in/janetestperson")],
        provider_id="provider-x",
    )
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run1["id"]})

    run2 = _run_people_discovery_with(
        client, icp["id"], company_id,
        [_person_record("ext-9", "Jane Testperson", "CMO", linkedin_id="in/janetestperson")],
        provider_id="provider-y",
    )
    result2 = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run2["id"]}).json()
    assert result2[0]["status"] == "MATCH"

    people = client.get("/api/v1/people").json()
    assert len(people) == 1
    assert people[0]["provider_identities"] == {"provider-x": "ext-1", "provider-y": "ext-9"}


def test_one_providers_id_never_overwrites_anothers(client):
    icp = _create_icp(client, "ICP Person E")
    company_id = _resolve_a_company(client, icp["id"])

    run1 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Stable Person", "CMO", linkedin_id="in/stableperson")], provider_id="provider-x")
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run1["id"]})

    run2 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("different-ext-id", "Stable Person", "CMO", linkedin_id="in/stableperson")], provider_id="provider-z")
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run2["id"]})

    people = client.get("/api/v1/people").json()
    assert people[0]["provider_identities"]["provider-x"] == "ext-1"
    assert people[0]["provider_identities"]["provider-z"] == "different-ext-id"


def test_alternate_name_formatting_preserved_as_alias(client):
    """A person resolution is held to a stricter bar than company
    resolution (see person_resolution.py): any *real* name difference on
    top of a LinkedIn/provider match is a conflict, not a silent alias.
    Aliases can only accumulate here when the normalized names truly agree
    but the raw formatting differs (e.g. stray whitespace from a provider's
    own data entry) — a genuine formatting variant, not a different name.
    """
    icp = _create_icp(client, "ICP Person F")
    company_id = _resolve_a_company(client, icp["id"])

    run1 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO", linkedin_id="in/janetestperson")], provider_id="provider-x")
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run1["id"]})

    run2 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-2", "Jane  Testperson", "CMO", linkedin_id="in/janetestperson")], provider_id="provider-y")
    result2 = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run2["id"]}).json()
    assert result2[0]["status"] == "MATCH"

    people = client.get("/api/v1/people").json()
    assert people[0]["canonical_name"] == "Jane Testperson"
    assert "Jane  Testperson" in people[0]["aliases"]


def test_a_genuinely_different_name_on_a_linkedin_match_is_unresolved_not_an_alias(client):
    icp = _create_icp(client, "ICP Person F2")
    company_id = _resolve_a_company(client, icp["id"])

    run1 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO", linkedin_id="in/janetestperson")], provider_id="provider-x")
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run1["id"]})

    run2 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-2", "Someone Else Entirely", "CMO", linkedin_id="in/janetestperson")], provider_id="provider-y")
    result2 = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run2["id"]}).json()

    assert result2[0]["status"] == "UNRESOLVED"
    assert "name" in result2[0]["conflicting_signals"]


# --- discovery history intact / idempotency --------------------------------


def test_discovery_history_is_unchanged_after_resolution(client):
    icp = _create_icp(client, "ICP Person G")
    company_id = _resolve_a_company(client, icp["id"])
    run = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")])
    before = client.get(f"/api/v1/people-discovery/runs/{run['id']}").json()

    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]})

    after = client.get(f"/api/v1/people-discovery/runs/{run['id']}").json()
    assert after == before


def test_resolving_the_same_run_twice_is_idempotent(client):
    icp = _create_icp(client, "ICP Person H")
    company_id = _resolve_a_company(client, icp["id"])
    run = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")])

    first = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]}).json()
    second = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]}).json()

    assert first == second
    assert len(client.get("/api/v1/people").json()) == 1
    resolutions = client.get("/api/v1/people/resolutions", params={"people_discovery_run_id": run["id"]}).json()
    assert len(resolutions) == 1


# --- UNRESOLVED persists without a canonical person ------------------------


def test_unresolved_candidate_is_persisted_without_a_canonical_person(client):
    icp = _create_icp(client, "ICP Person I")
    company_id = _resolve_a_company(client, icp["id"])

    run1 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Ambiguous Person", "CMO")])
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run1["id"]})

    # same name, same company, no shared provider or linkedin id -> insufficient evidence
    run2 = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-2", "Ambiguous Person", "CMO")], provider_id="another-provider")
    result2 = client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run2["id"]}).json()

    assert result2[0]["status"] == "UNRESOLVED"
    assert result2[0]["canonical_person_id"] is None
    assert result2[0]["matched_person_id"] is not None


# --- no qualification/verification fields ----------------------------------


def test_canonical_person_has_no_verification_or_qualification_fields(client):
    icp = _create_icp(client, "ICP Person J")
    company_id = _resolve_a_company(client, icp["id"])
    run = _run_people_discovery_with(client, icp["id"], company_id, [_person_record("ext-1", "Jane Testperson", "CMO")])
    client.post("/api/v1/people/resolve", json={"people_discovery_run_id": run["id"]})

    person = client.get("/api/v1/people").json()[0]
    assert set(person.keys()) == {
        "id",
        "canonical_name",
        "canonical_company_id",
        "associated_company_ids",
        "aliases",
        "linkedin_id",
        "provider_identities",
        "created_at",
        "updated_at",
    }
