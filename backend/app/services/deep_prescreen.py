"""Deep discovery mode — LLM relevance pre-screen orchestration.

Runs AFTER a discovery candidate's company has already been resolved
(app/services/company_resolution.py) and BEFORE evidence import / hard-rule
validation ever runs for it (see app/services/batch_orchestration.py::
_advance_company_pipeline, the one call site) — a discovery-stage relevance
filter/rank signal only. It never produces, and can never be mistaken for,
a hard-rule PASS/FAIL/HOLD (app/schemas/hard_rule_result.py) or a
qualification decision (app/schemas/llm_qualification.py): see
app/schemas/deep_prescreen.py's own docstring for the deliberate vocabulary
separation (RELEVANT/NOT_RELEVANT, never PASS/FAIL/HOLD/GOOD_FIT/etc).

FAIL-SAFE, NEVER FAIL-CLOSED: every failure mode here — no homepage domain,
a failed/blocked/timed-out fetch with no usable snippet fallback either, a
provider error, malformed JSON, a schema violation — degrades to
DeepPrescreenResult(verdict=None, status=<specific code>). The ONE caller
(_deep_prescreen_company in batch_orchestration.py) treats verdict=None
exactly like verdict="RELEVANT": the candidate proceeds through the
unchanged pipeline. This mirrors app/services/discovery_strategy.py::
interpret_icp's own "a failure never blocks the caller" discipline, and
app/services/hard_icp_validation.py's own "never guess a value to fill a
gap" discipline applied to its opposite: here, an uncertain case is never
guessed into an exclusion, only ever into "let it through."

Pure orchestration below `run_prescreen`: no database access, mirroring
app/services/llm_qualification.py::qualify_lead / discovery_strategy.py::
interpret_icp's own "pure orchestration" contract. Homepage fetching
(app/services/homepage_fetch.py) and concurrency (run_prescreen_batch,
below) are the only I/O this module performs itself.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.deep_prescreen import DeepPrescreenContext, DeepPrescreenResult, RawDeepPrescreenOutput
from app.services.homepage_fetch import fetch_homepage
from app.services.llm_providers.base import LLMProvider

PROMPT_VERSION = "deep-prescreen-v1"

_SYSTEM_INSTRUCTIONS = (
    "You are a discovery-stage relevance pre-screen for a company-prospecting pipeline. You are given an ICP "
    "(Ideal Customer Profile) summary and one candidate company's own real website content (or, when a real "
    "fetch was not available, a short third-party search snippet clearly labeled as such). Judge ONLY whether "
    "this candidate is PLAUSIBLY a genuine match for the WHOLE ICP — not just one keyword or category.\n\n"
    "Reject candidates whose actual business model is adjacent-but-wrong even if they use matching industry "
    "keywords: marketing/creative/ad AGENCIES, CONSULTANCIES, staffing/recruiting firms, media/publishing "
    "companies, or marketplaces/tool vendors that merely SERVE the target industry rather than BEING a company "
    "in it. A company that sells software/services TO the ICP's target industry is not itself a company IN "
    "that industry.\n\n"
    "This is a PRELIMINARY filter, not a final decision — a real, evidence-based validation pipeline runs "
    "AFTER you, on every candidate you mark RELEVANT. Because of this asymmetry: a wrong RELEVANT verdict "
    "costs one extra (cheap) pipeline pass that the real validation will still catch; a wrong NOT_RELEVANT "
    "verdict permanently loses a real candidate this system will never reconsider. When genuinely uncertain, "
    "or when the given content is too thin/generic to judge confidently, you MUST answer RELEVANT — never "
    "guess NOT_RELEVANT from ambiguous or insufficient information.\n\n"
    "Respond with strict JSON matching the required schema only."
)


def build_icp_summary(icp: CanonicalICP) -> str:
    """A deterministic, backend-built restatement of the WHOLE canonical
    ICP — the same field selection app/services/discovery_strategy.py::
    build_discovery_strategy_request already uses (industries, geography,
    employee_range, company_types, exclusions), plus allowed_titles (a
    genuine discovery-relevant signal for "who this company hires" —
    already carried into discovery queries themselves, see
    app/schemas/candidate_company.py::CompanyDiscoveryQuery.allowed_titles's
    own docstring). Never the ICP's raw free-text input (there is none —
    see DiscoveryStrategyRequest's own docstring: ICPCreate is already
    structured) and never re-derives normalization — selects and compresses
    only, exactly like build_discovery_strategy_request itself."""
    hard = icp.hard_rules
    lines: list[str] = []
    if hard.industries:
        lines.append(f"Industries: {', '.join(hard.industries)}")
    if hard.company_types:
        lines.append(f"Company types: {', '.join(hard.company_types)}")
    geography_terms = tuple(entry.label for entry in hard.geography.countries) + hard.geography.unrecognized
    if geography_terms:
        lines.append(f"Geography: {', '.join(geography_terms)}")
    if hard.employee_range.min is not None or hard.employee_range.max is not None:
        lo = hard.employee_range.min if hard.employee_range.min is not None else "0"
        hi = hard.employee_range.max if hard.employee_range.max is not None else "unbounded"
        lines.append(f"Employee count: {lo}-{hi}")
    if hard.allowed_titles:
        lines.append(f"Target decision-maker titles (who this company should employ): {', '.join(hard.allowed_titles)}")
    if hard.exclusions:
        lines.append(f"Explicitly excluded: {', '.join(hard.exclusions)}")
    return "\n".join(lines) if lines else "No specific hard-rule criteria were provided."


def build_prescreen_prompt(context: DeepPrescreenContext) -> str:
    """A compact, deterministic textual rendering of the context — the
    same context always renders to the same prompt string, mirroring
    app/services/discovery_strategy.py::build_discovery_strategy_prompt /
    app/services/llm_qualification.py::build_prompt."""
    payload = {
        "icp_summary": context.icp_summary,
        "candidate_name": context.candidate_name,
        "candidate_content_source": context.content_source,
        "candidate_homepage_url": context.candidate_homepage_url,
        "candidate_content": context.candidate_homepage_text,
    }
    return _SYSTEM_INSTRUCTIONS + "\n\n" + json.dumps(payload, sort_keys=True)


def _degraded_result(status: str, content_source: str, homepage_fetched: bool, error_message: str) -> DeepPrescreenResult:
    return DeepPrescreenResult(
        status=status, content_source=content_source, homepage_fetched=homepage_fetched, error_message=error_message
    )


def run_prescreen(
    icp_summary: str,
    candidate_name: str,
    homepage_url: str | None,
    search_snippet: str,
    provider: LLMProvider,
    settings: Settings,
) -> DeepPrescreenResult:
    """One candidate, one homepage fetch (best-effort) + at most one LLM
    call. Never raises.

    Content-source resolution, in order:
      1. A real homepage fetch (see app/services/homepage_fetch.py) if
         `homepage_url` is set and the fetch succeeds -> content_source="homepage".
      2. The existing discovery search snippet
         (CandidateCompany.attributes["description"], already produced by
         Tavily/Serper/Explorium today) if non-empty -> content_source="search_snippet".
      3. Neither available -> status="SKIPPED_NO_CONTENT", verdict=None. The
         LLM is deliberately never called with only a bare company name and
         no content: that would mean guessing relevance from a name alone,
         which this codebase's existing "never guess to fill a gap"
         discipline (app/services/hard_icp_validation.py) forbids applying
         here too."""
    homepage_fetched = False
    content_source = "none"
    content = ""

    if homepage_url:
        fetch_result = fetch_homepage(
            homepage_url,
            timeout_seconds=settings.deep_prescreen_homepage_timeout_seconds,
            max_bytes=settings.deep_prescreen_homepage_max_bytes,
            max_chars=settings.deep_prescreen_homepage_max_chars,
        )
        if fetch_result.success:
            homepage_fetched = True
            content_source = "homepage"
            content = fetch_result.text

    if not content and search_snippet.strip():
        content_source = "search_snippet"
        content = search_snippet.strip()

    if not content:
        return _degraded_result("SKIPPED_NO_CONTENT", "none", homepage_fetched, "no homepage content or search snippet was available")

    context = DeepPrescreenContext(
        icp_summary=icp_summary,
        candidate_name=candidate_name,
        candidate_homepage_text=content,
        candidate_homepage_url=homepage_url,
        content_source=content_source,
    )
    response = provider.qualify(context)

    if not response.success:
        error = response.error
        message = error.message if error else "unknown provider failure"
        return _degraded_result("PROVIDER_UNAVAILABLE", content_source, homepage_fetched, message)

    if not response.raw_text or not response.raw_text.strip():
        return _degraded_result("PROVIDER_UNAVAILABLE", content_source, homepage_fetched, "provider returned an empty response")

    try:
        parsed_json = json.loads(response.raw_text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _degraded_result("MALFORMED_OUTPUT", content_source, homepage_fetched, f"could not parse JSON: {exc}")

    try:
        output = RawDeepPrescreenOutput.model_validate(parsed_json)
    except ValidationError as exc:
        return _degraded_result("SCHEMA_INVALID", content_source, homepage_fetched, f"schema validation failed: {exc}")

    return DeepPrescreenResult(
        status="SUCCESS",
        verdict=output.verdict,
        reason=output.reason,
        confidence=output.confidence,
        content_source=content_source,
        homepage_fetched=homepage_fetched,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
    )


@dataclass(frozen=True)
class PrescreenCandidate:
    """One candidate's inputs for a batch pre-screen run — deliberately a
    plain dataclass, not a pydantic model: this never crosses an API
    boundary, it only threads a batch_item_id through the ThreadPoolExecutor
    call below and back."""

    batch_item_id: str
    candidate_name: str
    homepage_url: str | None
    search_snippet: str


def run_prescreen_batch(
    candidates: list[PrescreenCandidate],
    icp_summary: str,
    provider: LLMProvider,
    settings: Settings,
) -> dict[str, DeepPrescreenResult]:
    """Bounded-concurrency wrapper around run_prescreen — the ONE genuinely
    new concurrency primitive in this codebase (no asyncio/Semaphore/
    ThreadPoolExecutor precedent existed anywhere in app/ before this; see
    the deep-mode implementation plan's own audit). A ThreadPoolExecutor
    bounding blocking httpx/LLMProvider calls, not an async rewrite —
    every other call site in this codebase stays exactly as synchronous as
    it always was; only this one, self-contained boundary uses threads.

    Bounded by settings.deep_prescreen_max_concurrency (default 4). One
    slow/hanging candidate can never block collection of the others'
    results (each future is independent; as_completed yields whichever
    finishes first). run_prescreen itself never raises, but this function
    also guards each future.result() defensively — a worker-thread
    exception of any kind becomes a SKIPPED_ERROR result for that one
    candidate only, never propagates, and never prevents the other
    candidates' results from being collected.

    Returns {batch_item_id: DeepPrescreenResult} — every input candidate is
    guaranteed a result (never a silently missing key)."""
    if not candidates:
        return {}

    results: dict[str, DeepPrescreenResult] = {}
    max_workers = max(1, min(settings.deep_prescreen_max_concurrency, len(candidates)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_id = {
            pool.submit(
                run_prescreen, icp_summary, candidate.candidate_name, candidate.homepage_url, candidate.search_snippet, provider, settings
            ): candidate.batch_item_id
            for candidate in candidates
        }
        for future in as_completed(future_to_id):
            batch_item_id = future_to_id[future]
            try:
                results[batch_item_id] = future.result()
            except Exception as exc:  # a worker-thread failure must never abort the batch or drop a result
                logging.getLogger(__name__).warning("deep_prescreen_worker_failed batch_item_id=%r error=%s", batch_item_id, exc)
                results[batch_item_id] = _degraded_result("PROVIDER_UNAVAILABLE", "none", False, f"unexpected pre-screen error: {exc}")
    return results
