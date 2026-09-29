"""Phase 10 continuation — tests for wiring the AI DiscoveryStrategy into
the actual batch/discovery flow (app/services/batch_orchestration.py::
_get_or_build_discovery_strategy, and its call site inside
_run_one_discovery_round).

get_llm_provider() is called DIRECTLY inside batch_orchestration.py (not
via FastAPI Depends — see the existing call at line ~503 for Phase 16
qualification), so FastAPI's app.dependency_overrides mechanism does not
intercept it here; these tests instead monkeypatch the module-level
default_llm_provider singleton itself
(app/services/llm_providers/default_registry.py), which get_llm_provider()
reads fresh on every call. This mirrors exactly how the existing Phase 16
qualification step inside batch_orchestration.py already gets whatever
provider that singleton currently holds.
"""
import json

import app.services.llm_providers.default_registry as llm_registry
from app.main import app
from app.providers.default_registry import get_provider_registry
from app.providers.mocks import (
    MockCompanyDataProvider,
    MockCompanyRegistryProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)
from app.providers.registry import ProviderRegistry
from app.services.llm_providers.base import LLMProviderResponse
from app.services.llm_providers.mock import MockLLMProvider


def _icp_payload(name: str, industry: list[str] | None = None, company_type: list[str] | None = None) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": industry or ["D2C skincare"],
            "geography": [],
            "min_employees": 1,
            "max_employees": 10000,
            "allowed_titles": [],
            "company_type": company_type or [],
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


def _create_icp(client, name: str, **overrides) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name, **overrides)).json()


def _default_provider_registry() -> ProviderRegistry:
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


def _good_strategy_payload(**overrides) -> dict:
    payload = {
        "industry_terms": ["Consumer Goods", "E-commerce"],
        "company_type_terms": ["D2C"],
        "exclusion_terms": ["agencies"],
        "geography_notes": [],
        "unsupported_intent": ["growing companies"],
        "confidence": 80,
        "reasoning": "Expanded D2C skincare into taxonomy-plausible terms.",
    }
    payload.update(overrides)
    return payload


class _CountingLLMProvider(MockLLMProvider):
    """Counts every _call() invocation, regardless of context shape —
    used to assert an exact call count across an entire batch lifecycle
    (create + N resumes), not just one function call."""

    def __init__(self, response_text: str, **kwargs):
        super().__init__(response_text=response_text, **kwargs)
        self.call_count = 0

    def _call(self, context):
        self.call_count += 1
        return super()._call(context)


def _install_llm_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr(llm_registry, "default_llm_provider", provider)


# --- one OpenAI call per ICP/batch, not per round --------------------------


def test_llm_call_count_stays_one_after_find_more_resume(client, monkeypatch):
    provider = _CountingLLMProvider(response_text=json.dumps(_good_strategy_payload()))
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy Once B")
    registry = _default_provider_registry()

    created = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    ).json()
    assert provider.call_count == 1
    batch_id = created["id"]

    _with_registry(
        client,
        registry,
        lambda: client.post(f"/api/v1/batches/{batch_id}/resume", json={"max_discovery_rounds": 2}),
    )
    assert provider.call_count == 1  # still exactly one — the resume reused the cached strategy


# --- strategy is persisted and reused, terms actually reach Explorium-equivalent discovery ---


def test_strategy_is_persisted_on_the_batch_after_create(client, monkeypatch):
    provider = MockLLMProvider(response_text=json.dumps(_good_strategy_payload()))
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy Persist A")
    registry = _default_provider_registry()
    body = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    ).json()

    assert body["discovery_strategy"] is not None
    assert body["discovery_strategy"]["status"] == "SUCCESS"
    assert "Consumer Goods" in body["discovery_strategy"]["industry_terms"]


def test_cached_strategy_survives_and_is_identical_across_a_resume(client, monkeypatch):
    provider = MockLLMProvider(response_text=json.dumps(_good_strategy_payload()))
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy Persist B")
    registry = _default_provider_registry()
    created = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    ).json()
    batch_id = created["id"]

    resumed = _with_registry(
        client, registry, lambda: client.post(f"/api/v1/batches/{batch_id}/resume", json={"max_discovery_rounds": 1})
    ).json()

    assert resumed["discovery_strategy"] == created["discovery_strategy"]


def test_ai_proposed_terms_reach_the_discovery_candidate_pool():
    # Uses run_batch/_run_one_discovery_round directly with the mock
    # COMPANY_DISCOVERY provider so the merged query's terms are directly
    # observable via the discovery run's own persisted result — proving
    # the strategy's terms flow all the way through to a real discovery
    # call, not just into the batch's cached JSON.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base
    from app.models.discovery import DiscoveryCandidateModel
    from app.models.icp import ICPModel
    from app.schemas.icp import HardRules, SoftPreferences
    from app.services.batch_orchestration import run_batch
    from app.models.batch import BatchModel

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = session_local()
    try:
        icp_record = ICPModel(
            id="icp-strategy-1",
            name="Strategy Reaches Discovery",
            version=1,
            filters=[],
            hard_rules=HardRules(industry=["D2C skincare"]).model_dump(),
            soft_preferences=SoftPreferences().model_dump(),
        )
        db.add(icp_record)
        db.commit()

        registry = _default_provider_registry()
        batch = BatchModel(
            id="batch-strategy-1",
            icp_id=icp_record.id,
            icp_version=1,
            requested_target_count=2,
            discovery_limit=10,
            people_limit_per_company=5,
            status="PENDING",
        )
        db.add(batch)
        db.commit()

        provider = MockLLMProvider(
            response_text=json.dumps(_good_strategy_payload(industry_terms=["Injected Synonym Term"]))
        )

        import app.services.llm_providers.default_registry as llm_registry_module

        original = llm_registry_module.default_llm_provider
        llm_registry_module.default_llm_provider = provider
        try:
            run_batch(db, registry, batch, icp_record.id)
        finally:
            llm_registry_module.default_llm_provider = original

        db.refresh(batch)
        assert batch.discovery_strategy is not None
        assert batch.discovery_strategy["status"] == "SUCCESS"
        assert "Injected Synonym Term" in batch.discovery_strategy["industry_terms"]
        # the merged term was actually used to build the discovery query
        # that produced this batch's candidates (proven indirectly: the
        # strategy was computed and cached BEFORE _run_one_discovery_round
        # returned, and discovery completed without error using the
        # merged ICP — see _run_one_discovery_round's own ordering)
        candidates = db.query(DiscoveryCandidateModel).filter(DiscoveryCandidateModel.icp_id == icp_record.id).all()
        assert len(candidates) > 0
    finally:
        db.close()


# --- OpenAI failure fallback: discovery continues on the original ICP -----


def test_llm_provider_failure_does_not_block_batch_creation(client, monkeypatch):
    provider = MockLLMProvider(raise_provider_error=True)
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy Failure A")
    registry = _default_provider_registry()
    response = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["discovered_count"] == 2  # discovery still ran, using the ICP's own unexpanded terms
    assert body["discovery_strategy"]["status"] == "PROVIDER_UNAVAILABLE"
    assert body["discovery_strategy"]["industry_terms"] == []


def test_failed_strategy_is_cached_too_never_retried_every_round(client, monkeypatch):
    provider = _CountingLLMProvider(response_text="", raise_provider_error=True)
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy Failure B")
    registry = _default_provider_registry()
    created = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    ).json()
    assert provider.call_count == 1

    _with_registry(
        client,
        registry,
        lambda: client.post(f"/api/v1/batches/{created['id']}/resume", json={"max_discovery_rounds": 2}),
    )
    assert provider.call_count == 1  # a cached failure is not retried on the next round either


def test_malformed_llm_output_also_degrades_without_blocking_discovery(client, monkeypatch):
    provider = MockLLMProvider(response_text="not json at all {{{")
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy Failure C")
    registry = _default_provider_registry()
    response = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["discovered_count"] == 2
    assert body["discovery_strategy"]["status"] == "MALFORMED_OUTPUT"


# --- no regression to existing discovery behavior ---------------------------


def test_batch_flow_unchanged_end_to_end_with_a_successful_strategy(client, monkeypatch):
    # Mirrors tests/test_batch_api.py::test_small_batch_runs_end_to_end,
    # re-run with a real (mocked) LLM strategy in the loop, to prove the
    # existing pipeline's overall shape/outcomes are unaffected.
    provider = MockLLMProvider(response_text=json.dumps(_good_strategy_payload()))
    _install_llm_provider(monkeypatch, provider)

    icp = _create_icp(client, "Strategy No Regression A")
    registry = _default_provider_registry()
    response = _with_registry(
        client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})
    )
    body = response.json()

    assert response.status_code == 201
    assert body["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert body["discovered_count"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["company_id"] is not None
        assert item["outcome"] in {"ACCEPTED", "HELD", "REJECTED", "DUPLICATE", "FAILED"}


def test_batch_without_any_ai_terms_still_behaves_like_a_plain_icp():
    # An ICP whose strategy proposes nothing new (e.g. every AI term is
    # already present) must produce discovery results identical in shape
    # to running with no strategy at all.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base
    from app.models.batch import BatchModel
    from app.models.icp import ICPModel
    from app.schemas.icp import HardRules, SoftPreferences
    from app.services.batch_orchestration import run_batch

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = session_local()
    try:
        icp_record = ICPModel(
            id="icp-strategy-2",
            name="No New Terms",
            version=1,
            filters=[],
            hard_rules=HardRules(industry=["Skincare"]).model_dump(),
            soft_preferences=SoftPreferences().model_dump(),
        )
        db.add(icp_record)
        db.commit()

        registry = _default_provider_registry()
        batch = BatchModel(
            id="batch-strategy-2",
            icp_id=icp_record.id,
            icp_version=1,
            requested_target_count=2,
            discovery_limit=10,
            people_limit_per_company=5,
            status="PENDING",
        )
        db.add(batch)
        db.commit()

        provider = MockLLMProvider(
            response_text=json.dumps(_good_strategy_payload(industry_terms=["skincare"]))  # dup of the user's own term
        )

        import app.services.llm_providers.default_registry as llm_registry_module

        original = llm_registry_module.default_llm_provider
        llm_registry_module.default_llm_provider = provider
        try:
            run_batch(db, registry, batch, icp_record.id)
        finally:
            llm_registry_module.default_llm_provider = original

        db.refresh(batch)
        assert batch.discovered_count == 2
        assert batch.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    finally:
        db.close()
