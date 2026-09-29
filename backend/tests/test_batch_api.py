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
from app.providers.mocks import (
    MockCompanyDataProvider,
    MockCompanyRegistryProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)
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


def _default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())
    registry.register(MockPeopleDataProvider())
    registry.register(MockWebSearchProvider())
    return registry


def _with_registry(client, registry, fn):
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        return fn()
    finally:
        del app.dependency_overrides[get_provider_registry]


def _create_batch(client, icp_id, target_count=2, registry=None, **overrides):
    payload = {"icp_id": icp_id, "target_count": target_count}
    payload.update(overrides)
    registry = registry or _default_registry()
    return _with_registry(client, registry, lambda: client.post("/api/v1/batches", json=payload))


# --- small batch / multiple leads -------------------------------------


def test_small_batch_runs_end_to_end(client):
    icp = _create_icp(client, "Batch API A")
    response = _create_batch(client, icp["id"], target_count=2)
    body = response.json()
    assert response.status_code == 201
    assert body["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert body["discovered_count"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["company_id"] is not None
        assert item["outcome"] in {"ACCEPTED", "HELD", "REJECTED", "DUPLICATE", "FAILED"}
        assert item["stage"] == "DONE" or item["outcome"] == "FAILED"


def test_multiple_leads_each_get_their_own_item(client):
    icp = _create_icp(client, "Batch API B")
    body = _create_batch(client, icp["id"], target_count=2).json()
    company_ids = {item["company_id"] for item in body["items"]}
    assert len(company_ids) == 2  # the default mock provider returns 2 distinct companies


# --- Phase 4 (AI/UX + live-safety audit): used_mock_company_data flag ------


def test_batch_run_entirely_on_mocks_is_flagged(client):
    """Root cause: a batch run on MockCompanyDataProvider (e.g.
    EXPLORIUM_API_KEY unset) returned a response structurally identical
    to a real one — no signal anywhere that the "leads" are fabricated.
    used_mock_company_data makes that visible without redesigning
    discovery/batch orchestration itself."""
    icp = _create_icp(client, "Batch API Mock Flag A")
    body = _create_batch(client, icp["id"], target_count=2).json()  # _default_registry() = all mocks
    assert body["used_mock_company_data"] is True


def test_batch_run_on_a_real_looking_provider_is_not_flagged(client):
    icp = _create_icp(client, "Batch API Mock Flag B")
    provider = _PagedDiscoveryProvider(pages=[[("ext-1", "Company One"), ("ext-2", "Company Two")]])
    registry = ProviderRegistry()
    registry.register(provider)
    body = _create_batch(client, icp["id"], target_count=2, discovery_limit=5, registry=registry).json()
    assert body["used_mock_company_data"] is False


def test_get_batch_also_reports_the_mock_flag(client):
    icp = _create_icp(client, "Batch API Mock Flag C")
    created = _create_batch(client, icp["id"], target_count=2).json()
    fetched = client.get(f"/api/v1/batches/{created['id']}").json()
    assert fetched["used_mock_company_data"] is True


# --- Phase 5: provider_call_outcomes observability -------------------------


def test_provider_call_outcomes_surfaces_the_real_discovery_call(client):
    """Root cause: the per-provider call record (provider_id, requested
    vs. returned, latency_ms) already existed on DiscoveryRunModel, but
    nothing traced it back to the batch that triggered it — a caller
    could not answer "how many provider calls did this batch make" at
    all without already knowing internal discovery_run ids."""
    icp = _create_icp(client, "Batch API ProviderOutcomes A")
    body = _create_batch(client, icp["id"], target_count=2).json()

    assert len(body["provider_call_outcomes"]) >= 1
    outcome = body["provider_call_outcomes"][0]
    assert outcome["provider_id"] == "mock-company-data-v1"
    assert outcome["success"] is True
    assert outcome["returned"] == 2


def test_provider_call_outcomes_grows_across_resume_rounds(client):
    icp = _create_icp(client, "Batch API ProviderOutcomes B")
    provider = _PagedDiscoveryProvider(pages=[
        [("ext-1", "Company One")],
        [("ext-2", "Company Two")],
    ])
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp["id"], target_count=100, discovery_limit=1, registry=registry).json()
    assert len(first["provider_call_outcomes"]) == 1

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()
    assert len(resumed["provider_call_outcomes"]) == 2


# --- under-delivery: requested more than discovery/resolution can produce ---


def test_under_delivery_is_valid_never_force_filled(client):
    icp = _create_icp(client, "Batch API C")
    # the default mock company provider only ever returns 2 records
    body = _create_batch(client, icp["id"], target_count=50).json()
    assert body["discovered_count"] == 2
    assert len(body["items"]) == 2
    assert body["requested_target_count"] == 50  # honestly reported as requested, never silently changed


def test_zero_candidates_from_an_empty_provider_never_fabricates_a_lead(client):
    icp = _create_icp(client, "Batch API D")

    class _EmptyDiscoveryProvider(ProviderAdapter):
        def __init__(self):
            super().__init__(provider_id="empty-disc", provider_name="empty-disc", capabilities={ProviderCapability.COMPANY_DISCOVERY})

        def execute(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                provider_id=self.provider_id, capability=request.capability, success=True, data=(),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
            )

    registry = ProviderRegistry()
    registry.register(_EmptyDiscoveryProvider())
    body = _create_batch(client, icp["id"], target_count=5, registry=registry).json()
    assert body["discovered_count"] == 0
    assert body["items"] == []
    assert body["status"] == "COMPLETED"


# --- partial provider failure -------------------------------------------


def test_partial_provider_failure_does_not_fail_the_whole_batch(client):
    icp = _create_icp(client, "Batch API E")

    class _FailingEnrichmentProvider(ProviderAdapter):
        def __init__(self):
            super().__init__(provider_id="fail-enrich", provider_name="fail-enrich", capabilities={ProviderCapability.COMPANY_ENRICHMENT})

        def execute(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                provider_id=self.provider_id, capability=request.capability, success=False,
                error=ProviderError(code="SIMULATED", message="simulated enrichment failure", retryable=True),
            )

    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())  # discovery still works
    registry.register(_FailingEnrichmentProvider())  # the only enrichment provider always fails
    registry.register(MockPeopleDataProvider())
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()
    # enrichment failing gracefully (Phase 8's own partial-failure handling)
    # must not crash the batch — items should still reach a terminal state
    assert body["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["outcome"] is not None


# --- one failed individual item does not fail the batch ---------------


def test_one_failed_item_does_not_abort_the_batch(client, monkeypatch):
    icp = _create_icp(client, "Batch API F")

    import app.services.batch_orchestration as orch

    original = orch.classify_company_business_model
    call_count = {"n": 0}

    def _flaky(company_id, db):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure for the first item only")
        return original(company_id, db=db)

    monkeypatch.setattr(orch, "classify_company_business_model", _flaky)

    body = _create_batch(client, icp["id"], target_count=2).json()
    assert len(body["items"]) == 2
    outcomes = [item["outcome"] for item in body["items"]]
    assert "FAILED" in outcomes
    assert any(o != "FAILED" for o in outcomes)  # the other item still completed
    assert body["status"] == "COMPLETED_WITH_ERRORS"
    assert body["failed_count"] == 1


# --- duplicate candidates --------------------------------------------


def test_duplicate_candidate_across_two_batches_is_marked_duplicate(client):
    icp = _create_icp(client, "Batch API G")
    registry = _default_registry()

    first = _create_batch(client, icp["id"], target_count=1, registry=registry).json()
    first_lead_id = first["items"][0]["lead_id"]
    assert first["items"][0]["outcome"] != "DUPLICATE"

    second = _create_batch(client, icp["id"], target_count=1, registry=registry).json()
    # the default mock provider returns the same first company deterministically,
    # so the second batch's first item resolves to the SAME canonical company/lead
    assert second["items"][0]["lead_id"] == first_lead_id
    assert second["items"][0]["outcome"] == "DUPLICATE"


# --- retry / idempotency: resuming never creates duplicate canonical rows --


def test_resume_is_idempotent_and_creates_no_duplicate_canonical_rows(client):
    icp = _create_icp(client, "Batch API H")
    registry = _default_registry()
    first = _create_batch(client, icp["id"], target_count=2, registry=registry).json()
    batch_id = first["id"]

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{batch_id}/resume")
    ).json()

    assert resumed["id"] == batch_id
    assert resumed["discovered_count"] == first["discovered_count"]
    assert len(resumed["items"]) == len(first["items"])
    assert {i["id"] for i in resumed["items"]} == {i["id"] for i in first["items"]}
    for a, b in zip(sorted(first["items"], key=lambda i: i["id"]), sorted(resumed["items"], key=lambda i: i["id"])):
        assert a["lead_id"] == b["lead_id"]
        assert a["company_id"] == b["company_id"]
        assert a["outcome"] == b["outcome"]

    # verify no duplicate canonical leads exist for the same (company, person) pair
    company_ids = [i["company_id"] for i in resumed["items"]]
    assert len(company_ids) == len(set(company_ids))


def test_resuming_an_unknown_batch_returns_404(client):
    response = client.post("/api/v1/batches/does-not-exist/resume")
    assert response.status_code == 404


# --- multi-ICP batches --------------------------------------------------


def test_multi_icp_batches_are_independent(client):
    icp_a = _create_icp(client, "Batch API I-A")
    icp_b = _create_icp(client, "Batch API I-B")

    batch_a = _create_batch(client, icp_a["id"], target_count=1).json()
    batch_b = _create_batch(client, icp_b["id"], target_count=1).json()

    assert batch_a["id"] != batch_b["id"]
    assert batch_a["icp_id"] != batch_b["icp_id"]
    # the underlying company is the SAME real company (Phase 7 reuses it),
    # but each batch tracks its own item independently
    assert batch_a["items"][0]["company_id"] == batch_b["items"][0]["company_id"]
    assert batch_a["items"][0]["icp_id"] == icp_a["id"]
    assert batch_b["items"][0]["icp_id"] == icp_b["id"]


# --- hard-fail protection -----------------------------------------------


def test_hard_fail_never_becomes_accepted_in_batch_outcome(client):
    icp = _create_icp(client, "Batch API J", min_employees=999999, max_employees=9999999)
    body = _create_batch(client, icp["id"], target_count=2).json()
    for item in body["items"]:
        if item["hard_rule_result"] == "FAIL":
            assert item["outcome"] == "REJECTED"
            assert item["outcome"] != "ACCEPTED"


# --- partial progress / history preserved -------------------------------


def test_batch_items_are_individually_inspectable(client):
    icp = _create_icp(client, "Batch API K")
    body = _create_batch(client, icp["id"], target_count=2).json()
    batch_id = body["id"]

    items = client.get(f"/api/v1/batches/{batch_id}/items").json()
    assert len(items) == 2
    for item in items:
        assert item["batch_id"] == batch_id
        assert "stage" in item
        assert "created_at" in item


def test_get_batch_returns_full_detail(client):
    icp = _create_icp(client, "Batch API L")
    created = _create_batch(client, icp["id"], target_count=1).json()
    fetched = client.get(f"/api/v1/batches/{created['id']}").json()
    assert fetched == created


def test_list_batches_includes_created_batch(client):
    icp = _create_icp(client, "Batch API M")
    created = _create_batch(client, icp["id"], target_count=1).json()
    batches = client.get("/api/v1/batches").json()
    assert any(b["id"] == created["id"] for b in batches)


def test_get_unknown_batch_returns_404(client):
    response = client.get("/api/v1/batches/does-not-exist")
    assert response.status_code == 404


def test_create_batch_unknown_icp_returns_404(client):
    registry = _default_registry()
    response = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": "does-not-exist", "target_count": 1})
    )
    assert response.status_code == 404


# --- provenance: every lead traces back to batch + ICP/version + source ---


def test_every_item_retains_full_provenance(client):
    icp = _create_icp(client, "Batch API N")
    body = _create_batch(client, icp["id"], target_count=1).json()
    item = body["items"][0]
    assert item["batch_id"] == body["id"]
    assert item["icp_id"] == icp["id"]
    assert item["icp_version"] == 1
    assert item["source_candidate_id"] is not None

    dedup_history = client.get("/api/v1/lead-deduplications", params={"company_id": item["company_id"]}).json()
    assert any(d["company_candidate_id"] == item["source_candidate_id"] for d in dedup_history)


# --- deterministic metrics -----------------------------------------------


def test_batch_metrics_sum_to_item_count(client):
    icp = _create_icp(client, "Batch API O")
    body = _create_batch(client, icp["id"], target_count=2).json()
    total = body["accepted_count"] + body["held_count"] + body["rejected_count"] + body["duplicate_count"] + body["failed_count"]
    assert total == len(body["items"])


# --- no duplicate canonical leads / no ICP or evidence mutation -------------


def test_no_duplicate_canonical_leads_created_across_repeated_batches(client):
    icp = _create_icp(client, "Batch API P")
    registry = _default_registry()
    _create_batch(client, icp["id"], target_count=2, registry=registry)
    _create_batch(client, icp["id"], target_count=2, registry=registry)

    all_companies = client.get("/api/v1/companies").json()
    all_leads = []
    for company in all_companies:
        history = client.get("/api/v1/lead-deduplications", params={"company_id": company["id"]}).json()
        for h in history:
            if h["lead_id"]:
                all_leads.append(h["lead_id"])
    # every (company, person) pair maps to exactly one canonical lead id,
    # even though it may have been deduplicated multiple times across batches
    company_person_to_leads: dict[tuple, set] = {}
    for company in all_companies:
        history = client.get("/api/v1/lead-deduplications", params={"company_id": company["id"]}).json()
        for h in history:
            if h["lead_id"] is None:
                continue
            key = (h["company_id"], h["person_id"])
            company_person_to_leads.setdefault(key, set()).add(h["lead_id"])
    for key, lead_ids in company_person_to_leads.items():
        assert len(lead_ids) == 1, f"multiple canonical leads found for {key}: {lead_ids}"


def test_batch_never_mutates_icp(client):
    icp = _create_icp(client, "Batch API Q")
    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    _create_batch(client, icp["id"], target_count=2)
    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    assert icp_before == icp_after


def test_batch_never_deletes_or_overwrites_evidence(client):
    icp = _create_icp(client, "Batch API R")
    body = _create_batch(client, icp["id"], target_count=1).json()
    company_id = body["items"][0]["company_id"]

    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    assert len(evidence_before) > 0

    # resuming must not delete or reduce evidence, only ever add to it
    _with_registry(client, _default_registry(), lambda: client.post(f"/api/v1/batches/{body['id']}/resume"))
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    before_ids = {e["id"] for e in evidence_before}
    after_ids = {e["id"] for e in evidence_after}
    assert before_ids <= after_ids


# --- progressive discovery / "Find More Leads" ----------------------------
# A fake, cursor-aware, non-mock COMPANY_DISCOVERY provider so these tests
# exercise real pagination bookkeeping (batch.discovery_cursors /
# discovery_exhausted_providers / discovery_pool_exhausted) rather than
# relying on the deterministic-but-static MockCompanyDataProvider, which
# always returns the exact same 2 companies and therefore can never
# demonstrate a genuinely new page of results.


class _PagedDiscoveryProvider(ProviderAdapter):
    """Each call returns the next page of `pages` (a list of lists of
    (external_id, name) tuples), keyed by an opaque page-index cursor.
    Reports exhausted=True once the last page has been served."""

    def __init__(self, pages, provider_id="paged-disc"):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._pages = pages
        self.call_count = 0

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        page_index = int(request.cursor) if request.cursor else 0
        page = self._pages[page_index] if page_index < len(self._pages) else []
        next_index = page_index + 1
        is_last = next_index >= len(self._pages)
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(NormalizedRecord(external_id=ext_id, name=name, attributes={"domain": f"{ext_id}.invalid"}) for ext_id, name in page),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
            cursor=None if is_last else str(next_index),
            exhausted=is_last,
        )


class _AlwaysFailingDiscoveryProvider(ProviderAdapter):
    def __init__(self, error_code="SIMULATED_NON_RETRYABLE", provider_id="failing-disc"):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._error_code = error_code

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=False,
            error=ProviderError(code=self._error_code, message="simulated non-retryable discovery failure", retryable=False),
        )


def test_resume_with_no_body_behaves_exactly_as_today(client):
    icp = _create_icp(client, "Batch API S")
    registry = _default_registry()
    first = _create_batch(client, icp["id"], target_count=2, registry=registry).json()

    response = _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume"))
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == len(first["items"])
    assert {i["id"] for i in body["items"]} == {i["id"] for i in first["items"]}


def test_find_more_appends_new_items_without_removing_existing_ones(client):
    icp = _create_icp(client, "Batch API T")
    provider = _PagedDiscoveryProvider(pages=[
        [("ext-1", "Company One"), ("ext-2", "Company Two")],
        [("ext-3", "Company Three"), ("ext-4", "Company Four")],
    ])
    registry = ProviderRegistry()
    registry.register(provider)

    # target_count deliberately higher than what round 1 alone could ever
    # satisfy, so _should_run_another_discovery_round actually requests a
    # second round rather than stopping because round 1 already met it.
    first = _create_batch(client, icp["id"], target_count=100, discovery_limit=2, registry=registry).json()
    assert len(first["items"]) == 2
    first_ids = {i["id"] for i in first["items"]}

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()

    assert len(resumed["items"]) == 4
    resumed_ids = {i["id"] for i in resumed["items"]}
    assert first_ids <= resumed_ids  # every original item is still present, never replaced
    assert resumed["discovered_count"] == 4


def test_find_more_never_creates_duplicate_batch_items_for_the_same_external_id(client):
    icp = _create_icp(client, "Batch API U")
    # Page 2 overlaps page 1 (ext-1 repeats) — simulates a provider/cursor
    # quirk; the pre-filter must still catch it even though Phase 19 dedup
    # would eventually catch it too, avoiding wasted pipeline work.
    provider = _PagedDiscoveryProvider(pages=[
        [("ext-1", "Company One")],
        [("ext-1", "Company One"), ("ext-2", "Company Two")],
    ])
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp["id"], target_count=100, discovery_limit=5, registry=registry).json()
    assert len(first["items"]) == 1

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()

    # only ext-2 is genuinely new
    assert len(resumed["items"]) == 2


class _TwoBranchSameCompanyProvider(ProviderAdapter):
    """P2 fix regression: page 1 returns a company found via a real
    structured Healthcare taxonomy match; page 2 returns the SAME real
    company (same external_id — Explorium's own business_id is stable
    across branches) found via a DIFFERENT branch, the SaaS keyword
    fallback. Before the P2 fix, page 2's sighting — and the ONLY
    evidence that could ever prove the "SaaS" half of a compound
    Healthcare+SaaS ICP — was silently discarded by the pre-existing
    (provider_id, external_id) dedup, before any evidence for it was ever
    created. provider_id is deliberately
    "explorium-company-discovery-v1" — the one name
    app/services/evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS
    allowlist trusts for SUPPORTED_STRUCTURED, exactly like production
    Explorium data."""

    def __init__(self):
        super().__init__(
            provider_id="explorium-company-discovery-v1",
            provider_name="explorium-company-discovery-v1",
            capabilities={ProviderCapability.COMPANY_DISCOVERY},
        )

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        page_index = int(request.cursor) if request.cursor else 0
        if page_index == 0:
            attributes = {
                "domain": "acme-health-saas.invalid",
                "industry": "General Medical and Surgical Hospitals",
                "industry_match_branch": "linkedin_category",
                "industry_match_terms": ["Healthcare"],
                "industry_match_resolved_category_count": 1,
                "country": "United States",
                "employee_range": "51-200",
            }
        else:
            attributes = {
                "domain": "acme-health-saas.invalid",
                "industry": "Software Publishers",
                "keyword_match_terms": ["SaaS"],
                "keyword_match_term_sources": ["industry"],
            }
        is_last = page_index >= 1
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(NormalizedRecord(external_id="same-real-company", name="Acme Health SaaS Inc", attributes=attributes),),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
            cursor=None if is_last else "1",
            exhausted=is_last,
        )


def test_compound_icp_company_rediscovered_via_a_different_branch_reopens_but_still_holds(client):
    """P2 fix, end-to-end: round 1 alone (Healthcare only) must HOLD —
    "SaaS" is unproven. Round 2 rediscovers the SAME real company via the
    SaaS branch; before the P2 fix that sighting (and its evidence) was
    silently dropped and the item stayed HELD forever. After the P2 fix,
    the new branch's evidence is persisted and the HELD item is reopened
    and re-validated within the SAME resume call — proving the item is no
    longer stuck. It still resolves to HELD, though, not ACCEPTED: the
    "Healthcare" half only ever resolved via an unverifiable structured
    taxonomy match (linkedin_category "General Medical and Surgical
    Hospitals" for requested term "Healthcare" — no provider data proves
    that relationship), so per the later structured-bridge retirement
    that half correctly never counts as proof. Only the "SaaS" half
    (a real keyword match) is trustworthy, which is not enough on its
    own to satisfy a two-term compound ICP."""
    icp = client.post(
        "/api/v1/icps",
        json={
            "name": "Batch API Cross-Round",
            "hard_rules": {
                "industry": ["Healthcare", "SaaS"], "geography": [], "min_employees": 1, "max_employees": 10000,
                "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
            },
            "soft_preferences": {
                "business_model_preferences": [], "commercial_signals": [],
                "growth_signals": [], "marketing_signals": [], "other_preferences": [],
            },
        },
    ).json()

    provider = _TwoBranchSameCompanyProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp["id"], target_count=1, discovery_limit=5, registry=registry).json()
    assert len(first["items"]) == 1
    assert first["items"][0]["outcome"] == "HELD"  # Healthcare alone can never prove the compound ICP

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()

    # No new BatchItemModel was created for the repeat external_id — the
    # original item was REOPENED and re-processed in place, never
    # duplicated.
    assert len(resumed["items"]) == 1
    assert resumed["items"][0]["id"] == first["items"][0]["id"]
    assert resumed["items"][0]["outcome"] == "HELD"
    assert resumed["items"][0]["hard_rule_result"] == "HOLD"
    assert resumed["accepted_count"] == 0
    assert resumed["held_count"] == 1


class _PagedDiscoveryProviderWithHardRuleEvidence(ProviderAdapter):
    """Like _PagedDiscoveryProvider, but each record also carries
    country/employee_range attributes so a wide-open ICP's hard rules can
    actually resolve to ACCEPTED instead of HOLDing on missing evidence —
    needed for test_find_more_leads_stops_processing_once_target_count_is_reached_mid_round,
    which must observe real ACCEPTED outcomes to prove the processing-time
    bound works. Two things must both be true for that:
      1. provider_id is deliberately the same string
         app/services/evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS
         names ("explorium-company-discovery-v1") — a single sighting from
         any other provider id never reaches SUPPORTED for a structured
         field (see that module's own trust-gate docstring).
      2. "employee_range" (a bucket string, e.g. "51-200") is used, not
         "employee_count" — _TRUSTED_STRUCTURED_FIELDS names "industry",
         "country", "employee_range" only; a single employee_count
         sighting never reaches SUPPORTED regardless of provider trust,
         which would otherwise still leave every item HOLDing."""

    def __init__(self, pages, provider_id="explorium-company-discovery-v1"):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._pages = pages

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        page_index = int(request.cursor) if request.cursor else 0
        page = self._pages[page_index] if page_index < len(self._pages) else []
        next_index = page_index + 1
        is_last = next_index >= len(self._pages)
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(
                NormalizedRecord(
                    external_id=ext_id, name=name,
                    attributes={"domain": f"{ext_id}.invalid", "country": "United States", "employee_range": "51-200"},
                )
                for ext_id, name in page
            ),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
            cursor=None if is_last else str(next_index),
            exhausted=is_last,
        )


def test_find_more_leads_stops_processing_once_target_count_is_reached_mid_round(client):
    # Regression test: a "Find More Leads" round is never truncated to
    # target_count at DISCOVERY time (see _seed_items_from_discovery's own
    # docstring — only the very first seed round is truncated that way),
    # so without a processing-time bound, a round that discovers more raw
    # candidates than the batch still needs would run every single one of
    # them through the full pipeline (evidence import, hard validation,
    # LLM qualification, enrichment) — real, unnecessary per-company
    # provider spend for leads nobody asked for. This test proves that
    # once enough items are ACCEPTED to satisfy requested_target_count
    # mid-round, remaining discovered items in that same round are left
    # untouched (still at BatchItemStage.DISCOVERED, no outcome) rather
    # than all being processed.
    icp = _create_icp(client, "Batch API Overshoot")  # wide-open hard rules: everything discovered here PASSes
    provider = _PagedDiscoveryProviderWithHardRuleEvidence(pages=[
        [("ext-1", "Company One")],
        [
            ("ext-2", "Company Two"), ("ext-3", "Company Three"), ("ext-4", "Company Four"),
            ("ext-5", "Company Five"), ("ext-6", "Company Six"),
        ],
    ])
    registry = ProviderRegistry()
    registry.register(provider)

    # Round 1: discovery_limit=1 so it discovers/accepts exactly 1 of the
    # 3 requested leads, leaving a genuine gap of 2 for round 2 to close.
    first = _create_batch(client, icp["id"], target_count=3, discovery_limit=1, registry=registry).json()
    assert len(first["items"]) == 1
    assert first["items"][0]["outcome"] == "ACCEPTED"

    # Round 2's page returns 5 new candidates — far more than the 2 still
    # needed to reach target_count=3.
    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()

    assert resumed["discovered_count"] == 6  # every discovered candidate is still an honest, real record
    # Company resolution (a cheap, local, no-credit-spend DB merge) still
    # runs for every discovered item unconditionally — that alone advances
    # stage past DISCOVERED and is not what this test is bounding. The
    # real signal for "went through the expensive pipeline" (evidence
    # import, hard validation, LLM qualification, enrichment) is whether
    # an outcome was ever assigned at all: _process_one_company always
    # sets one (ACCEPTED/HELD/REJECTED/DUPLICATE/FAILED) for an item it
    # actually advances, and an item this test's early-stop left behind
    # keeps outcome=None.
    processed_items = [i for i in resumed["items"] if i["outcome"] is not None]
    accepted_items = [i for i in resumed["items"] if i["outcome"] == "ACCEPTED"]
    untouched_items = [i for i in resumed["items"] if i["outcome"] is None]
    # exactly enough were processed/accepted to reach target_count=3 —
    # never all 6 discovered candidates.
    assert len(accepted_items) == 3
    assert len(processed_items) == 3
    assert len(untouched_items) == 3
    for item in untouched_items:
        assert item["stage"] == "COMPANY_RESOLVED"  # advanced only as far as the unconditional resolution step, never further
    assert resumed["accepted_count"] == 3


def test_resume_reports_discovery_pool_exhausted_when_provider_reports_exhausted(client):
    icp = _create_icp(client, "Batch API V")
    provider = _PagedDiscoveryProvider(pages=[[("ext-1", "Only Company")]])
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp["id"], target_count=10, discovery_limit=5, registry=registry).json()
    assert first["discovery_pool_exhausted"] is True  # the single page IS the whole pool

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()
    assert resumed["discovery_pool_exhausted"] is True
    assert len(resumed["items"]) == 1  # no new candidates were ever available
    assert provider.call_count == 1  # an exhausted provider is never called again


def test_resume_reports_discovery_error_code_on_non_retryable_provider_failure(client):
    icp = _create_icp(client, "Batch API W")
    registry = ProviderRegistry()
    registry.register(_AlwaysFailingDiscoveryProvider(error_code="EXPLORIUM_CREDITS_EXHAUSTED"))

    first = _create_batch(client, icp["id"], target_count=5, registry=registry).json()
    assert first["items"] == []
    assert first["discovery_error_code"] == "EXPLORIUM_CREDITS_EXHAUSTED"
    # Phase 7I: the real message must be preserved alongside the code —
    # previously only the code was ever persisted, so a completed-but-empty
    # batch had no way to reveal WHY beyond a bare code string.
    assert first["discovery_error_message"] == "simulated non-retryable discovery failure"

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 3})
    ).json()
    # a real, non-retryable error must stop further automatic rounds, and
    # the batch's existing (empty, in this case) results are preserved —
    # never wiped — rather than the endpoint erroring out.
    assert resumed["discovery_error_code"] == "EXPLORIUM_CREDITS_EXHAUSTED"
    assert resumed["discovery_error_message"] == "simulated non-retryable discovery failure"
    assert resumed["items"] == []


def test_discovery_error_message_reflects_the_actual_provider_exception_text(client):
    """The Phase 7H live test's real gap: a PROVIDER_ERROR from a genuine
    exception (e.g. a timeout inside ProviderAdapter.run()'s except clause,
    see app/providers/base.py) carries a real, specific message via
    ProviderError.message — this must survive all the way into the
    persisted batch, not just a generic code, so a completed-but-empty
    batch is actually diagnosable afterward."""
    icp = _create_icp(client, "Batch API W2")
    registry = ProviderRegistry()
    registry.register(
        _AlwaysFailingDiscoveryProvider(
            error_code="PROVIDER_ERROR", provider_id="failing-disc-2"
        )
    )

    first = _create_batch(client, icp["id"], target_count=5, registry=registry).json()
    assert first["discovery_error_code"] == "PROVIDER_ERROR"
    assert first["discovery_error_message"] == "simulated non-retryable discovery failure"


def test_discovery_error_message_cleared_once_a_round_succeeds(client):
    """discovery_error_message must follow the exact same clear-at-start-
    of-round lifecycle as discovery_error_code (see
    _run_one_discovery_round's `batch.discovery_error_code = None` /
    `batch.discovery_error_message = None` at the top) — a stale message
    from a since-resolved failure must never be shown alongside a later
    success."""
    icp = _create_icp(client, "Batch API W3")
    provider = _PagedDiscoveryProvider(pages=[[("ext-1", "Recovered Co")]])
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp["id"], target_count=5, registry=registry).json()
    assert first["discovery_error_code"] is None
    assert first["discovery_error_message"] is None
    assert len(first["items"]) == 1


def test_provider_credits_error_preserves_already_qualified_results(client):
    icp = _create_icp(client, "Batch API X")
    # A multi-page provider so round 1 does NOT already exhaust the pool —
    # otherwise resume would correctly never call the provider again
    # (discovery_pool_exhausted short-circuits _should_run_another_discovery_round),
    # which would make this test unable to reach the failing-provider path.
    provider = _PagedDiscoveryProvider(pages=[[("ext-1", "First Page Co")], [("ext-2", "Second Page Co")]])
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp["id"], target_count=100, discovery_limit=5, registry=registry).json()
    assert len(first["items"]) == 1
    assert first["discovery_pool_exhausted"] is False
    first_item_id = first["items"][0]["id"]
    first_company_id = first["items"][0]["company_id"]

    # swap in a failing provider under the SAME provider_id so it inherits
    # this batch's existing (non-exhausted) discovery_cursors/state
    failing_registry = ProviderRegistry()
    failing_registry.register(_AlwaysFailingDiscoveryProvider(error_code="EXPLORIUM_CREDITS_EXHAUSTED", provider_id="paged-disc"))

    resumed = _with_registry(
        client, failing_registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 1})
    ).json()

    assert resumed["discovery_error_code"] == "EXPLORIUM_CREDITS_EXHAUSTED"
    # the already-qualified item from before the credits ran out is fully preserved
    assert any(i["id"] == first_item_id and i["company_id"] == first_company_id for i in resumed["items"])


def test_target_count_mode_stops_at_genuine_qualified_count_not_padded(client):
    icp = _create_icp(client, "Batch API Y", min_employees=1, max_employees=10000)
    # 5 total candidates across 2 pages; discovery_limit small enough that
    # a single round can't exhaust the pool in one call.
    provider = _PagedDiscoveryProvider(pages=[
        [("ext-1", "Co A"), ("ext-2", "Co B"), ("ext-3", "Co C")],
        [("ext-4", "Co D"), ("ext-5", "Co E")],
    ])
    registry = ProviderRegistry()
    registry.register(provider)

    # Ask for far more qualified leads than the fake provider's whole pool
    # (5 candidates) could ever produce.
    first = _create_batch(client, icp["id"], target_count=100, discovery_limit=3, registry=registry).json()

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 5})
    ).json()

    # the pool only ever had 5 real candidates — the batch must report that
    # honestly (pool exhausted, accepted_count far below the requested 100)
    # rather than looping forever or fabricating additional candidates.
    assert resumed["discovery_pool_exhausted"] is True
    assert resumed["requested_target_count"] == 100
    assert resumed["accepted_count"] < 100
    assert len(resumed["items"]) <= 5


# --- Phase 25: hard round-count ceiling (the live-test-derived fix) ------
# Root cause reproduced here: a provider pool large enough to never
# exhaust naturally, candidates with no evidence beyond name/domain so
# they can never reach ACCEPTED (missing employee_range/industry/country
# entirely -> HOLD, never PASS) — exactly what made the real Phase 24 live
# test keep requesting rounds (target_count=1, 0 ever ACCEPTED) until a
# real Explorium credit balance was fully exhausted at round 6.


def test_target_count_one_zero_accepted_stops_at_configured_round_cap(client, monkeypatch):
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=3))

    # 10 pages of 2 candidates each — far more than enough rounds' worth of
    # "pool" that a naive loop could keep pulling from; none carry any
    # evidence beyond name/domain, so none can ever reach ACCEPTED.
    provider = _PagedDiscoveryProvider(pages=[[(f"ext-{p}-{i}", f"Co {p}-{i}") for i in range(2)] for p in range(10)])
    registry = ProviderRegistry()
    registry.register(provider)

    first = _create_batch(client, icp_id=_create_icp(client, "Batch API Z1")["id"], target_count=1, discovery_limit=2, registry=registry).json()

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 10})
    ).json()

    # The cap (3) must stop it, not the never-exhausting 10-page pool and
    # not a satisfied target_count (0 ever ACCEPTED, by design).
    assert resumed["discovery_pool_exhausted"] is False
    assert resumed["accepted_count"] == 0
    assert resumed["discovery_error_code"] == "DISCOVERY_ROUND_LIMIT_REACHED"
    assert resumed["discovery_error_message"]
    # provider.call_count reflects exactly 3 rounds' worth of real search
    # calls (never 10) — the actual cost-safety guarantee this phase adds.
    assert provider.call_count == 3


def test_round_limit_reached_is_a_clean_terminal_state_not_retried_on_further_resume(client, monkeypatch):
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=2))

    provider = _PagedDiscoveryProvider(pages=[[(f"ext-{p}-{i}", f"Co {p}-{i}") for i in range(2)] for p in range(10)])
    registry = ProviderRegistry()
    registry.register(provider)

    icp = _create_icp(client, "Batch API Z2")
    first = _create_batch(client, icp["id"], target_count=1, discovery_limit=2, registry=registry).json()
    resumed_once = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 5})
    ).json()
    assert resumed_once["discovery_error_code"] == "DISCOVERY_ROUND_LIMIT_REACHED"
    calls_after_first_resume = provider.call_count

    # A second, independent resume call (simulating a status-refresh/second
    # click) must never trigger another discovery round once the cap's
    # terminal error is set — this is what "resume/status polling does not
    # duplicate discovery" and "credit exhaustion remains a clean terminal
    # state" mean concretely: no new provider calls, ever, past this point.
    resumed_again = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 5})
    ).json()
    assert resumed_again["discovery_error_code"] == "DISCOVERY_ROUND_LIMIT_REACHED"
    assert provider.call_count == calls_after_first_resume  # zero additional provider calls


def test_bodyless_resume_after_round_limit_never_triggers_a_new_discovery_call(client, monkeypatch):
    """The bodyless GET-batch-status-style resume (default max_discovery_rounds=1,
    the shape any status-refresh/polling call would use) must be a pure
    no-op once the round cap has already tripped — never a paid discovery
    call, regardless of how many times a client calls it."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=1))

    provider = _PagedDiscoveryProvider(pages=[[(f"ext-{p}-{i}", f"Co {p}-{i}") for i in range(2)] for p in range(10)])
    registry = ProviderRegistry()
    registry.register(provider)

    icp = _create_icp(client, "Batch API Z3")
    # create_batch's own seed round counts as round 1 (rounds_used=1 right
    # after it) but never itself calls _should_run_another_discovery_round
    # again since its own max_new_discovery_rounds defaults to 1 — so the
    # cap (also 1) is trip-READY but not yet TRIPPED until the next resume
    # attempts a round.
    first = _create_batch(client, icp["id"], target_count=1, discovery_limit=2, registry=registry).json()
    assert first["discovery_error_code"] is None
    calls_after_create = provider.call_count
    assert calls_after_create == 1

    # The first bodyless resume (max_discovery_rounds defaults to 1 — the
    # exact shape a status-refresh/polling call would use) is what
    # actually trips the cap, since discovery_rounds_run (1) already
    # equals the configured max (1).
    first_resume = _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume")).json()
    assert first_resume["discovery_error_code"] == "DISCOVERY_ROUND_LIMIT_REACHED"
    assert provider.call_count == calls_after_create  # tripped WITHOUT making a new provider call

    # Every subsequent "polling" resume must remain a pure no-op — zero
    # additional provider calls, ever, once the cap's terminal error is set.
    for _ in range(3):
        status_check = _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume")).json()
        assert status_check["discovery_error_code"] == "DISCOVERY_ROUND_LIMIT_REACHED"

    assert provider.call_count == calls_after_create  # four "polling" resumes total, zero new provider calls


# --- Phase 31: safe lead-batch UX — the real frontend's own request shapes ---
# frontend/src/lib/companies/useBatchDiscovery.ts now sends EXACTLY these
# request shapes (no more automatic multi-round continuation loop client-
# side — see that file's own module docstring): an initial createBatch()
# with target_count defaulting to DEFAULT_TARGET_COUNT=10, and each
# "Add More Leads" click as ONE resumeBatch(id, ADD_MORE_LEADS_ROUND_BUDGET=3)
# call. These tests exercise those EXACT shapes against the real batch
# pipeline to prove the backend's own round cap is what actually protects
# the account, never trusting the frontend to behave.


def test_default_ten_company_target_never_exceeds_the_round_cap(client, monkeypatch):
    """The real frontend's own default (DEFAULT_TARGET_COUNT=10, see
    useBatchDiscovery.ts) with a candidate pool that never runs dry and
    where NOTHING ever reaches ACCEPTED (evidence-less paged candidates,
    same trick as the other round-cap tests) must still stop at the
    backend's configured round ceiling — never loop indefinitely trying
    to reach 10."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=5))

    provider = _PagedDiscoveryProvider(pages=[[(f"ext-{p}-{i}", f"Co {p}-{i}") for i in range(2)] for p in range(20)])
    registry = ProviderRegistry()
    registry.register(provider)

    icp = _create_icp(client, "Batch API Z4")
    first = _create_batch(client, icp["id"], target_count=10, discovery_limit=2, registry=registry).json()

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 10})
    ).json()

    assert resumed["requested_target_count"] == 10
    assert resumed["accepted_count"] == 0
    assert resumed["discovery_error_code"] == "DISCOVERY_ROUND_LIMIT_REACHED"


# --- Phase 5: live-test-mode safety clamp (app/api/batch.py::_clamp_for_live_test) ---
# settings.live_test_mode is forced off for this whole test suite (see
# tests/conftest.py's own module docstring) so every OTHER test above can
# exercise the full, real [1,100]/[1,1000]/[1,10] schema ranges unclamped —
# these tests turn it back on locally, the same monkeypatch pattern the
# round-cap tests above already use for max_discovery_rounds_per_batch.


def test_live_test_mode_clamps_discovery_limit_and_target_count_on_create(client, monkeypatch):
    import app.api.batch as batch_module
    from app.core.config import Settings

    monkeypatch.setattr(
        batch_module, "get_settings",
        lambda: Settings(live_test_mode=True, live_test_max_discovery_limit=5, live_test_max_target_count=5),
    )

    icp = _create_icp(client, "Batch API LiveTest A")
    body = _create_batch(client, icp["id"], target_count=50, discovery_limit=100).json()

    assert body["requested_target_count"] == 5
    assert body["discovery_limit"] == 5


def test_live_test_mode_never_raises_a_caller_request_below_the_clamp(client, monkeypatch):
    """A caller asking for LESS than the live-test cap keeps exactly what
    they asked for — the clamp only ever lowers, never raises, a request."""
    import app.api.batch as batch_module
    from app.core.config import Settings

    monkeypatch.setattr(
        batch_module, "get_settings",
        lambda: Settings(live_test_mode=True, live_test_max_discovery_limit=5, live_test_max_target_count=5),
    )

    icp = _create_icp(client, "Batch API LiveTest B")
    body = _create_batch(client, icp["id"], target_count=2, discovery_limit=2).json()

    assert body["requested_target_count"] == 2
    assert body["discovery_limit"] == 2


def test_live_test_mode_off_leaves_requests_unclamped(client, monkeypatch):
    """The default posture for THIS test suite (conftest.py forces
    live_test_mode off) — asserted explicitly here so the clamp's absence
    is a tested contract, not just an assumption every other test relies on."""
    import app.api.batch as batch_module
    from app.core.config import Settings

    monkeypatch.setattr(batch_module, "get_settings", lambda: Settings(live_test_mode=False))

    icp = _create_icp(client, "Batch API LiveTest C")
    body = _create_batch(client, icp["id"], target_count=50, discovery_limit=100).json()

    assert body["requested_target_count"] == 50
    assert body["discovery_limit"] == 100


def test_live_test_mode_clamps_resume_rounds_to_one_per_call(client, monkeypatch):
    import app.api.batch as batch_module
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    live_settings = Settings(
        max_discovery_rounds_per_batch=5, live_test_mode=True, live_test_max_discovery_limit=5, live_test_max_target_count=5,
    )
    monkeypatch.setattr(batch_module, "get_settings", lambda: live_settings)
    monkeypatch.setattr(orchestration_module, "get_settings", lambda: live_settings)

    # A pool that never runs dry and where nothing ever reaches ACCEPTED —
    # same trick as the round-cap tests above — so discovery_rounds_run
    # directly reflects how many rounds THIS call actually seeded.
    provider = _PagedDiscoveryProvider(pages=[[(f"ext-{p}-{i}", f"Co {p}-{i}") for i in range(2)] for p in range(10)])
    registry = ProviderRegistry()
    registry.register(provider)

    icp = _create_icp(client, "Batch API LiveTest D")
    first = _create_batch(client, icp["id"], target_count=5, discovery_limit=2, registry=registry).json()
    assert first["discovery_rounds_run"] == 1  # the initial seed always counts as one round

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 10})
    ).json()

    # A caller asked for 10 additional rounds in ONE call; live-test mode
    # allows at most 1 more per call, regardless of what was requested.
    assert resumed["discovery_rounds_run"] == 2


def test_repeated_add_more_leads_clicks_never_exceed_the_lifetime_round_cap(client, monkeypatch):
    """Simulates a user clicking "Add More Leads" several times in a row —
    each click is the real frontend's own ADD_MORE_LEADS_ROUND_BUDGET=3
    resume call. The backend's round cap is a LIFETIME total across the
    whole batch (initial + every resume combined), so repeated clicking
    must never be able to accumulate past it, no matter how many separate
    clicks are made."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=5))

    provider = _PagedDiscoveryProvider(pages=[[(f"ext-{p}-{i}", f"Co {p}-{i}") for i in range(2)] for p in range(30)])
    registry = ProviderRegistry()
    registry.register(provider)

    icp = _create_icp(client, "Batch API Z5")
    first = _create_batch(client, icp["id"], target_count=10, discovery_limit=2, registry=registry).json()

    ADD_MORE_LEADS_ROUND_BUDGET = 3  # mirrors frontend/src/lib/companies/useBatchDiscovery.ts's own constant
    seen_error_codes = []
    for _ in range(5):  # five separate "Add More Leads" clicks — far more than the round budget could ever satisfy
        clicked = _with_registry(
            client, registry,
            lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": ADD_MORE_LEADS_ROUND_BUDGET}),
        ).json()
        seen_error_codes.append(clicked["discovery_error_code"])

    # The round cap (5, LIFETIME) must trip well before 5 clicks x 3 rounds
    # each (which would be 15) could ever be seeded — confirmed by the
    # error code appearing and by the provider's own call count never
    # exceeding the cap.
    assert "DISCOVERY_ROUND_LIMIT_REACHED" in seen_error_codes
    assert seen_error_codes[-1] == "DISCOVERY_ROUND_LIMIT_REACHED"  # still tripped on the final click
    assert provider.call_count == 5  # exactly the lifetime cap, regardless of 5 separate click-shaped calls


def test_ten_company_target_with_pool_exhausted_before_target_reports_honestly(client):
    """The candidate pool genuinely running out (not the round cap) before
    reaching the default 10-company target must be reported as
    discovery_pool_exhausted, distinct from the round-limit safety stop —
    the frontend's FindMoreLeadsButton renders these two cases with
    different, honest messaging (see that component's own phase handling)."""
    provider = _PagedDiscoveryProvider(pages=[[("ext-1", "Only Co")]])
    registry = ProviderRegistry()
    registry.register(provider)

    icp = _create_icp(client, "Batch API Z6")
    first = _create_batch(client, icp["id"], target_count=10, discovery_limit=5, registry=registry).json()

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume", json={"max_discovery_rounds": 3})
    ).json()

    assert resumed["discovery_pool_exhausted"] is True
    assert resumed["discovery_error_code"] is None  # exhaustion is NOT the same as the round-limit error code
    assert resumed["accepted_count"] < 10
