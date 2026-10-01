# Deep Discovery Mode — Implementation Plan (no code yet)

Status: **proposed, not approved for implementation**. This document is the concrete, file-by-file
build plan for the opt-in "deep" discovery mode discussed and scoped with the user. It does not change
any code. It is grounded in a full audit of the current pipeline (`app/services/batch_orchestration.py`,
`app/schemas/batch.py`, `app/services/llm_qualification.py`, `app/services/discovery_strategy.py`,
`app/services/company_resolution.py`, `app/providers/tavily.py` / `serper.py`) — file/line references
below point at the audited locations as of 2026-09-03.

## 1. Goal and non-goals

**Goal:** for ICPs where taxonomy/keyword-only discovery pulls in adjacent-but-wrong companies (agencies,
consultancies, media, recruiters), let a candidate's *actual homepage content* be judged against the
*whole ICP* by an LLM, as a relevance pre-filter — before the candidate is spent on the existing
enrichment/evidence/hard-rule/qualification pipeline.

**Non-goals (explicit):**
- Do not change hard-rule validation (`hard_rule_engine.py` / `hard_icp_validation.py`) in any way. It
  never sees a `CandidateCompany` or `BatchItemModel` directly today (only `EvidenceRecord` +
  `CanonicalICP`) — audit point 6 confirms this structural separation, and deep mode must preserve it.
- Do not let the pre-screen assign PASS/FAIL/HOLD, write evidence, or influence qualification's decision
  space. It is a *discovery-stage* filter only — same rung as "did the provider return this candidate at
  all," not "does this candidate qualify."
- Do not change FAST/SAFE/HARD behavior. Deep is additive and opt-in; the three existing modes must be
  provably byte-identical in behavior after this ships (see §10, regression tests).
- Do not introduce a 4th discovery provider or new provider capability. Homepage fetching is a new
  *internal* HTTP call, not a `ProviderAdapter`/`ProviderCapability` addition — audit point 3/8 confirms
  there is no existing precedent or need for that abstraction here.
- Do not make discovery async. Audit point 3/8 confirms the entire codebase (routes, orchestration,
  providers, LLM calls) is synchronous `httpx.post`/`httpx.get` throughout, with zero `asyncio`/
  `AsyncClient` precedent anywhere in `app/`. Bounded concurrency will use a `ThreadPoolExecutor`
  wrapping the existing synchronous call shape, not an async rewrite.

## 2. Where deep mode sits in the pipeline

Per audit point 1 and point 7, the pipeline today is strictly:

```
_run_one_discovery_round (per round)
  → run_company_discovery(...)               [raw CandidateCompany list, no domain guarantee]
  → persist DiscoveryCandidateModel rows
  → _resolve_companies(...)                   [domain-match dedup/merge — company_resolution.py]
run_batch's item loop
  → _advance_company_pipeline (per item)
      → import_evidence
      → create_validation (hard rules)         ← must never see pre-screen output
      → scoring, quality gate, enrichment, people discovery, qualification, dedup
```

**Insertion point:** a new step at the very top of `_advance_company_pipeline`
(`app/services/batch_orchestration.py`, ~line 1112), immediately before `import_evidence` (line 1144),
gated on `batch.discovery_mode == DiscoveryMode.DEEP.value`. Rationale for *after* `_resolve_companies`
rather than before:
- Resolution/dedup has already run by the time `_advance_company_pipeline` executes for any item — so a
  company sighted by both Tavily and Explorium is already merged into one `company_id` before pre-screen
  would run, meaning **one pre-screen call per unique company, never per raw sighting**. Placing it before
  resolution would waste LLM calls on duplicate sightings of the same company.
- It still runs strictly before hard validation (line 1151), satisfying "before the unchanged pipeline."

Concretely: add `_deep_prescreen_company(db, registry, item, icp) -> None` (new function, same file),
called as the first line of `_advance_company_pipeline`, wrapped in the same try/except discipline the
function already uses (audit point 1: one candidate's failure must never abort the batch). On any
pre-screen failure (fetch error, LLM error, malformed output), the function **must not raise** — it
degrades to "no pre-screen opinion," and the candidate proceeds through the unchanged pipeline exactly as
it would in Hard mode today. This mirrors `discovery_strategy.py::interpret_icp`'s own failure discipline
(audit point 4d: any failure degrades to a safe no-op, never raises, never blocks the caller).

If the pre-screen actively judges a candidate NOT a fit, the item is marked with a new stage/reason (see
§5) and **skipped from the rest of `_advance_company_pipeline`** — i.e. it does not consume an
evidence-import/hard-validation/qualification cycle. This is the entire point of the feature: cheap
homepage+LLM judgment before the expensive, multi-step downstream pipeline runs.

## 3. Schema/config changes

### 3.1 `app/schemas/batch.py`

- Add `DEEP = "deep"` to `DiscoveryMode` enum (currently FAST/SAFE/HARD, lines 32–60). Update the enum's
  docstring to describe deep mode: "runs the same provider set as Hard, plus a homepage-fetch + LLM
  relevance pre-screen before each candidate enters the qualification pipeline."
- No change to `_providers_allowed_for_mode` (`batch_orchestration.py` lines 127–146) is required if deep
  mode should use the same (unrestricted) provider set as Hard — its existing fallthrough (line 146)
  already treats any unrecognized-or-HARD-like mode as "keep all providers," so `DEEP` needs one explicit
  line added for clarity/documentation even though behavior would be identical without it. Recommend
  adding it explicitly rather than relying on fallthrough, so a future mode addition doesn't silently
  inherit deep's provider set by accident.

### 3.2 New config flags — `app/core/config.py`

Following the existing `live_test_*` / `max_discovery_rounds_per_batch` naming convention (audit point
5), add:

```python
deep_prescreen_max_concurrency: int = 4          # ThreadPoolExecutor bound
deep_prescreen_homepage_timeout_seconds: float = 8.0
deep_prescreen_homepage_max_bytes: int = 300_000  # raw response size cap before truncation
deep_prescreen_llm_timeout_seconds: float = 20.0
```

These are infrastructure bounds, not per-batch user input — no schema exposure needed beyond the
`discovery_mode` enum value itself. This keeps the opt-in surface to exactly one field, per the user's
"opt-in per batch, off by default" decision.

### 3.3 `CandidateCompany` / `BatchItemModel` — new fields

Per audit point 6, the lowest-risk approach is new nullable fields, not new abstractions:

- `app/schemas/candidate_company.py::CandidateCompany` — no change needed. This model is pre-resolution,
  transient, and already frozen; pre-screen runs *after* resolution, against the resolved company/item,
  not this object.
- `app/schemas/batch.py::BatchItemStage` (lines 63–114) — add `DEEP_PRESCREENED` between whatever stage
  represents "resolved" and `HARD_VALIDATED` (exact neighboring stage names to be confirmed by reading
  the full enum body before implementation — the audit read lines 63–114 but not verified against every
  member name).
- New nullable columns on `BatchItemModel` (`app/models/batch.py`, migration required):
  - `deep_prescreen_verdict: str | None` — one of `"RELEVANT" | "NOT_RELEVANT" | "SKIPPED_NO_HOMEPAGE" | "SKIPPED_ERROR"`
  - `deep_prescreen_reason: str | None` — short LLM-produced justification (bounded length, e.g. max 500 chars)
  - `deep_prescreen_homepage_fetched: bool | None` — whether a real homepage fetch succeeded (vs. falling back to the search snippet)
- Corresponding additive fields on `BatchItemRead` (`app/schemas/batch.py`, ~lines 155–172) so the
  frontend can eventually surface "why was this filtered" — out of scope to build the UI for in this
  phase, but the field should exist so it isn't a second migration later.

### 3.4 New Pydantic schemas — `app/schemas/deep_prescreen.py` (new file)

Mirroring `app/schemas/llm_qualification.py`'s and `app/schemas/discovery_strategy.py`'s established
anti-hallucination pattern exactly (audit point 4b):

```python
class DeepPrescreenContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    icp_summary: str            # deterministic, backend-built restatement of the whole ICP (industries,
                                 # company_types, employee_range, geography, allowed_titles, exclusions) —
                                 # never the raw free-text ICP input, to keep the LLM anchored to
                                 # normalized/canonical fields only
    candidate_name: str
    candidate_homepage_text: str        # truncated/cleaned homepage text, or the existing search snippet
    candidate_homepage_url: str | None
    content_source: Literal["homepage", "search_snippet", "none"]

class RawDeepPrescreenOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["RELEVANT", "NOT_RELEVANT"]
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=100)
```

No evidence-citation-id mechanism is needed here (unlike qualification) because this schema never makes
an evidentiary claim about a specific fact — it only classifies fit based on content already supplied to
it, exactly like `discovery_strategy.py`'s `RawDiscoveryStrategyOutput` (audit point 4b). `extra="forbid"`
and the enum-constrained `verdict` field are the anti-hallucination floor, matching both existing modules.

**Critical constraint, stated explicitly so it's not lost during implementation:** `verdict` uses the
words RELEVANT/NOT_RELEVANT, deliberately *not* PASS/FAIL/HOLD/GOOD_FIT/WEAK_FIT — different vocabulary
from both `HardRuleResult` and `QualificationDecision`, so no code path can accidentally conflate a
pre-screen verdict with a hard-rule or qualification decision by variable-naming confusion alone.

## 4. Homepage fetching

New module: `app/services/homepage_fetch.py`.

```python
def fetch_homepage(url: str, timeout_seconds: float, max_bytes: int) -> HomepageFetchResult:
    """Single synchronous httpx.get, matching the existing call shape used by
    every other outbound HTTP call in this codebase (tavily.py, serper.py,
    openai_provider.py — see audit point 8). Never raises; all failure modes
    are represented in the returned result."""
```

- **Method:** blocking `httpx.get(url, timeout=timeout_seconds, follow_redirects=True)` — same library,
  same synchronous shape as every other provider in this codebase. No new HTTP client dependency.
- **Timeout:** `deep_prescreen_homepage_timeout_seconds` (default 8s) — short, because this runs inside a
  bounded-concurrency pool and one slow site must not starve the batch (see §6).
- **Size cap:** stream-limited or post-fetch-truncated to `deep_prescreen_homepage_max_bytes` (default
  300KB raw). Enforce by reading via `response.iter_bytes()` with an early break once the cap is hit,
  rather than `response.text` on a potentially huge page.
- **Content extraction:** strip HTML to plain text (a lightweight approach — e.g. regex-strip
  `<script>`/`<style>` blocks then tags, or a minimal dependency like `readability`-style extraction if
  already vendored; audit did not find an existing HTML-text-extraction utility in this codebase, so this
  is new, minimal, dependency-light code). Truncate extracted text to a fixed character budget (e.g.
  6,000 chars) before it ever reaches the LLM prompt — bounds prompt/token cost predictably regardless of
  page size.
- **Failure modes, all non-raising, all degrading to snippet fallback:**
  - Connection error / DNS failure / TLS error → `HomepageFetchResult(success=False, reason="fetch_error")`
  - Timeout → `success=False, reason="timeout"`
  - Non-2xx status (403 bot-block, 404, 5xx) → `success=False, reason="http_<status>"`
  - 2xx but content looks like a bot-block/CAPTCHA page → reuse the pattern already proven in
    `tavily.py::_is_crunchbase_block_page` (audit-confirmed regex approach), generalized to a
    `_looks_like_block_page(text)` helper covering common phrases ("verify you are human", "access
    denied", "enable javascript and cookies", etc.) → `success=False, reason="block_page"`
  - Empty/near-empty extracted text (e.g. < 200 chars after stripping) → `success=False, reason="empty_content"`
- **Fallback on any failure:** the calling pre-screen step uses `CandidateCompany.attributes["description"]`
  (the existing Tavily/Serper search snippet, already present today) as `candidate_homepage_text`, with
  `content_source="search_snippet"`. If neither a homepage fetch nor a snippet is available,
  `content_source="none"` and the pre-screen step still runs the LLM call (an LLM judging "GreenLeaf Corp,
  no description available, ICP: X" is a legitimate low-confidence signal, not an error) — or,
  alternatively and more conservatively, `content_source="none"` skips the LLM call entirely and records
  `deep_prescreen_verdict="SKIPPED_NO_HOMEPAGE"`, letting the candidate through unfiltered rather than
  risk an LLM verdict with zero grounding. **Recommend the conservative option** — never let the LLM guess
  relevance from the company name alone, consistent with this codebase's "never guess to fill a gap"
  discipline (`hard_icp_validation.py`'s own HOLD-not-guess rule, cited in the earlier audit).
- **URL to fetch:** `CandidateCompany.domain` / the resolved company's domain (already present after
  `_resolve_companies` for Explorium-sourced or Tavily/Serper-homepage-resolved candidates). If no domain
  is present at all (resolution never found one), skip the fetch entirely and go straight to the snippet
  fallback — no new domain-guessing logic is introduced here.

## 5. LLM prompt / input / output contract

New module: `app/services/deep_prescreen.py`, structurally mirroring `llm_qualification.py`'s
`qualify_lead` shape (audit point 4):

```python
def build_prescreen_prompt(context: DeepPrescreenContext) -> str:
    return _SYSTEM_INSTRUCTIONS + "\n\n" + json.dumps(context.model_dump(), sort_keys=True)

def run_prescreen(context: DeepPrescreenContext, provider: LLMProvider) -> DeepPrescreenResult:
    """Pure orchestration, no DB access — same discipline as qualify_lead."""
```

`_SYSTEM_INSTRUCTIONS` content (paraphrased, to be finalized at implementation time, but the required
elements):
- State the full ICP summary is authoritative; the candidate's homepage content is the evidence to judge
  it against.
- Explicit instruction to reject adjacent-but-wrong business models even if they use matching industry
  keywords: agencies, consultancies, media/publishing companies, staffing/recruiting firms, marketplaces
  that merely serve the target industry rather than being a company IN it — this is the same instruction
  already proven in `llm_qualification.py`'s `_SYSTEM_INSTRUCTIONS` (audit point 4a confirms this exists
  today), reused/adapted here rather than invented fresh.
- Explicit instruction that this is a **preliminary relevance filter, not a final qualification decision**
  — ambiguous or uncertain cases should default to `RELEVANT` (false positives cost one extra downstream
  pipeline run; false negatives silently lose a real company forever, which is strictly worse). This
  asymmetry must be stated explicitly in the prompt.
- Structured-output JSON Schema mode via the same OpenAI provider mechanism already used for qualification
  and discovery-strategy (`openai_provider.py`'s per-purpose `*_RESPONSE_SCHEMA` constants, audit point
  4b/4d) — add a third `DEEP_PRESCREEN_RESPONSE_SCHEMA` constant there, following the existing pattern.

**LLM provider selection:** reuse `get_llm_provider()` (the same provider used for qualification, audit
point 4c) by default — do not introduce a third independently-configurable provider slot unless the user
wants pre-screen on a cheaper/faster model than qualification. Given cost sensitivity was explicitly
raised, recommend adding `deep_prescreen_llm_provider` as its own optional setting (defaulting to the same
value as `llm_provider` if unset) so a cheaper model can be assigned later without new code — mirroring
exactly how `discovery_strategy_llm_provider` is already an independent, optional override (audit point
4c, config.py line 76) for the exact same reason (keep an experimental/cheaper model change from affecting
qualification).

**Anti-hallucination safeguards** (matching audit point 4b's established floor):
- `RawDeepPrescreenOutput` has `extra="forbid"`.
- `verdict` is enum-constrained (`Literal["RELEVANT", "NOT_RELEVANT"]`) — cannot be any other string.
- No citation-id mechanism needed (see §3.4) since no evidentiary fact-claims are made.
- Same universal exception-to-failure conversion as `LLMProvider.qualify()` (audit point 4d) — any
  exception from the LLM call becomes a clean failure result, never propagates.
- **No retry loop**, consistent with every other LLM call site in this codebase (audit point 4d confirms
  zero retry precedent anywhere) — a single attempt, failure degrades to "no verdict" (candidate proceeds
  unfiltered), never blocks the batch.

## 6. Concurrency and spend limits

Per audit point 3/8, there is no concurrency precedent in this codebase, so this is genuinely new
infrastructure, kept as narrowly scoped as possible:

```python
# app/services/deep_prescreen.py
def run_prescreen_batch(items: list[BatchItemForPrescreen], ...) -> dict[str, DeepPrescreenResult]:
    with ThreadPoolExecutor(max_workers=settings.deep_prescreen_max_concurrency) as pool:
        futures = {pool.submit(_prescreen_one, item, ...): item.id for item in items}
        results = {}
        for future in as_completed(futures):
            results[futures[future]] = future.result()  # _prescreen_one never raises
    return results
```

- `_prescreen_one` wraps `fetch_homepage` + `run_prescreen` and is guaranteed non-raising (matches
  `_advance_company_pipeline`'s own per-item isolation discipline, audit point 1).
- Bound: `deep_prescreen_max_concurrency` (default 4) — deliberately conservative for a first ship; can be
  raised later once real-world latency/cost is observed. This is the only genuinely new tunable; everything
  else reuses existing limits.
- **Spend ceiling reuse, not reinvention:** the number of candidates entering pre-screen in any round is
  already bounded by `discovery_limit` (schema-capped 1–100, further clamped by `live_test_mode` to 25 when
  active — audit point 5) and by `max_discovery_rounds_per_batch` (hard ceiling of 5 rounds/batch,
  independent of mode). Deep mode adds **no new spend ceiling primitive** — it rides entirely on limits
  that already exist, satisfying "reuse all existing discovery_limit, live_test_mode,
  max_discovery_rounds_per_batch... controls."
- **New cost visibility (additive, not a new limit):** extend the existing `provider_call_counts`
  computed-view pattern (audit point 5, `app/api/batch.py::_provider_call_counts`) with two new derived
  counters — `deep_prescreen_homepage_fetch_count` and `deep_prescreen_llm_call_count` — computed the same
  way (traced from persisted `BatchItemModel.deep_prescreen_verdict IS NOT NULL` rows), not a live
  incrementing counter, consistent with how every other count in this codebase is derived rather than
  tracked in real time.
- **Total worst-case cost per round is bounded and predictable:** at most `discovery_limit` homepage
  fetches (≤ `deep_prescreen_homepage_timeout_seconds` each) + at most `discovery_limit` LLM calls
  (≤ `deep_prescreen_llm_timeout_seconds` each), running with concurrency ≤
  `deep_prescreen_max_concurrency` — i.e., wall-clock time is roughly
  `discovery_limit / max_concurrency * (fetch_timeout + llm_timeout)` in the worst case, and dollar cost is
  `discovery_limit * (one cheap LLM call)`, both fully deterministic from existing/new config, not open-ended.

## 7. Failure/fallback behavior (summary table)

| Failure point | Behavior |
|---|---|
| No domain resolved for candidate | Skip homepage fetch, use snippet fallback |
| Homepage fetch: timeout/DNS/TLS/connection error | Use snippet fallback |
| Homepage fetch: non-2xx status | Use snippet fallback |
| Homepage fetch: block/CAPTCHA page detected | Use snippet fallback |
| Homepage fetch: content empty after extraction | Use snippet fallback |
| No domain AND no snippet available | `verdict=SKIPPED_NO_HOMEPAGE`, candidate proceeds unfiltered (conservative default, see §4) |
| LLM call: timeout/provider error/malformed JSON/schema-invalid | `verdict=SKIPPED_ERROR`, candidate proceeds unfiltered |
| LLM call: succeeds, `verdict=RELEVANT` | Candidate proceeds through unchanged pipeline, verdict/reason persisted |
| LLM call: succeeds, `verdict=NOT_RELEVANT` | Candidate is marked filtered at this stage, `_advance_company_pipeline`'s remaining steps are skipped for this item, verdict/reason persisted for later inspection |
| Any unexpected exception anywhere in the pre-screen step | Caught at the `_deep_prescreen_company` boundary, logged, candidate proceeds unfiltered — mirrors `_process_one_company`'s existing outer try/except (audit point 1) |

The unifying rule: **every failure mode defaults to "let the candidate through," never to "silently drop
it."** A pre-screen bug or outage degrades deep mode to behave like Hard mode for the affected candidates,
never to a silent data-loss mode. This is the same posture as `discovery_strategy.py::interpret_icp`
(audit point 4d) and `hard_icp_validation.py`'s HOLD-not-guess rule — deep mode adds a new *positive*
capability without ever becoming a new way to lose a real candidate.

## 8. Duplicate / cross-provider candidate behavior

Per audit point 7, `_resolve_companies` (domain-match dedup) already runs before `_advance_company_pipeline`
for every item in every mode today. Because deep-mode pre-screen is placed *after* resolution (§2), this
means:
- A company sighted by both Explorium and Tavily, already merged into one `company_id` by the existing
  domain-match logic, gets exactly **one** pre-screen homepage fetch and **one** LLM call — never two.
- No new dedup logic is needed inside the pre-screen step itself; it operates on already-resolved
  `BatchItemModel` rows (one per unique company), inheriting the existing guarantee for free.
- If a *later* discovery round resurfaces the same already-resolved company (e.g. a second Tavily query
  matches it again), the existing "already seen" filtering (`seen_external_ids` inside providers, plus
  resolution's own trusted-identity/domain-match short-circuit, audit point 7) prevents it from reaching
  `_advance_company_pipeline` a second time at all — so it cannot be pre-screened twice across rounds
  either.

## 9. Files touched — summary

| File | Change |
|---|---|
| `app/schemas/batch.py` | Add `DiscoveryMode.DEEP`; add `BatchItemStage.DEEP_PRESCREENED`; add `deep_prescreen_*` fields to `BatchItemRead` |
| `app/core/config.py` | Add `deep_prescreen_max_concurrency`, `deep_prescreen_homepage_timeout_seconds`, `deep_prescreen_homepage_max_bytes`, `deep_prescreen_llm_timeout_seconds`, `deep_prescreen_llm_provider` (optional override) |
| `app/schemas/deep_prescreen.py` (new) | `DeepPrescreenContext`, `RawDeepPrescreenOutput`, result types |
| `app/services/homepage_fetch.py` (new) | `fetch_homepage`, block-page detection, HTML-to-text extraction |
| `app/services/deep_prescreen.py` (new) | Prompt builder, `run_prescreen`, `run_prescreen_batch` (ThreadPoolExecutor wrapper) |
| `app/services/llm_providers/openai_provider.py` | Add `DEEP_PRESCREEN_RESPONSE_SCHEMA` constant, wire structured-output mode for the new call type |
| `app/services/llm_providers/default_registry.py` | Add `get_deep_prescreen_llm_provider()` mirroring `get_discovery_strategy_llm_provider()` |
| `app/services/batch_orchestration.py` | Add `_deep_prescreen_company(...)`; call it as the first step of `_advance_company_pipeline`, gated on `discovery_mode == DEEP`; update `_providers_allowed_for_mode` with an explicit `DEEP` branch (same behavior as Hard) |
| `app/models/batch.py` | New nullable columns: `deep_prescreen_verdict`, `deep_prescreen_reason`, `deep_prescreen_homepage_fetched` |
| new Alembic migration | Add the above columns |
| `app/api/batch.py` | Extend `_provider_call_counts`-style derived-counter computation with `deep_prescreen_homepage_fetch_count` / `deep_prescreen_llm_call_count` |
| `frontend/src/schemas` / batch types (if the frontend TypeScript types mirror `BatchItemRead` 1:1) | Add the new optional fields so they round-trip cleanly; no UI required in this phase |

No changes anywhere to: `hard_rule_engine.py`, `hard_icp_validation.py`, `llm_qualification.py`,
`discovery_strategy.py`, `company_resolution.py`, `evidence_engine.py`, `app/providers/tavily.py`,
`app/providers/serper.py`, `app/providers/explorium.py` (name inferred, not directly audited this pass —
verify at implementation time), or any provider registry file.

## 10. Tests required

Following the conventions already established in `test_discovery_mode_web_search.py` and
`test_llm_qualification.py` (audit point 9):

**New test files:**
- `tests/test_homepage_fetch.py` — `respx`-mocked `httpx.get` calls covering: successful fetch + text
  extraction, timeout, non-2xx, block-page detection, oversized-content truncation, empty-content fallback.
  No live calls (matches the codebase-wide `respx` discipline).
- `tests/test_deep_prescreen.py` — mirroring `test_llm_qualification.py`'s pattern: a
  `MockLLMProvider`-style deterministic fake exercising RELEVANT / NOT_RELEVANT / malformed-output /
  timeout / provider-error branches; prompt-construction unit tests confirming the ICP summary and
  candidate content are correctly embedded; schema tests confirming `extra="forbid"` and enum constraints
  reject a fabricated `verdict` value.
- `tests/test_deep_prescreen_concurrency.py` — pure unit tests of the `ThreadPoolExecutor` wrapper using
  fake, in-process callables (no real I/O) confirming: (a) concurrency never exceeds
  `deep_prescreen_max_concurrency`, (b) one slow/hanging item never blocks results for the others, (c) an
  exception raised inside one worker never propagates out of `run_prescreen_batch` and never prevents
  other results from being collected.

**Extended existing files:**
- `tests/test_company_discovery.py` / `test_batch_orchestration.py` — add
  `test_deep_prescreen_runs_before_hard_validation_only_in_deep_mode`,
  `test_deep_prescreen_skipped_entirely_in_fast_safe_hard_modes`,
  `test_not_relevant_verdict_skips_remaining_pipeline_steps_for_that_item`,
  `test_relevant_verdict_proceeds_through_unchanged_pipeline`,
  `test_prescreen_failure_of_any_kind_lets_the_candidate_through_unfiltered` (parametrized over every
  failure mode in the §7 table).
- `tests/test_discovery_mode_web_search.py` — extend `_providers_allowed_for_mode` unit tests (already at
  lines 861–908) with `DEEP` mode cases confirming it selects the same provider set as `HARD`.

**Regression tests proving existing modes are unchanged (explicitly required by the task):**
- `test_fast_mode_batch_output_identical_before_and_after_deep_mode_exists` — run a full batch in FAST
  mode against a fixed set of mocked provider responses, assert the exact same `BatchItemModel` field
  values (stage sequence, hard-rule result, qualification decision, score) as a golden/reference result
  captured from the current (pre-deep-mode) test suite. Same for SAFE and HARD.
  Practically: this can be satisfied by asserting the new `_deep_prescreen_company` call is provably never
  invoked when `discovery_mode != DEEP` (a call-count/mock-assertion test, cheaper and more precise than a
  full golden-output diff) — e.g. patch `_deep_prescreen_company` with a `Mock()` and assert
  `mock.call_count == 0` across FAST/SAFE/HARD batch runs, and `> 0` only under DEEP.
- `test_deep_mode_with_prescreen_disabled_via_config_behaves_like_hard_mode` — a defense-in-depth test:
  even in DEEP mode, if a candidate's pre-screen result is `SKIPPED_*` for every candidate (simulating a
  total outage of the fetch/LLM layer), the batch's final accepted-lead set must be identical to what
  HARD mode would have produced from the same raw candidates — proving the "everything degrades to
  pass-through" guarantee from §7 holds end-to-end, not just at the unit level.

## 11. Rollout sequencing (suggested implementation order)

1. Schema/config additions (§3) — no behavior change, safe to land first and independently reviewed.
2. `homepage_fetch.py` + its tests — fully isolated, no orchestration wiring yet.
3. `deep_prescreen.py` (prompt/schema/LLM call) + its tests — isolated, reuses `LLMProvider` abstraction,
   no orchestration wiring yet.
4. Concurrency wrapper + its tests — isolated.
5. Wire into `batch_orchestration.py` behind the `DEEP` mode gate, with the FAST/SAFE/HARD
   never-invoked regression tests landing in the *same* change as the wiring (not after) — so the
   no-op guarantee for existing modes is proven at the moment the risk is introduced, not retrofitted.
6. Migration + `BatchItemRead`/API surface for the new fields.
7. Manual live test (small `discovery_limit`, `live_test_mode` still governing the ceiling) against one of
   the two real ICPs already used earlier in this project (Vardhan/deep-tech or MIIM) before considering
   this done.

## 12. Design risks to resolve before implementation

1. **HTML-text-extraction quality is unverified.** No existing utility in this codebase does this; a naive
   tag-stripping approach may produce noisy text (nav menus, cookie banners, footer boilerplate) that
   degrades LLM judgment quality. Recommend a brief live spike (fetch 5–10 real homepages from the
   Vardhan/MIIM test ICPs, inspect extracted text quality) before committing to the exact extraction
   method — this is a genuine unknown, not a solved problem elsewhere in the codebase.
2. **`BatchItemStage` exact neighboring member names** were not fully enumerated in the audit (only lines
   63–114 were read, not necessarily every member) — must be confirmed by reading the complete enum before
   writing the migration/stage-ordering logic, to avoid inserting `DEEP_PRESCREENED` at the wrong point in
   whatever ordering logic (`_stage_index`, referenced in audit point 9) depends on enum member order.
3. **Cost-asymmetry tuning (RELEVANT-by-default-on-uncertainty) is a judgment call, not a fact** — §5's
   recommendation to bias toward false-positives over false-negatives is a reasonable default but should
   be confirmed with the user, since it directly trades off "some agencies still slip through" against
   "never silently lose a real company," and the user may have a different risk tolerance once they see
   real pre-screen output on their two test ICPs.
4. **`deep_prescreen_llm_provider` defaulting to the same model as qualification** means deep mode's
   per-candidate cost is qualification-grade, not "cheap model" grade, on day one. If the user wants a
   materially cheaper model for pre-screening specifically (as implied by earlier cost discussion), the
   model name/provider choice should be decided before implementation, not left as a TODO — it affects the
   cost estimate given to the user when they enable deep mode.
5. **No existing dollar-cost estimator** (unlike `_estimated_explorium_credits`, audit point 5) exists for
   LLM token spend anywhere in this codebase. Deep mode is the first feature where LLM call *volume* scales
   with `discovery_limit` directly (today's qualification LLM calls only run on the much smaller
   already-hard-rule-passed subset). Recommend surfacing at minimum a call-count (§6, already planned) even
   if a dollar estimate is deferred — silent volume growth without any visible counter would be a real
   regression in spend transparency versus every other LLM-touching part of this codebase today.

## GO / NO-GO recommendation

**GO**, with the four items in §12 resolved first (items 1 and 2 are quick confirmations; items 3 and 4
are user decisions, not engineering unknowns). The design:
- Reuses every existing spend-safety primitive without adding a new unbounded cost path (§6).
- Never touches hard-rule validation, evidence, or qualification logic or their inputs (§1, confirmed
  structurally isolated per audit point 6).
- Fails safe in every identified failure mode — the system can only ever degrade toward "behaves like Hard
  mode," never toward silent candidate loss (§7).
- Is fully opt-in and additive; FAST/SAFE/HARD are provably unaffected by construction (the gate is a single
  `if discovery_mode == DEEP` at one call site) and are covered by explicit regression tests proving it (§10).
- Follows every existing convention in this codebase (Pydantic anti-hallucination schemas, `respx`-mocked
  tests, synchronous `httpx`, derived/computed counters, per-purpose LLM provider overrides) rather than
  introducing new architectural patterns beyond the one genuinely novel piece (bounded concurrency via
  `ThreadPoolExecutor`), which is itself scoped as narrowly as possible.

The main real risk is **quality**, not safety: whether homepage-text extraction is good enough to make the
LLM's judgment meaningfully better than the existing snippet-based signal. That's worth a small live spike
(§11 step 7 / §12 item 1) early, before investing in the full concurrency/orchestration wiring, so the
core hypothesis is validated cheaply before the rest of the plan is built out.
