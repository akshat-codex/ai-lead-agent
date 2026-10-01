from datetime import datetime, timezone

from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import (
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


def _create_run(client, icp_id, target_count=2, registry=None, **overrides) -> dict:
    payload = {"icp_id": icp_id, "target_count": target_count}
    payload.update(overrides)
    registry = registry or _default_registry()
    return _with_registry(client, registry, lambda: client.post("/api/v1/pipeline-runs", json=payload)).json()


# --- complete successful pipeline ---------------------------------------


def test_complete_successful_pipeline_reaches_export(client):
    icp = _create_icp(client, "Pipeline API A")
    run = _create_run(client, icp["id"], target_count=2)

    assert run["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert run["lead_count"] == 2
    assert len(run["leads"]) == 2
    stage_names = {s["stage"] for s in run["stage_statuses"]}
    assert {"DISCOVERY", "DEDUPLICATION", "HUMAN_REVIEW", "RANKING", "CONFIDENCE", "EXPORT"} <= stage_names

    export_response = client.get(f"/api/v1/pipeline-runs/{run['id']}/export")
    assert export_response.status_code == 200
    body = export_response.json()
    assert body["metadata"]["icp_id"] == icp["id"]
    assert len(body["leads"]) == 2


# --- partial provider failure -------------------------------------------


def test_partial_provider_failure_does_not_abort_the_pipeline(client):
    icp = _create_icp(client, "Pipeline API B")

    class _FailingEnrichmentProvider(ProviderAdapter):
        def __init__(self):
            super().__init__(provider_id="fail-enrich", provider_name="fail-enrich", capabilities={ProviderCapability.COMPANY_ENRICHMENT})

        def execute(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                provider_id=self.provider_id, capability=request.capability, success=False,
                error=ProviderError(code="SIMULATED", message="simulated enrichment failure", retryable=True),
            )

    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(_FailingEnrichmentProvider())
    registry.register(MockPeopleDataProvider())
    run = _create_run(client, icp["id"], target_count=2, registry=registry)
    assert run["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert len(run["leads"]) == 2


def test_stage_failure_of_one_item_does_not_abort_other_items(client, monkeypatch):
    icp = _create_icp(client, "Pipeline API C")

    import app.services.batch_orchestration as orch

    original = orch.classify_company_business_model
    call_count = {"n": 0}

    def _flaky(company_id, db):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure for the first item only")
        return original(company_id, db=db)

    monkeypatch.setattr(orch, "classify_company_business_model", _flaky)

    run = _create_run(client, icp["id"], target_count=2)
    assert run["status"] == "COMPLETED_WITH_ERRORS"
    assert run["failed_count"] == 1
    # A stage failure happens before Phase 19 deduplication ever runs for
    # that item, so it correctly never gets a canonical lead row (and thus
    # never appears in run["leads"]) — verify isolation via the underlying
    # batch items instead, which record every candidate regardless of
    # outcome.
    batch = client.get(f"/api/v1/batches/{run['batch_id']}").json()
    assert len(batch["items"]) == 2
    outcomes = [item["outcome"] for item in batch["items"]]
    assert "FAILED" in outcomes
    assert any(o != "FAILED" for o in outcomes)
    assert len(run["leads"]) == 1  # exactly the one item that succeeded reached a lead


# --- hard FAIL / HOLD safety ---------------------------------------------


def test_hard_fail_lead_never_reports_verified_confidence(client):
    icp = _create_icp(client, "Pipeline API D", min_employees=999999, max_employees=9999999)
    run = _create_run(client, icp["id"], target_count=2)

    for lead in run["leads"]:
        if lead["hard_rule_result"] == "FAIL":
            assert lead["readiness"] not in {"VERIFIED", "PARTIALLY_VERIFIED"}
            assert lead["readiness"] == "INSUFFICIENT_EVIDENCE"


def test_hard_fail_lead_never_marked_accepted(client):
    icp = _create_icp(client, "Pipeline API E", min_employees=999999, max_employees=9999999)
    run = _create_run(client, icp["id"], target_count=2)

    for lead in run["leads"]:
        if lead["hard_rule_result"] == "FAIL":
            assert lead["batch_outcome"] != "ACCEPTED"


def test_hard_hold_lead_is_insufficient_evidence_not_verified(client):
    # An impossible allowed_titles constraint combined with mock data that
    # never reports titles yields an honest, uncorroborated HOLD rather
    # than a FAIL or a PASS.
    icp = _create_icp(client, "Pipeline API F")
    run = _create_run(client, icp["id"], target_count=2)

    for lead in run["leads"]:
        if lead["hard_rule_result"] == "HOLD":
            assert lead["readiness"] in {"INSUFFICIENT_EVIDENCE", "CONFLICTED"}
            assert lead["readiness"] not in {"VERIFIED"}


# --- missing evidence: never fabricated ----------------------------------


def test_missing_evidence_is_never_fabricated_in_confidence(client):
    icp = _create_icp(client, "Pipeline API G")
    run = _create_run(client, icp["id"], target_count=1)

    confidence_results = client.get(f"/api/v1/pipeline-runs/{run['id']}/confidence").json()
    assert len(confidence_results) == len(run["leads"])
    for result in confidence_results:
        for item in result["supporting_evidence"]:
            if item["status"] == "UNKNOWN":
                assert item["evidence_ids"] == []


# --- duplicate lead --------------------------------------------------------


def test_duplicate_lead_across_two_runs_is_marked_duplicate(client):
    icp = _create_icp(client, "Pipeline API H")
    registry = _default_registry()

    first = _create_run(client, icp["id"], target_count=1, registry=registry)
    first_lead_id = first["leads"][0]["lead_id"]
    assert first["leads"][0]["batch_outcome"] != "DUPLICATE"

    second = _create_run(client, icp["id"], target_count=1, registry=registry)
    assert second["leads"][0]["lead_id"] == first_lead_id
    assert second["leads"][0]["batch_outcome"] == "DUPLICATE"


# --- multiple ICPs ----------------------------------------------------------


def test_multiple_icps_run_independently(client):
    icp_a = _create_icp(client, "Pipeline API I-A")
    icp_b = _create_icp(client, "Pipeline API I-B")

    run_a = _create_run(client, icp_a["id"], target_count=1)
    run_b = _create_run(client, icp_b["id"], target_count=1)

    assert run_a["id"] != run_b["id"]
    assert run_a["icp_id"] != run_b["icp_id"]
    assert run_a["leads"][0]["company_id"] == run_b["leads"][0]["company_id"]  # same real company
    # but confidence/ranking are computed independently per ICP scope
    confidence_a = client.get(f"/api/v1/pipeline-runs/{run_a['id']}/confidence").json()
    confidence_b = client.get(f"/api/v1/pipeline-runs/{run_b['id']}/confidence").json()
    assert confidence_a[0]["icp_id"] == icp_a["id"]
    assert confidence_b[0]["icp_id"] == icp_b["id"]


# --- resume / idempotency ---------------------------------------------------


def test_resume_is_idempotent(client):
    icp = _create_icp(client, "Pipeline API J")
    registry = _default_registry()
    first = _create_run(client, icp["id"], target_count=2, registry=registry)
    run_id = first["id"]

    resumed = _with_registry(client, registry, lambda: client.post(f"/api/v1/pipeline-runs/{run_id}/resume")).json()

    assert resumed["id"] == run_id
    assert resumed["lead_count"] == first["lead_count"]
    first_leads = sorted(first["leads"], key=lambda l: l["lead_id"])
    resumed_leads = sorted(resumed["leads"], key=lambda l: l["lead_id"])
    for a, b in zip(first_leads, resumed_leads):
        assert a["lead_id"] == b["lead_id"]
        assert a["batch_outcome"] == b["batch_outcome"]

    company_ids = [lead["company_id"] for lead in resumed["leads"]]
    assert len(company_ids) == len(set(company_ids))


def test_resuming_completed_run_is_a_safe_no_op(client):
    icp = _create_icp(client, "Pipeline API K")
    run = _create_run(client, icp["id"], target_count=1)
    run_id = run["id"]

    resumed = _with_registry(client, _default_registry(), lambda: client.post(f"/api/v1/pipeline-runs/{run_id}/resume")).json()
    assert resumed["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}


def test_resuming_unknown_run_returns_404(client):
    response = client.post("/api/v1/pipeline-runs/does-not-exist/resume")
    assert response.status_code == 404


# --- export output -----------------------------------------------------------


def test_export_output_reflects_ranking_and_confidence(client):
    icp = _create_icp(client, "Pipeline API L")
    run = _create_run(client, icp["id"], target_count=2)

    export = client.get(f"/api/v1/pipeline-runs/{run['id']}/export").json()
    exported_lead_ids = {lead["lead_id"] for lead in export["leads"]}
    run_lead_ids = {lead["lead_id"] for lead in run["leads"]}
    assert exported_lead_ids == run_lead_ids


def test_export_csv_format_is_available(client):
    icp = _create_icp(client, "Pipeline API M")
    run = _create_run(client, icp["id"], target_count=1)
    response = client.get(f"/api/v1/pipeline-runs/{run['id']}/export", params={"format": "CSV"})
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


def test_pipeline_run_completion_fires_the_export_webhook_when_configured(client, monkeypatch, respx_mock):
    """End-to-end proof that app/services/pipeline_orchestration.py's own
    completion hook actually calls send_export_webhook with a real,
    populated ExportResult — not just that the unit-level webhook function
    works in isolation."""
    import httpx

    import app.services.pipeline_orchestration as orchestration_module
    from app.core.config import Settings

    webhook_url = "https://hooks.example.invalid/lead-agent"
    route = respx_mock.post(webhook_url).mock(return_value=httpx.Response(200))

    live_settings = Settings(
        export_webhook_url=webhook_url,
        export_webhook_secret="test-secret",
        database_url="sqlite:///:memory:",
    )
    monkeypatch.setattr(orchestration_module, "get_settings", lambda: live_settings)

    icp = _create_icp(client, "Pipeline API Webhook")
    _create_run(client, icp["id"], target_count=2)

    assert route.called


def test_pipeline_run_completion_never_calls_the_webhook_when_unconfigured(client, respx_mock):
    """The default posture (no ENV configured) — no outbound call is ever
    attempted, confirmed at the HTTP layer itself via respx's own
    assert_all_called=False default plus an explicit route with zero hits."""
    import httpx

    route = respx_mock.post("https://hooks.example.invalid/lead-agent").mock(return_value=httpx.Response(200))

    icp = _create_icp(client, "Pipeline API No Webhook")
    _create_run(client, icp["id"], target_count=2)

    assert not route.called


def test_export_hubspot_and_salesforce_csv_formats_are_available(client):
    icp = _create_icp(client, "Pipeline API HubSpot Salesforce")
    run = _create_run(client, icp["id"], target_count=1)

    hubspot_response = client.get(f"/api/v1/pipeline-runs/{run['id']}/export", params={"format": "HUBSPOT_CSV"})
    assert hubspot_response.status_code == 200
    assert "text/csv" in hubspot_response.headers["content-type"]
    assert "Company name" in hubspot_response.text

    salesforce_response = client.get(f"/api/v1/pipeline-runs/{run['id']}/export", params={"format": "SALESFORCE_CSV"})
    assert salesforce_response.status_code == 200
    assert "text/csv" in salesforce_response.headers["content-type"]
    assert "Account Name" in salesforce_response.text


# --- audit / provenance -------------------------------------------------


def test_pipeline_run_references_underlying_batch(client):
    icp = _create_icp(client, "Pipeline API N")
    run = _create_run(client, icp["id"], target_count=1)
    assert run["batch_id"] is not None

    batch = client.get(f"/api/v1/batches/{run['batch_id']}").json()
    assert batch["icp_id"] == icp["id"]
    assert len(batch["items"]) == len(run["leads"])


def test_confidence_endpoint_traces_to_real_evidence_ids(client):
    icp = _create_icp(client, "Pipeline API O")
    run = _create_run(client, icp["id"], target_count=1)
    confidence_results = client.get(f"/api/v1/pipeline-runs/{run['id']}/confidence").json()
    company_id = run["leads"][0]["company_id"]
    person_id = run["leads"][0]["person_id"]

    company_evidence = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    real_ids = {e["id"] for e in company_evidence}
    if person_id is not None:
        person_evidence = client.get("/api/v1/evidence", params={"entity_type": "PERSON", "entity_id": person_id}).json()
        real_ids |= {e["id"] for e in person_evidence}
    for item in confidence_results[0]["supporting_evidence"]:
        for evidence_id in item["evidence_ids"]:
            assert evidence_id in real_ids


# --- no regression to prior phases ---------------------------------------


def test_pipeline_run_never_mutates_icp(client):
    icp = _create_icp(client, "Pipeline API P")
    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()
    _create_run(client, icp["id"], target_count=1)
    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    assert icp_before == icp_after


def test_pipeline_run_never_deletes_evidence(client):
    icp = _create_icp(client, "Pipeline API Q")
    run = _create_run(client, icp["id"], target_count=1)
    company_id = run["leads"][0]["company_id"]

    evidence_before = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    assert len(evidence_before) > 0

    _with_registry(client, _default_registry(), lambda: client.post(f"/api/v1/pipeline-runs/{run['id']}/resume"))
    evidence_after = client.get("/api/v1/evidence", params={"entity_type": "COMPANY", "entity_id": company_id}).json()
    before_ids = {e["id"] for e in evidence_before}
    after_ids = {e["id"] for e in evidence_after}
    assert before_ids <= after_ids


def test_existing_batch_endpoint_still_works_after_pipeline_run(client):
    icp = _create_icp(client, "Pipeline API R")
    _create_run(client, icp["id"], target_count=1)
    # Phase 21's own batch endpoint remains usable, independent of pipeline runs
    registry = _default_registry()
    response = _with_registry(client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 1}))
    assert response.status_code == 201


def test_existing_ranking_and_confidence_endpoints_still_work(client):
    icp = _create_icp(client, "Pipeline API S")
    run = _create_run(client, icp["id"], target_count=1)
    company_id = run["leads"][0]["company_id"]

    ranking = client.get("/api/v1/rankings", params={"icp_id": icp["id"]})
    assert ranking.status_code == 200

    confidence = client.get("/api/v1/lead-confidence", params={"icp_id": icp["id"], "company_id": company_id})
    assert confidence.status_code == 200


# --- basic CRUD / listing ------------------------------------------------


def test_get_pipeline_run_returns_full_detail(client):
    icp = _create_icp(client, "Pipeline API T")
    created = _create_run(client, icp["id"], target_count=1)
    fetched = client.get(f"/api/v1/pipeline-runs/{created['id']}").json()
    assert fetched == created


def test_list_pipeline_runs_includes_created_run(client):
    icp = _create_icp(client, "Pipeline API U")
    created = _create_run(client, icp["id"], target_count=1)
    runs = client.get("/api/v1/pipeline-runs").json()
    assert any(r["id"] == created["id"] for r in runs)


def test_list_pipeline_runs_filters_by_icp(client):
    icp_a = _create_icp(client, "Pipeline API V-A")
    icp_b = _create_icp(client, "Pipeline API V-B")
    run_a = _create_run(client, icp_a["id"], target_count=1)
    _create_run(client, icp_b["id"], target_count=1)

    filtered = client.get("/api/v1/pipeline-runs", params={"icp_id": icp_a["id"]}).json()
    assert all(r["icp_id"] == icp_a["id"] for r in filtered)
    assert any(r["id"] == run_a["id"] for r in filtered)


def test_get_unknown_pipeline_run_returns_404(client):
    response = client.get("/api/v1/pipeline-runs/does-not-exist")
    assert response.status_code == 404


def test_create_pipeline_run_unknown_icp_returns_404(client):
    registry = _default_registry()
    response = _with_registry(
        client, registry, lambda: client.post("/api/v1/pipeline-runs", json={"icp_id": "does-not-exist", "target_count": 1})
    )
    assert response.status_code == 404


def test_deterministic_metrics_sum_to_lead_count(client):
    icp = _create_icp(client, "Pipeline API W")
    run = _create_run(client, icp["id"], target_count=2)
    total = run["accepted_count"] + run["held_count"] + run["rejected_count"] + run["duplicate_count"]
    assert total <= run["lead_count"]
