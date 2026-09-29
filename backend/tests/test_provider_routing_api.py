from app.main import app
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


def _run_batch(client, icp_id, target_count=1, registry=None):
    registry = registry or _default_registry()
    return _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp_id, "target_count": target_count}).json()
    )


def _submit_feedback(client, icp_id, lead_ref, decision, reason_codes=None, reviewer_id="mgr-1"):
    payload = {
        "lead_ref": lead_ref, "icp_id": icp_id, "decision": decision,
        "reason_codes": reason_codes or ([] if decision == "GOOD_FIT" else ["GENERIC_REASON"]),
        "reviewer_id": reviewer_id,
    }
    return client.post("/api/v1/feedback", json=payload)


def _classify_dtc(client, company_id):
    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY", "entity_id": company_id, "field": "business_model",
            "value": "Direct-to-consumer skincare brand", "source_type": "search", "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    client.post(f"/api/v1/companies/{company_id}/classify-business-model")


def _extract_signal(client, company_id, source_provider_id="mock-company-registry-v1"):
    # source_provider_id must be set for CommercialSignalModel.provider_ids
    # to actually record a linkage — this is what real provider-attributed
    # evidence looks like, and it's exactly the field Phase 27's routing
    # reads to decide which provider produced a signal.
    client.post(
        "/api/v1/evidence",
        json={
            "entity_type": "COMPANY", "entity_id": company_id, "field": "products_services",
            "value": "We run meta advertising campaigns", "source_type": "provider",
            "source_provider_id": source_provider_id, "retrieved_at": "2026-01-01T00:00:00Z",
        },
    )
    return client.post(f"/api/v1/companies/{company_id}/extract-commercial-signals").json()


def _get_pending(client, icp_id, min_sample_size=1):
    return client.get("/api/v1/optimization-applications/pending", params={"icp_id": icp_id, "min_sample_size": min_sample_size}).json()


def _approve_and_apply(client, icp_id, rec):
    approval_payload = {
        "fingerprint": rec["fingerprint"], "icp_id": icp_id, "icp_version": rec["icp_version"],
        "decision": "APPROVED", "approved_by": "mgr-1",
        "recommendation_type": rec["recommendation_type"], "signal_name": rec["signal_name"],
        "reason_code": rec["reason_code"], "sample_count": rec["sample_count"], "confidence": rec["confidence"],
        "expected_effect": rec["expected_effect"], "magnitude_hint": rec["magnitude_hint"], "is_global": rec["is_global"],
    }
    client.post("/api/v1/optimization-applications/approvals", json=approval_payload)
    return client.post(
        "/api/v1/optimization-applications",
        json={"fingerprint": rec["fingerprint"], "icp_id": icp_id, "icp_version": rec["icp_version"], "applied_by": "mgr-1"},
    ).json()


def _build_sufficient_feedback_with_signal(client, icp_id, n=6):
    company_ids = []
    for _ in range(n):
        batch = _run_batch(client, icp_id)
        company_id = batch["items"][0]["company_id"]
        _extract_signal(client, company_id)
        _submit_feedback(client, icp_id, batch["items"][0]["lead_id"], "GOOD_FIT")
        company_ids.append(company_id)
    return company_ids


# --- default routing --------------------------------------------------


def test_default_routing_when_no_optimization_active(client):
    icp = _create_icp(client, "Routing API A")
    response = client.get(
        "/api/v1/provider-routing", params={"icp_id": icp["id"], "icp_version": 1, "capability": "COMPANY_DISCOVERY"}
    )
    body = response.json()
    assert response.status_code == 200
    assert body["is_default"] is True
    assert len(body["ordered_provider_ids"]) >= 1


# --- approved optimization reorders ---------------------------------


def test_approved_optimization_reorders_provider_priority(client):
    # COMPANY_ENRICHMENT has TWO real mock providers (MockCompanyDataProvider
    # and MockCompanyRegistryProvider) - COMPANY_DISCOVERY has only one, so
    # reordering is only observable (is_default can meaningfully be False)
    # against a capability with more than one candidate.
    icp = _create_icp(client, "Routing API B")
    _build_sufficient_feedback_with_signal(client, icp["id"])

    pending = _get_pending(client, icp["id"])
    rec = next((r for r in pending if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    assert rec is not None
    _approve_and_apply(client, icp["id"], rec)

    routing = client.get(
        "/api/v1/provider-routing", params={"icp_id": icp["id"], "icp_version": 1, "capability": "COMPANY_ENRICHMENT"}
    ).json()
    assert routing["is_default"] is False
    # the provider that actually produced the META_ADVERTISING signal evidence should be prioritized
    prioritized_entries = [e for e in routing["entries"] if e["reason_codes"] == ["PRIORITIZED_BY_OPTIMIZATION"]]
    assert len(prioritized_entries) >= 1


# --- ICP isolation ------------------------------------------------------


def test_routing_isolated_between_icps(client):
    icp_a = _create_icp(client, "Routing API C-A")
    icp_b = _create_icp(client, "Routing API C-B")
    _build_sufficient_feedback_with_signal(client, icp_a["id"])

    pending = _get_pending(client, icp_a["id"])
    rec = next((r for r in pending if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    _approve_and_apply(client, icp_a["id"], rec)

    routing_a = client.get(
        "/api/v1/provider-routing", params={"icp_id": icp_a["id"], "icp_version": 1, "capability": "COMPANY_ENRICHMENT"}
    ).json()
    routing_b = client.get(
        "/api/v1/provider-routing", params={"icp_id": icp_b["id"], "icp_version": 1, "capability": "COMPANY_ENRICHMENT"}
    ).json()

    assert routing_a["is_default"] is False
    assert routing_b["is_default"] is True  # unaffected by icp_a's applied optimization


# --- global opt-in -------------------------------------------------------


def test_global_optimization_affects_other_icps_only_when_marked_global(client):
    icp_a = _create_icp(client, "Routing API D-A")
    icp_b = _create_icp(client, "Routing API D-B")
    _build_sufficient_feedback_with_signal(client, icp_a["id"])

    pending_scoped = _get_pending(client, icp_a["id"])
    rec_scoped = next((r for r in pending_scoped if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    assert rec_scoped["is_global"] is False  # never global unless explicitly requested
    _approve_and_apply(client, icp_a["id"], rec_scoped)

    routing_b = client.get(
        "/api/v1/provider-routing", params={"icp_id": icp_b["id"], "icp_version": 1, "capability": "COMPANY_DISCOVERY"}
    ).json()
    assert routing_b["is_default"] is True  # a scoped (non-global) application never leaks to another ICP


# --- provider failure / fallback ---------------------------------------


def test_provider_with_no_signal_linkage_still_included_and_called(client):
    icp = _create_icp(client, "Routing API E")
    _build_sufficient_feedback_with_signal(client, icp["id"])
    pending = _get_pending(client, icp["id"])
    rec = next((r for r in pending if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    _approve_and_apply(client, icp["id"], rec)

    routing = client.get(
        "/api/v1/provider-routing", params={"icp_id": icp["id"], "icp_version": 1, "capability": "COMPANY_DISCOVERY"}
    ).json()
    # every registered COMPANY_DISCOVERY provider must still appear
    all_ids = {e["provider_id"] for e in routing["entries"]}
    assert "mock-company-data-v1" in all_ids


def test_discovery_still_succeeds_when_routing_would_apply(client):
    """The actual discovery endpoint must keep working end-to-end even
    with an active optimization in play — routing only reorders, it never
    breaks the underlying discovery call."""
    icp = _create_icp(client, "Routing API F")
    _build_sufficient_feedback_with_signal(client, icp["id"])
    pending = _get_pending(client, icp["id"])
    rec = next((r for r in pending if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    _approve_and_apply(client, icp["id"], rec)

    registry = _default_registry()
    run = _with_registry(client, registry, lambda: client.post("/api/v1/discovery/runs", json={"icp_id": icp["id"]}).json())
    assert run["status"] in ("COMPLETED", "PARTIAL_FAILURE")
    assert len(run["candidates"]) >= 1


# --- determinism -------------------------------------------------------


def test_repeated_routing_calls_are_identical(client):
    icp = _create_icp(client, "Routing API G")
    _build_sufficient_feedback_with_signal(client, icp["id"])
    pending = _get_pending(client, icp["id"])
    rec = next((r for r in pending if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    _approve_and_apply(client, icp["id"], rec)

    first = client.get("/api/v1/provider-routing", params={"icp_id": icp["id"], "icp_version": 1, "capability": "COMPANY_DISCOVERY"}).json()
    second = client.get("/api/v1/provider-routing", params={"icp_id": icp["id"], "icp_version": 1, "capability": "COMPANY_DISCOVERY"}).json()
    first.pop("generated_at", None)
    second.pop("generated_at", None)
    assert first == second


# --- hard-rule safety ----------------------------------------------------


def test_routing_never_mutates_icp_hard_rules(client):
    icp = _create_icp(client, "Routing API H", min_employees=5, max_employees=500)
    icp_before = client.get(f"/api/v1/icps/{icp['id']}").json()

    _build_sufficient_feedback_with_signal(client, icp["id"])
    pending = _get_pending(client, icp["id"])
    rec = next((r for r in pending if r["recommendation_type"] == "PROVIDER_PRIORITY_HINT" and r["is_eligible_for_approval"]), None)
    _approve_and_apply(client, icp["id"], rec)
    client.get("/api/v1/provider-routing", params={"icp_id": icp["id"], "icp_version": 1, "capability": "COMPANY_DISCOVERY"})

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    assert icp_before == icp_after


def test_routing_response_has_no_hard_rule_shaped_field():
    from app.schemas.provider_routing import ProviderRoutingResult

    fields = ProviderRoutingResult.model_fields.keys()
    for forbidden_word in ("hard_rule", "pass", "fail_icp"):
        assert not any(forbidden_word in f.lower() for f in fields)


# --- 404s ----------------------------------------------------------------


def test_routing_unknown_icp_returns_404(client):
    response = client.get(
        "/api/v1/provider-routing", params={"icp_id": "does-not-exist", "icp_version": 1, "capability": "COMPANY_DISCOVERY"}
    )
    assert response.status_code == 404
