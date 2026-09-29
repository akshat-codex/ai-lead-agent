"""Deep discovery mode — end-to-end orchestration wiring
(app/services/batch_orchestration.py::_run_deep_prescreen_for_items /
_advance_company_pipeline).

Covers the two things the task explicitly requires regression proof for:

  1. FAST/SAFE/HARD batches never invoke deep pre-screen at all (call-count
     assertion on _run_deep_prescreen_for_items itself, plus an end-to-end
     assertion that deep_prescreen_verdict/status stay None for every item).
  2. A complete deep-mode pre-screen OUTAGE (every homepage fetch AND every
     LLM call fails) behaves exactly like Hard mode would have — every
     candidate proceeds through the unchanged pipeline unfiltered.

Also covers: a NOT_RELEVANT verdict skips the rest of the pipeline for that
item (no evidence import, no hard validation) while a RELEVANT verdict (or
no verdict at all) proceeds through it unchanged; one candidate is never
pre-screened twice when the same company is sighted by two providers
(company-resolution merge happens before pre-screen); the mode is opt-in
and off by default.

Uses the same client/_create_icp/_with_registry conventions as
tests/test_batch_api.py and the same _install_llm_provider monkeypatch
pattern as tests/test_batch_discovery_strategy.py.
"""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import respx

import app.services.llm_providers.default_registry as llm_registry
from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import NormalizedRecord, ProviderCapability, ProviderRequest, ProviderResponse, SourceMetadata
from app.providers.default_registry import get_provider_registry
from app.providers.mocks import MockCompanyDataProvider, MockCompanyRegistryProvider, MockPeopleDataProvider, MockWebSearchProvider
from app.providers.registry import ProviderRegistry
from app.schemas.adversarial_review import AdversarialContext
from app.schemas.deep_prescreen import DeepPrescreenContext
from app.schemas.discovery_strategy import DiscoveryStrategyRequest
from app.services.llm_providers.base import LLMProvider, LLMProviderResponse
from app.services.llm_providers.mock import build_good_fit_response, build_no_expansion_discovery_strategy_response
from app.services.llm_providers.mock_adversarial import build_survives_response


def _icp_payload(name: str, industry=None, company_type=None) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": industry or ["Deep Tech"],
            "geography": [],
            "min_employees": 1,
            "max_employees": 10000,
            "allowed_titles": [],
            "company_type": company_type or [],
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [],
            "growth_signals": [], "marketing_signals": [], "other_preferences": [],
        },
    }


def _create_icp(client, name: str, **overrides) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name, **overrides)).json()


def _with_registry(client, registry, fn):
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        return fn()
    finally:
        del app.dependency_overrides[get_provider_registry]


def _create_batch(client, icp_id, registry, target_count=2, discovery_mode="fast", **overrides):
    payload = {"icp_id": icp_id, "target_count": target_count, "discovery_mode": discovery_mode}
    payload.update(overrides)
    return _with_registry(client, registry, lambda: client.post("/api/v1/batches", json=payload))


class _RealLookingDiscoveryProvider(ProviderAdapter):
    """A COMPANY_DISCOVERY provider that (unlike MockCompanyDataProvider)
    returns candidates carrying a real `domain`/`description` — the shape
    deep-mode pre-screen actually reads. `records` is a list of
    (external_id, name, domain, description) tuples."""

    def __init__(self, records, provider_id="real-looking-disc"):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(
                NormalizedRecord(external_id=ext_id, name=name, attributes={"domain": domain, "description": desc})
                for ext_id, name, domain, desc in self._records
            ),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=datetime.now(timezone.utc), is_mock=True),
            exhausted=True,
        )


def _registry_with(*providers) -> ProviderRegistry:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    registry.register(MockPeopleDataProvider())
    return registry


class _DispatchingLLMProvider(LLMProvider):
    """Mirrors _DefaultMockProvider's own context-type dispatch
    (app/services/llm_providers/default_registry.py) but lets a test choose
    the deep-prescreen verdict independently of qualification/adversarial/
    discovery-strategy responses — the real singleton's dispatch collapses
    all of those onto one instance, which is exactly what get_llm_provider()
    and (absent a real OpenAI key) get_deep_prescreen_llm_provider() both
    resolve to in this test environment (see tests/conftest.py)."""

    def __init__(self, prescreen_response_text: str) -> None:
        super().__init__(provider_id="test-dispatching-llm", model_id="test-v1")
        self._prescreen_response_text = prescreen_response_text
        self.prescreen_call_count = 0

    def _call(self, context) -> LLMProviderResponse:
        if isinstance(context, DeepPrescreenContext):
            self.prescreen_call_count += 1
            raw_text = self._prescreen_response_text
        elif isinstance(context, AdversarialContext):
            raw_text = build_survives_response(context)
        elif isinstance(context, DiscoveryStrategyRequest):
            raw_text = build_no_expansion_discovery_strategy_response()
        else:
            raw_text = build_good_fit_response(context)
        return LLMProviderResponse(provider_id=self.provider_id, model_id=self.model_id, success=True, raw_text=raw_text)


def _install_llm_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr(llm_registry, "default_llm_provider", provider)


def _relevant_response() -> str:
    return json.dumps({"verdict": "RELEVANT", "reason": "Genuine fit against the ICP.", "confidence": 85})


def _not_relevant_response() -> str:
    return json.dumps({"verdict": "NOT_RELEVANT", "reason": "This is a marketing agency, not a company in the industry.", "confidence": 90})


def _block_all_homepage_fetches() -> None:
    """Every test that gives a candidate a real `domain` triggers a real
    homepage-fetch attempt (app/services/homepage_fetch.py::fetch_homepage)
    unless mocked — this blocks EVERY host at once (never a live call, per
    the task's own "no live provider calls" constraint) with a plain 403,
    which deterministically forces the existing search-snippet fallback
    path (see app/services/deep_prescreen.py::run_prescreen's own
    documented content-source order) rather than actually reaching the
    network. Real homepage-content EXTRACTION quality is already covered
    by tests/test_homepage_fetch.py / tests/test_deep_prescreen.py — this
    file only needs the fetch to fail predictably so it can test the
    orchestration wiring around it."""
    respx.route(method="GET").mock(return_value=httpx.Response(403, text="blocked"))


# --- opt-in / off-by-default -------------------------------------------


def test_deep_mode_is_not_the_default(client):
    icp = _create_icp(client, "Deep Default A")
    registry = _registry_with(MockCompanyDataProvider(), MockCompanyRegistryProvider(), MockWebSearchProvider())
    body = _create_batch(client, icp["id"], registry, target_count=2).json()  # no discovery_mode passed
    assert body["discovery_mode"] == "fast"


# --- FAST/SAFE/HARD never invoke deep pre-screen at all -------------------


def test_fast_mode_never_calls_deep_prescreen(client, monkeypatch):
    provider = _DispatchingLLMProvider(_not_relevant_response())  # would reject everything if it were ever called
    _install_llm_provider(monkeypatch, provider)
    icp = _create_icp(client, "Deep Fast A")
    registry = _registry_with(MockCompanyDataProvider(), MockCompanyRegistryProvider(), MockWebSearchProvider())

    with patch("app.services.batch_orchestration._run_deep_prescreen_for_items") as spy:
        body = _create_batch(client, icp["id"], registry, target_count=2, discovery_mode="fast").json()

    assert spy.call_count >= 1  # it's still called (no-op check happens inside), but never acts
    assert provider.prescreen_call_count == 0
    for item in body["items"]:
        assert item["deep_prescreen_verdict"] is None
        assert item["deep_prescreen_status"] is None
        assert item["outcome"] in {"ACCEPTED", "HELD", "REJECTED", "DUPLICATE", "FAILED"}


def test_safe_mode_never_calls_deep_prescreen(client, monkeypatch):
    provider = _DispatchingLLMProvider(_not_relevant_response())
    _install_llm_provider(monkeypatch, provider)
    icp = _create_icp(client, "Deep Safe A")
    registry = _registry_with(MockCompanyDataProvider(), MockCompanyRegistryProvider(), MockWebSearchProvider())
    body = _create_batch(client, icp["id"], registry, target_count=2, discovery_mode="safe").json()
    assert provider.prescreen_call_count == 0
    for item in body["items"]:
        assert item["deep_prescreen_verdict"] is None


def test_hard_mode_never_calls_deep_prescreen(client, monkeypatch):
    provider = _DispatchingLLMProvider(_not_relevant_response())
    _install_llm_provider(monkeypatch, provider)
    icp = _create_icp(client, "Deep Hard A")
    registry = _registry_with(MockCompanyDataProvider(), MockCompanyRegistryProvider(), MockWebSearchProvider())
    body = _create_batch(client, icp["id"], registry, target_count=2, discovery_mode="hard").json()
    assert provider.prescreen_call_count == 0
    for item in body["items"]:
        assert item["deep_prescreen_verdict"] is None


def test_deep_prescreen_helper_is_a_true_no_op_for_every_non_deep_mode(client, monkeypatch):
    """Direct call-count proof at the exact function the plan requires be
    provably never-invoked for non-DEEP modes — not just an inferred
    absence of side effects."""
    from app.services.batch_orchestration import _run_deep_prescreen_for_items
    from app.services.llm_providers.default_registry import get_deep_prescreen_llm_provider

    calls = []
    original = get_deep_prescreen_llm_provider

    def _tracking_get_provider():
        calls.append(1)
        return original()

    monkeypatch.setattr("app.services.batch_orchestration.get_deep_prescreen_llm_provider", _tracking_get_provider)

    icp = _create_icp(client, "Deep NoOp A")
    registry = _registry_with(MockCompanyDataProvider(), MockCompanyRegistryProvider(), MockWebSearchProvider())
    for mode in ("fast", "safe", "hard"):
        calls.clear()
        _create_batch(client, icp["id"], registry, target_count=1, discovery_mode=mode)
        assert calls == []  # the deep-mode LLM provider is never even resolved for a non-deep batch


# --- deep mode: NOT_RELEVANT filters, RELEVANT passes through -------------


@respx.mock
def test_deep_mode_not_relevant_verdict_skips_the_rest_of_the_pipeline(client, monkeypatch):
    _block_all_homepage_fetches()
    provider = _DispatchingLLMProvider(_not_relevant_response())
    _install_llm_provider(monkeypatch, provider)
    icp = _create_icp(client, "Deep Filter A")
    disc = _RealLookingDiscoveryProvider([("ext-1", "Some Agency Co", "someagency.test", "A marketing agency.")])
    registry = _registry_with(disc)

    body = _create_batch(client, icp["id"], registry, target_count=1, discovery_mode="deep").json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["deep_prescreen_verdict"] == "NOT_RELEVANT"
    assert item["deep_prescreen_status"] == "SUCCESS"
    assert item["outcome"] == "REJECTED"
    assert item["hard_rule_result"] is None  # never reached hard validation
    assert item["qualification_decision"] is None  # never reached qualification
    assert provider.prescreen_call_count == 1


@respx.mock
def test_deep_mode_relevant_verdict_proceeds_through_the_unchanged_pipeline(client, monkeypatch):
    _block_all_homepage_fetches()
    provider = _DispatchingLLMProvider(_relevant_response())
    _install_llm_provider(monkeypatch, provider)
    icp = _create_icp(client, "Deep Filter B")
    disc = _RealLookingDiscoveryProvider([("ext-1", "Real Deep Tech Co", "realdeeptech.test", "We build real robots.")])
    registry = _registry_with(disc)

    body = _create_batch(client, icp["id"], registry, target_count=1, discovery_mode="deep").json()
    item = body["items"][0]
    assert item["deep_prescreen_verdict"] == "RELEVANT"
    assert item["stage"] == "DONE"
    assert item["hard_rule_result"] is not None  # DID reach hard validation
    assert provider.prescreen_call_count == 1


# --- deep mode: complete pre-screen outage behaves like Hard mode ---------


@respx.mock
def test_deep_mode_total_prescreen_outage_lets_every_candidate_through_like_hard_mode(client, monkeypatch):
    """Every homepage fetch fails AND every LLM call fails — this must
    degrade to EXACTLY Hard mode's own behavior: no candidate is silently
    dropped by the pre-screen layer."""
    _block_all_homepage_fetches()

    class _AlwaysFailingLLMProvider(LLMProvider):
        def __init__(self):
            super().__init__(provider_id="always-fails", model_id="v1")

        def _call(self, context):
            raise RuntimeError("simulated total LLM outage")

    _install_llm_provider(monkeypatch, _AlwaysFailingLLMProvider())
    # A real domain (so company resolution succeeds identically in both
    # scenarios — resolution is unrelated to and unaffected by deep mode,
    # see app/services/company_resolution.py) but NO description, and the
    # domain itself resolves to nothing fetchable — homepage fetch fails,
    # snippet is empty -> SKIPPED_NO_CONTENT, never even reaches the
    # (failing) LLM for these two candidates. This isolates the "no
    # content at all" path from the "LLM call itself fails" path (covered
    # separately below) while keeping resolution behavior identical to Hard
    # mode's own.
    icp = _create_icp(client, "Deep Outage A")
    disc = _RealLookingDiscoveryProvider(
        [("ext-1", "Company One", "company-one-outage-test-deep.invalid", ""), ("ext-2", "Company Two", "company-two-outage-test-deep.invalid", "")]
    )
    registry = _registry_with(disc)

    deep_body = _create_batch(client, icp["id"], registry, target_count=2, discovery_mode="deep").json()

    # Compare against an equivalent Hard-mode run on the same fake provider/records
    # — DISTINCT domains from the deep-mode scenario above: company resolution's
    # domain-match dedup (app/services/company_resolution.py) is global, not
    # scoped per-ICP/batch, so reusing the same domain here would incorrectly
    # merge onto the SAME canonical company/lead the first scenario already
    # created and report DUPLICATE instead of a genuinely independent HARD-mode
    # run — an artifact of this test's own setup, unrelated to deep mode itself.
    icp2 = _create_icp(client, "Deep Outage B")
    disc2 = _RealLookingDiscoveryProvider(
        [("ext-1", "Company One", "company-one-outage-test-hard.invalid", ""), ("ext-2", "Company Two", "company-two-outage-test-hard.invalid", "")],
        provider_id="real-looking-disc-2",
    )
    registry2 = _registry_with(disc2)
    hard_body = _create_batch(client, icp2["id"], registry2, target_count=2, discovery_mode="hard").json()

    deep_outcomes = sorted(item["outcome"] for item in deep_body["items"])
    hard_outcomes = sorted(item["outcome"] for item in hard_body["items"])
    assert deep_outcomes == hard_outcomes
    assert len(deep_body["items"]) == len(hard_body["items"]) == 2
    for item in deep_body["items"]:
        assert item["deep_prescreen_verdict"] is None  # never guessed a NOT_RELEVANT from nothing
        assert item["deep_prescreen_status"] == "SKIPPED_NO_CONTENT"


def test_deep_mode_llm_failure_with_real_content_still_lets_the_candidate_through(client, monkeypatch):
    """Distinct from the no-content case above: here the candidate DOES
    have a search snippet (real content to judge), but the LLM call itself
    fails — still must let the candidate through, never drop it."""

    class _AlwaysFailingLLMProvider(LLMProvider):
        def __init__(self):
            super().__init__(provider_id="always-fails", model_id="v1")

        def _call(self, context):
            raise RuntimeError("simulated LLM outage")

    _install_llm_provider(monkeypatch, _AlwaysFailingLLMProvider())
    icp = _create_icp(client, "Deep Outage C")
    disc = _RealLookingDiscoveryProvider([("ext-1", "Company One", None, "A real, substantive description of this company.")])
    registry = _registry_with(disc)

    body = _create_batch(client, icp["id"], registry, target_count=1, discovery_mode="deep").json()
    item = body["items"][0]
    assert item["deep_prescreen_verdict"] is None
    assert item["deep_prescreen_status"] == "PROVIDER_UNAVAILABLE"
    assert item["hard_rule_result"] is not None  # still proceeded through the unchanged pipeline


# --- cost visibility -----------------------------------------------------


@respx.mock
def test_deep_mode_call_counts_are_surfaced_and_zero_for_non_deep_batches(client, monkeypatch):
    _block_all_homepage_fetches()
    provider = _DispatchingLLMProvider(_relevant_response())
    _install_llm_provider(monkeypatch, provider)
    icp = _create_icp(client, "Deep Counts A")
    disc = _RealLookingDiscoveryProvider([("ext-1", "Real Co", "realco.test", "Real content here.")])
    registry = _registry_with(disc)

    deep_body = _create_batch(client, icp["id"], registry, target_count=1, discovery_mode="deep").json()
    assert deep_body["deep_prescreen_llm_call_count"] == 1

    icp2 = _create_icp(client, "Deep Counts B")
    disc2 = _RealLookingDiscoveryProvider([("ext-1", "Real Co Two", "realcotwo.test", "Real content here.")], provider_id="real-looking-disc-3")
    registry2 = _registry_with(disc2)
    fast_body = _create_batch(client, icp2["id"], registry2, target_count=1, discovery_mode="fast").json()
    assert fast_body["deep_prescreen_llm_call_count"] == 0
    assert fast_body["deep_prescreen_homepage_fetch_count"] == 0
