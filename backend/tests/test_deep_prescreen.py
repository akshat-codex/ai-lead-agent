"""Deep discovery mode — app/services/deep_prescreen.py.

No live calls: homepage fetches are mocked via respx and the LLM provider
is a deterministic MockLLMProvider, mirroring tests/test_llm_qualification.py's
own conventions. Confirms:

  - build_icp_summary renders the whole canonical ICP deterministically.
  - run_prescreen resolves content in the documented order (homepage ->
    search snippet -> SKIPPED_NO_CONTENT) and never calls the LLM with no
    content at all.
  - Every provider/parse failure mode (timeout, provider error, malformed
    JSON, schema-invalid output) degrades to verdict=None, never raises.
  - RawDeepPrescreenOutput's anti-hallucination floor: extra="forbid" and
    the enum-constrained verdict field reject a fabricated shape.
  - run_prescreen_batch respects deep_prescreen_max_concurrency, isolates a
    per-candidate worker failure from every other candidate's result, and
    guarantees a result for every input candidate.
"""
import json
import time

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.deep_prescreen import DeepPrescreenContext, RawDeepPrescreenOutput
from app.services.deep_prescreen import PrescreenCandidate, build_icp_summary, run_prescreen, run_prescreen_batch
from app.services.llm_providers.base import LLMProviderError, LLMProviderErrorCode, LLMProviderResponse
from app.services.llm_providers.mock import MockLLMProvider


def _icp(industries=("Deep Tech",), company_types=(), allowed_titles=(), exclusions=()) -> CanonicalICP:
    return CanonicalICP(
        icp_id="icp-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=industries,
            company_types=company_types,
            allowed_titles=allowed_titles,
            exclusions=exclusions,
            geography=CanonicalGeography(countries=(GeographyEntry(raw="US", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=10, max=200),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_url="sqlite:///:memory:",
        deep_prescreen_homepage_timeout_seconds=8.0,
        deep_prescreen_homepage_max_bytes=300_000,
        deep_prescreen_homepage_max_chars=6_000,
        deep_prescreen_max_concurrency=4,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _relevant_response(reason="Genuine fit.") -> str:
    return json.dumps({"verdict": "RELEVANT", "reason": reason, "confidence": 80})


def _not_relevant_response(reason="This is a marketing agency, not a company in the target industry.") -> str:
    return json.dumps({"verdict": "NOT_RELEVANT", "reason": reason, "confidence": 90})


# --- build_icp_summary --------------------------------------------------


def test_icp_summary_includes_every_populated_hard_rule_field():
    icp = _icp(industries=("Deep Tech", "Robotics"), company_types=("Startup",), allowed_titles=("CTO",), exclusions=("Acme",))
    summary = build_icp_summary(icp)
    assert "Deep Tech" in summary and "Robotics" in summary
    assert "Startup" in summary
    assert "United States" in summary
    assert "10-200" in summary
    assert "CTO" in summary
    assert "Acme" in summary


def test_icp_summary_is_deterministic():
    icp = _icp()
    assert build_icp_summary(icp) == build_icp_summary(icp)


def test_icp_summary_never_empty_even_with_no_hard_rules():
    icp = CanonicalICP(icp_id="icp-empty", version=1, hard_rules=CanonicalHardRules(), soft_preferences=CanonicalSoftPreferences())
    summary = build_icp_summary(icp)
    assert summary  # never an empty string


# --- content-source resolution order ------------------------------------


@respx.mock
def test_uses_real_homepage_content_when_the_fetch_succeeds():
    respx.get("https://acme.test/").mock(
        return_value=httpx.Response(200, html="<html><body><p>" + ("We build real widgets for factories. " * 20) + "</p></body></html>")
    )
    provider = MockLLMProvider(response_text=_relevant_response())
    result = run_prescreen("ICP summary", "Acme Corp", "https://acme.test/", search_snippet="", provider=provider, settings=_settings())
    assert result.status == "SUCCESS"
    assert result.content_source == "homepage"
    assert result.homepage_fetched is True
    assert result.verdict == "RELEVANT"


@respx.mock
def test_falls_back_to_search_snippet_when_homepage_fetch_fails():
    respx.get("https://dead.test/").mock(side_effect=httpx.ConnectError("refused"))
    provider = MockLLMProvider(response_text=_relevant_response())
    result = run_prescreen(
        "ICP summary", "Dead Co", "https://dead.test/", search_snippet="A real search snippet describing this company.",
        provider=provider, settings=_settings(),
    )
    assert result.status == "SUCCESS"
    assert result.content_source == "search_snippet"
    assert result.homepage_fetched is False


def test_falls_back_to_search_snippet_when_no_homepage_url_at_all():
    provider = MockLLMProvider(response_text=_relevant_response())
    result = run_prescreen(
        "ICP summary", "No Domain Co", None, search_snippet="A real search snippet.", provider=provider, settings=_settings()
    )
    assert result.content_source == "search_snippet"
    assert result.homepage_fetched is False


def test_skips_the_llm_call_entirely_when_no_content_is_available_at_all():
    calls = []

    class _RecordingProvider(MockLLMProvider):
        def _call(self, context):
            calls.append(context)
            return super()._call(context)

    provider = _RecordingProvider(response_text=_relevant_response())
    result = run_prescreen("ICP summary", "No Content Co", None, search_snippet="", provider=provider, settings=_settings())
    assert result.status == "SKIPPED_NO_CONTENT"
    assert result.verdict is None
    assert calls == []  # the LLM was never called — never guess relevance from a bare name


@respx.mock
def test_never_guesses_from_a_bare_name_even_when_homepage_returns_a_blocked_page():
    respx.get("https://blocked.test/").mock(
        return_value=httpx.Response(200, html="<html><body><h1>Please verify you are human</h1>" + ("pad " * 60) + "</body></html>")
    )
    calls = []

    class _RecordingProvider(MockLLMProvider):
        def _call(self, context):
            calls.append(context)
            return super()._call(context)

    provider = _RecordingProvider(response_text=_relevant_response())
    result = run_prescreen("ICP summary", "Blocked Co", "https://blocked.test/", search_snippet="", provider=provider, settings=_settings())
    assert result.status == "SKIPPED_NO_CONTENT"
    assert calls == []


# --- failure modes never raise, always degrade to verdict=None ----------


def test_provider_timeout_degrades_to_provider_unavailable_verdict_none():
    provider = MockLLMProvider(raise_timeout=True)
    result = run_prescreen("ICP", "Some Co", None, search_snippet="a real snippet", provider=provider, settings=_settings())
    assert result.status == "PROVIDER_UNAVAILABLE"
    assert result.verdict is None


def test_provider_error_degrades_to_provider_unavailable_verdict_none():
    provider = MockLLMProvider(raise_provider_error=True)
    result = run_prescreen("ICP", "Some Co", None, search_snippet="a real snippet", provider=provider, settings=_settings())
    assert result.status == "PROVIDER_UNAVAILABLE"
    assert result.verdict is None


def test_empty_provider_response_degrades_to_provider_unavailable_verdict_none():
    provider = MockLLMProvider(return_empty=True)
    result = run_prescreen("ICP", "Some Co", None, search_snippet="a real snippet", provider=provider, settings=_settings())
    assert result.status == "PROVIDER_UNAVAILABLE"
    assert result.verdict is None


def test_malformed_json_degrades_to_malformed_output_verdict_none():
    provider = MockLLMProvider(response_text="not json at all {{{")
    result = run_prescreen("ICP", "Some Co", None, search_snippet="a real snippet", provider=provider, settings=_settings())
    assert result.status == "MALFORMED_OUTPUT"
    assert result.verdict is None


def test_schema_invalid_output_degrades_to_schema_invalid_verdict_none():
    provider = MockLLMProvider(response_text=json.dumps({"verdict": "MAYBE", "reason": "x", "confidence": 50}))
    result = run_prescreen("ICP", "Some Co", None, search_snippet="a real snippet", provider=provider, settings=_settings())
    assert result.status == "SCHEMA_INVALID"
    assert result.verdict is None


def test_fabricated_extra_field_is_rejected_by_extra_forbid():
    provider = MockLLMProvider(
        response_text=json.dumps({"verdict": "RELEVANT", "reason": "x", "confidence": 50, "made_up_field": "hi"})
    )
    result = run_prescreen("ICP", "Some Co", None, search_snippet="a real snippet", provider=provider, settings=_settings())
    assert result.status == "SCHEMA_INVALID"
    assert result.verdict is None


def test_raw_output_schema_rejects_any_verdict_outside_the_closed_enum():
    with pytest.raises(ValidationError):
        RawDeepPrescreenOutput.model_validate({"verdict": "PASS", "reason": "x", "confidence": 50})


def test_raw_output_schema_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RawDeepPrescreenOutput.model_validate({"verdict": "RELEVANT", "reason": "x", "confidence": 50, "extra": "nope"})


# --- concurrency wrapper --------------------------------------------------


def test_run_prescreen_batch_returns_a_result_for_every_input_candidate():
    candidates = [PrescreenCandidate(batch_item_id=f"item-{i}", candidate_name=f"Co {i}", homepage_url=None, search_snippet="a snippet") for i in range(6)]
    provider = MockLLMProvider(response_text=_relevant_response())
    results = run_prescreen_batch(candidates, "ICP summary", provider, _settings(deep_prescreen_max_concurrency=2))
    assert set(results.keys()) == {c.batch_item_id for c in candidates}
    assert all(r.verdict == "RELEVANT" for r in results.values())


def test_run_prescreen_batch_never_exceeds_the_configured_concurrency():
    max_concurrent = 0
    current = 0
    lock_calls = []

    class _SlowProvider(MockLLMProvider):
        def _call(self, context):
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            time.sleep(0.05)
            current -= 1
            lock_calls.append(1)
            return super()._call(context)

    candidates = [PrescreenCandidate(batch_item_id=f"item-{i}", candidate_name=f"Co {i}", homepage_url=None, search_snippet="s") for i in range(8)]
    provider = _SlowProvider(response_text=_relevant_response())
    results = run_prescreen_batch(candidates, "ICP", provider, _settings(deep_prescreen_max_concurrency=3))
    assert len(results) == 8
    assert max_concurrent <= 3


def test_run_prescreen_batch_isolates_one_worker_failure_from_the_others():
    class _FlakyProvider(MockLLMProvider):
        def _call(self, context):
            if "Boom" in context.candidate_name:
                raise RuntimeError("simulated worker crash")
            return super()._call(context)

    candidates = [
        PrescreenCandidate(batch_item_id="ok-1", candidate_name="Fine Co", homepage_url=None, search_snippet="s"),
        PrescreenCandidate(batch_item_id="boom-1", candidate_name="Boom Co", homepage_url=None, search_snippet="s"),
        PrescreenCandidate(batch_item_id="ok-2", candidate_name="Also Fine Co", homepage_url=None, search_snippet="s"),
    ]
    provider = _FlakyProvider(response_text=_relevant_response())
    results = run_prescreen_batch(candidates, "ICP", provider, _settings())
    assert len(results) == 3  # every candidate still gets a result
    assert results["ok-1"].verdict == "RELEVANT"
    assert results["ok-2"].verdict == "RELEVANT"
    assert results["boom-1"].verdict is None  # the crash degraded to a safe no-verdict result, never propagated


def test_run_prescreen_batch_empty_input_returns_empty_dict():
    provider = MockLLMProvider(response_text=_relevant_response())
    assert run_prescreen_batch([], "ICP", provider, _settings()) == {}
