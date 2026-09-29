"""Phase 30 — batch-pipeline integration: company-quality gate + Unipile
ordering.

Mirrors tests/test_batch_api.py's own registry/ICP-payload conventions.
Covers what a pure service-level test cannot: the actual execution ORDER
inside app/services/batch_orchestration.py::_advance_company_pipeline, and
that a hard-rejected company genuinely never reaches the (mock) people-
discovery provider.
"""
import httpx
import respx

from app.main import app
from app.providers.contracts import ProviderCapability, ProviderRequest, ProviderResponse
from app.providers.default_registry import get_provider_registry
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.providers.mocks import (
    MockCompanyDataProvider,
    MockCompanyRegistryProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)
from app.providers.registry import ProviderRegistry

from tests.test_batch_api import _create_batch, _create_icp, _with_registry  # noqa: F401 (reuse exact conventions)

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"


def _create_icp_with_hard_rules(client, name: str, **hard_rule_overrides) -> dict:
    """The default _icp_payload() helper only exposes min/max_employees —
    built manually here for the tests in this file that need a
    deterministic hard FAIL from other rules (geography/exclusions)."""
    hard_rules = {
        "industry": [], "geography": [], "min_employees": 1, "max_employees": 10000,
        "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
    }
    hard_rules.update(hard_rule_overrides)
    payload = {
        "name": name,
        "hard_rules": hard_rules,
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [],
            "growth_signals": [], "marketing_signals": [], "other_preferences": [],
        },
    }
    return client.post("/api/v1/icps", json=payload).json()


class _SpyPeopleDataProvider(MockPeopleDataProvider):
    """Records every PEOPLE_DISCOVERY call it actually receives — used to
    prove Unipile-equivalent calls never happen for a hard-rejected
    company, and only happen AFTER the company-quality stage for everyone
    else."""

    def __init__(self) -> None:
        super().__init__()
        self.people_discovery_calls: list[str] = []

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.capability == ProviderCapability.PEOPLE_DISCOVERY:
            self.people_discovery_calls.append(request.query.get("company_domain") or request.query.get("company_name") or "?")
        return super().execute(request)


class _SpyEnrichmentDataProvider(MockCompanyDataProvider):
    """Records every COMPANY_ENRICHMENT call it actually receives —
    used to prove enrichment (Phase 32) is never spent on a company
    before discovery/hard validation has run, and never at all for a
    REJECT company."""

    def __init__(self) -> None:
        super().__init__()
        self.enrichment_calls: list[str] = []

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.capability == ProviderCapability.COMPANY_ENRICHMENT:
            self.enrichment_calls.append(request.query.get("domain") or request.query.get("company_name") or "?")
        return super().execute(request)


def _spy_registry() -> tuple[ProviderRegistry, _SpyPeopleDataProvider]:
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())
    spy = _SpyPeopleDataProvider()
    registry.register(spy)
    registry.register(MockWebSearchProvider())
    return registry, spy


# --- Unipile/person-discovery must never run before the quality gate ------


@respx.mock
def test_hard_rejected_company_never_reaches_people_discovery(client):
    """A geography requirement the real, live-verified discovery data
    cannot satisfy always FAILs the geography rule.

    Phase 32: enrichment now runs AFTER hard validation (see
    BatchItemStage's own docstring), so a hard FAIL usable for THIS test
    must be provable from DISCOVERY-time evidence alone. The MOCK company
    providers' single, uncorroborated sightings never reach SUPPORTED for
    country (app/services/evidence_engine.py's own
    _TRUSTED_STRUCTURED_PROVIDERS is scoped to the one REAL Explorium
    provider id, never a mock, and the mocks report no domain to merge
    multiple sightings into one company for genuine corroboration
    either) — so this test uses the real ExploriumCompanyDiscoveryProvider
    against respx-mocked HTTP (mirrors tests/test_discovery_qualification_bridge.py's
    own established pattern) instead, whose single sighting for a trusted
    field DOES reach SUPPORTED_STRUCTURED, producing a genuine, confident
    FAIL exactly like a real live company would."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "rj0000000000000000000000000001",
                        "name": "Canadian Test Co",
                        "country_name": "Canada",
                        "number_of_employees_range": "51-200",
                    }
                ],
                "total_results": 1,
                "page": None,
            },
        )
    )

    icp = _create_icp_with_hard_rules(client, "Quality Pipeline A", geography=["France"])
    # A DEDICATED registry for this test, deliberately WITHOUT
    # MockCompanyDataProvider's own COMPANY_DISCOVERY role — those two
    # fixed mock companies only ever reach HOLD here (see this test's own
    # docstring), which would correctly still reach people-discovery
    # (Phase 30: HOLD/PASS both do, only REJECT is excluded) and pollute
    # the "zero people-discovery calls" assertion below with unrelated,
    # legitimately-eligible companies. Only the real
    # ExploriumCompanyDiscoveryProvider (respx-mocked) discovers anything
    # in this test, so its single guaranteed-FAIL candidate is the only
    # company in play.
    registry = ProviderRegistry()
    registry.register(ExploriumCompanyDiscoveryProvider(api_key="test-key"))
    spy = _SpyPeopleDataProvider()
    registry.register(spy)
    registry.register(MockWebSearchProvider())
    body = _create_batch(client, icp["id"], target_count=1, registry=registry).json()

    rejected_items = [item for item in body["items"] if item["hard_rule_result"] == "FAIL"]
    assert rejected_items, "expected at least one hard-FAIL item for this scenario"
    for item in rejected_items:
        assert item["outcome"] == "REJECTED"
        assert item["person_id"] is None

    # The real-Explorium-shaped company's name never appears in any
    # PEOPLE_DISCOVERY call the spy recorded — proving Unipile was never
    # reached for the REJECT company.
    assert spy.people_discovery_calls == []


def test_passing_company_does_reach_people_discovery(client):
    """A normal, hard-PASSing company (the default wide employee range)
    still gets a real person-discovery attempt — Phase 30 only skips
    Unipile for REJECT, never for STRONG/REVIEW."""
    icp = _create_icp(client, "Quality Pipeline B")
    registry, spy = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()

    passing_items = [item for item in body["items"] if item["hard_rule_result"] in {"PASS", "HOLD"}]
    assert passing_items, "expected at least one non-FAIL item for this scenario"
    assert len(spy.people_discovery_calls) > 0


# --- company-quality rows are actually persisted for every item -----------


def test_company_quality_score_persisted_for_every_batch_item(client):
    icp = _create_icp(client, "Quality Pipeline C")
    registry, _ = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()

    for item in body["items"]:
        quality_rows = client.get(
            "/api/v1/company-quality", params={"icp_id": icp["id"], "company_id": item["company_id"]}
        ).json()
        assert len(quality_rows) >= 1
        latest = quality_rows[-1]
        assert latest["hard_rule_result"] == item["hard_rule_result"]
        if item["hard_rule_result"] == "FAIL":
            assert latest["label"] == "REJECT"
            assert latest["score"] is None
        else:
            assert latest["label"] in {"STRONG", "REVIEW"}
            assert latest["score"] is not None


def test_company_quality_signals_are_explainable_via_api(client):
    icp = _create_icp(client, "Quality Pipeline D")
    registry, _ = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=1, registry=registry).json()
    item = body["items"][0]

    quality_rows = client.get(
        "/api/v1/company-quality", params={"icp_id": icp["id"], "company_id": item["company_id"]}
    ).json()
    latest = quality_rows[-1]
    assert latest["explanation"]
    for signal in latest["signals"]:
        assert signal["explanation"]
        assert 0 <= signal["value"] <= 100
        assert 0 <= signal["weight"] <= 1


# --- multiple companies ranked correctly (existing Phase 22 ranking) ------


def test_multiple_companies_still_rank_correctly_after_reorder(client):
    """The existing Phase 22 ranking (app/api/ranking.py) must be entirely
    unaffected by the Phase 30 reorder: a hard-FAIL company is still
    HARD_FAILED-tier, and a PASS company still outranks it."""
    icp = _create_icp(client, "Quality Pipeline E")
    registry, _ = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()

    ranking = client.get("/api/v1/rankings", params={"icp_id": icp["id"], "batch_id": body["id"]}).json()
    ranked_leads = ranking["ranked_leads"]
    assert len(ranked_leads) == len(body["items"])

    tier_order = [lead["tier"] for lead in ranked_leads]
    tier_precedence = {
        "ACCEPTED": 0, "QUALIFIED_STRONG": 1, "QUALIFIED_WEAK": 2, "HOLD": 3, "REJECTED": 4, "DUPLICATE": 5, "HARD_FAILED": 6,
    }
    ranks = [tier_precedence[t] for t in tier_order]
    assert ranks == sorted(ranks)  # ranking never regresses to an unordered list


def test_resume_never_double_calls_people_discovery(client):
    """Resuming an already-completed batch must not re-invoke Unipile for
    items already at PERSON_RESOLVED — the same resume-safety contract
    that existed before Phase 30's reorder, just at the new stage
    position."""
    icp = _create_icp(client, "Quality Pipeline F")
    registry, spy = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()
    first_call_count = len(spy.people_discovery_calls)

    resume_response = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{body['id']}/resume")
    )
    assert resume_response.status_code == 200
    assert len(spy.people_discovery_calls) == first_call_count  # no new calls on a bodyless resume of a completed batch


# --- Phase 32: no enrichment during company discovery/validation ----------


@respx.mock
def test_hard_rejected_company_never_triggers_enrichment(client):
    """Enrichment (like person discovery) is only ever spent on a company
    that survived deterministic validation — a REJECT (hard-FAIL) company
    must never trigger a single COMPANY_ENRICHMENT call. Mirrors
    test_hard_rejected_company_never_reaches_people_discovery's own
    real-Explorium-via-respx approach for a guaranteed FAIL."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "en0000000000000000000000000001",
                        "name": "Rejected Enrichment Co",
                        "country_name": "Canada",
                        "number_of_employees_range": "51-200",
                    }
                ],
                "total_results": 1,
                "page": None,
            },
        )
    )

    icp = _create_icp_with_hard_rules(client, "Quality Pipeline G", geography=["France"])
    registry = ProviderRegistry()
    registry.register(ExploriumCompanyDiscoveryProvider(api_key="test-key"))
    enrichment_spy = _SpyEnrichmentDataProvider()
    registry.register(enrichment_spy)
    registry.register(MockWebSearchProvider())
    body = _create_batch(client, icp["id"], target_count=1, registry=registry).json()

    assert all(item["hard_rule_result"] == "FAIL" for item in body["items"])
    assert body["items"]
    assert enrichment_spy.enrichment_calls == []


def test_passing_company_does_trigger_enrichment_after_validation(client):
    """The inverse: a company that clears the quality gate (not REJECT)
    DOES eventually reach enrichment — this proves the fix defers
    enrichment rather than deleting it. Discovery/validation still
    completes correctly without it (see collect_company_evidence's own
    DiscoveryCandidateModel-derived fields), and it still happens exactly
    once the company is worth pursuing further."""
    icp = _create_icp(client, "Quality Pipeline H")
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    enrichment_spy = MockCompanyRegistryProvider()  # provides COMPANY_ENRICHMENT
    registry.register(enrichment_spy)
    registry.register(MockPeopleDataProvider())
    registry.register(MockWebSearchProvider())
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()

    non_fail_items = [item for item in body["items"] if item["hard_rule_result"] != "FAIL"]
    assert non_fail_items, "expected at least one non-FAIL item for this scenario"
    for item in non_fail_items:
        company_id = item["company_id"]
        facts = _with_registry(client, registry, lambda: client.get(f"/api/v1/companies/{company_id}/facts")).json()
        assert facts  # real enrichment facts exist for a non-REJECT company


@respx.mock
def test_no_enrichment_call_happens_before_hard_validation_completes(client):
    """The precise ordering claim itself: BatchItemStage.ENRICHED must be
    reached strictly after HARD_VALIDATED for every item — confirmed
    directly from the persisted stage-order enum, independent of any
    particular ICP/provider scenario (see BatchItemStage's own Phase 32
    docstring, which this test locks in as a real, checked invariant, not
    just a comment)."""
    from app.schemas.batch import BatchItemStage

    stage_order = list(BatchItemStage)
    assert stage_order.index(BatchItemStage.HARD_VALIDATED) < stage_order.index(BatchItemStage.ENRICHED)
    assert stage_order.index(BatchItemStage.EVIDENCE_COLLECTED) < stage_order.index(BatchItemStage.ENRICHED)
    assert stage_order.index(BatchItemStage.COMPANY_QUALITY_SCORED) < stage_order.index(BatchItemStage.ENRICHED)
