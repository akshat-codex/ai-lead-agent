"""Phase 21 — Batch Orchestration contracts.

A batch drives the existing pipeline — discovery, resolution, enrichment,
people discovery/resolution, evidence, hard validation, business-model/
signals, scoring, LLM qualification, adversarial review, verification,
deduplication — for many candidate companies under one ICP/version. It
never reimplements any of those stages: app/services/batch_orchestration.py
calls each phase's existing service/API function directly, in-process.

A batch is a coordination record, not a second source of truth: canonical
companies/people/leads/evidence/scores/etc. all remain exactly what
Phases 6-20 already produce. This module only tracks, per candidate,
which stage it has reached and what its terminal outcome was.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.discovery import ProviderOutcomeRead


class BatchStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"


class DiscoveryMode(str, Enum):
    """Which COMPANY_DISCOVERY provider(s) a batch actually calls each
    round — a per-batch, create-time choice (like discovery_limit),
    reused unchanged on every later "Find More"/resume round for the same
    batch. See app/services/batch_orchestration.py::_run_one_discovery_round
    for the exact filtering this drives.

    FAST — Explorium only. This is today's pre-existing, unchanged
    behavior — precise (structured taxonomy match) but limited to
    industry terms Explorium's own linkedin_category/naics_category
    taxonomy actually has (confirmed live: broad/novel terms like "FMCG",
    "OTT Platforms", "Microdrama Companies" resolve to zero Explorium
    categories — see app/providers/explorium.py's own module docstring).

    SAFE — Tavily + Serper only, Explorium skipped entirely. A genuine
    open-web search (see app/providers/tavily.py / serper.py), for ICPs
    whose industries Explorium's taxonomy can't cover at all.

    HARD — every registered COMPANY_DISCOVERY provider (Explorium,
    Tavily, Serper) runs every round. The existing, fully generic
    domain-match company-resolution path (app/services/company_resolution.py)
    and cross-provider evidence merge (app/services/evidence_import.py::
    collect_company_evidence) already combine independent providers'
    sightings of the SAME real company into one canonical company with
    corroborating evidence — no new merge logic was needed for this mode.

    DEEP — the SAME provider set as HARD (every registered
    COMPANY_DISCOVERY provider), PLUS one additional step per resolved
    candidate: a homepage fetch (app/services/homepage_fetch.py) and a
    bounded-concurrency LLM relevance pre-screen
    (app/services/deep_prescreen.py) run after company resolution and
    before evidence import/hard-rule validation (see
    app/services/batch_orchestration.py::_run_deep_prescreen_for_items,
    whose result _advance_company_pipeline checks first). This is a
    discovery-stage relevance filter/rank signal ONLY — it can never
    produce or influence a hard-rule PASS/FAIL/HOLD or a qualification
    decision, and every failure mode
    (no homepage, fetch/LLM error, malformed output) degrades to letting
    the candidate through unfiltered, exactly like HARD mode would have
    handled it. Opt-in; every other mode's behavior is completely
    unaffected by this mode's existence."""

    FAST = "fast"
    SAFE = "safe"
    HARD = "hard"
    DEEP = "deep"


class BatchItemStage(str, Enum):
    """How far this candidate has progressed through the pipeline — a
    coarse checkpoint used to make retry/resume safe: a stage already
    reached is never repeated, so resuming a batch never re-runs discovery
    for a candidate that already has a company_id, never re-collects
    evidence it already has, and never re-deduplicates a lead that already
    has one.

    Phase 30: PEOPLE_DISCOVERED/PERSON_RESOLVED were moved to AFTER
    COMPANY_QUALITY_SCORED (previously they ran right after ENRICHED,
    before hard validation ever ran) — see
    app/services/batch_orchestration.py::_advance_company_pipeline's own
    comment for why: Unipile must only ever be called for a company that
    has already cleared deterministic hard-rule validation, never for one
    that will turn out to hard-FAIL. The enum member NAMES are unchanged
    (so any persisted stage string from before this phase still parses),
    only their ordinal position in this class changed, which is exactly
    what _stage_index/_at_least use to gate resume-skipping — a batch item
    already at PERSON_RESOLVED from before this phase still correctly
    skips people-discovery again on resume.

    Phase 32: ENRICHED was moved the same way, for the same reason — a
    real discovery provider (Explorium) already supplies every evidence
    field the hard-rule engine reads (industry, employee_range/count,
    country — see app/services/evidence_import.py::collect_company_evidence's
    own DiscoveryCandidateModel-derived fields) directly on the discovery
    candidate, so enrichment was never actually required for hard
    validation to run at all. Running it unconditionally, before
    validation, meant every discovered company — including ones that
    would immediately hard-FAIL a basic industry/geography/size check —
    triggered a real enrichment provider call. ENRICHED now sits alongside
    PEOPLE_DISCOVERED/PERSON_RESOLVED, gated by the exact same
    "not REJECT" company-quality check, so enrichment (like person
    discovery) is only ever spent on a company that survived deterministic
    validation. Discovery (Step 1: find + validate real companies against
    the ICP) and enrichment (a later step) are now genuinely separate
    stages, matching the product's own required architecture.

    Deep mode: DEEP_PRESCREENED sits between COMPANY_RESOLVED and
    EVIDENCE_COLLECTED — the pre-screen (app/services/deep_prescreen.py)
    runs on an already-resolved company (one pre-screen per unique company,
    never per raw provider sighting) and strictly before any evidence/
    hard-rule work is ever spent on it. Every OTHER discovery mode
    (fast/safe/hard) never touches this stage at all: a non-DEEP batch's
    items go straight from COMPANY_RESOLVED to EVIDENCE_COLLECTED exactly
    as before this stage existed, so inserting a new enum member here
    changes _stage_index's computed ORDINALS for every stage after it, but
    never changes which stages a non-DEEP item actually passes through."""

    DISCOVERED = "DISCOVERED"
    COMPANY_RESOLVED = "COMPANY_RESOLVED"
    DEEP_PRESCREENED = "DEEP_PRESCREENED"
    EVIDENCE_COLLECTED = "EVIDENCE_COLLECTED"
    HARD_VALIDATED = "HARD_VALIDATED"
    CLASSIFIED = "CLASSIFIED"
    SCORED = "SCORED"
    COMPANY_QUALITY_SCORED = "COMPANY_QUALITY_SCORED"
    ENRICHED = "ENRICHED"
    PEOPLE_DISCOVERED = "PEOPLE_DISCOVERED"
    PERSON_RESOLVED = "PERSON_RESOLVED"
    QUALIFIED = "QUALIFIED"
    ADVERSARIALLY_REVIEWED = "ADVERSARIALLY_REVIEWED"
    DEDUPLICATED = "DEDUPLICATED"
    DONE = "DONE"


class BatchItemOutcome(str, Enum):
    """The terminal classification for one candidate — what the batch's
    accepted/held/rejected/duplicate/failed counts are computed from.
    Never fabricated: a candidate only reaches ACCEPTED/HELD/REJECTED via
    the real Phase 12 hard-rule result (a HARD FAIL always yields
    REJECTED, never ACCEPTED — see app/services/batch_orchestration.py),
    only reaches DUPLICATE via the real Phase 19 dedup decision, and only
    reaches FAILED when a pipeline call itself raised or returned an
    execution failure."""

    ACCEPTED = "ACCEPTED"
    HELD = "HELD"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    target_count: int = Field(ge=1, le=1000)
    discovery_limit: int = Field(default=20, ge=1, le=100)
    people_limit_per_company: int = Field(default=5, ge=1, le=100)
    discovery_mode: DiscoveryMode = Field(default=DiscoveryMode.FAST)


class BatchResumeRequest(BaseModel):
    """Optional body for POST /batches/{id}/resume. Omitting the body
    entirely (today's exact existing call shape) is equivalent to passing
    max_discovery_rounds=1 with the default — a plain resume never seeds
    more than one additional discovery round unless explicitly asked."""

    model_config = ConfigDict(extra="forbid")

    max_discovery_rounds: int = Field(default=1, ge=1, le=10)


class BatchItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    batch_id: str
    icp_id: str
    icp_version: int
    source_candidate_id: str
    company_id: str | None
    person_id: str | None
    lead_id: str | None
    stage: str
    outcome: str | None
    error_message: str | None
    hard_rule_result: str | None
    qualification_decision: str | None
    deep_prescreen_verdict: str | None = None
    deep_prescreen_reason: str | None = None
    deep_prescreen_homepage_fetched: bool | None = None
    deep_prescreen_status: str | None = None
    # Deep mode only (see DiscoveryMode.DEEP) — always None for every item
    # from a fast/safe/hard batch, since _run_deep_prescreen_for_items
    # (app/services/batch_orchestration.py) is a no-op for one.
    # deep_prescreen_verdict is "RELEVANT" | "NOT_RELEVANT" | None (None
    # covers every degraded/skipped case); deep_prescreen_status is the raw
    # DeepPrescreenResult.status verbatim — see
    # app/schemas/deep_prescreen.py::DeepPrescreenResult's own docstring;
    # never any hard-rule or qualification vocabulary.
    created_at: datetime
    updated_at: datetime


class BatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    requested_target_count: int
    discovery_limit: int
    people_limit_per_company: int
    discovery_mode: str = DiscoveryMode.FAST.value
    status: str
    discovered_count: int
    deduplicated_lead_count: int
    accepted_count: int
    held_count: int
    rejected_count: int
    duplicate_count: int
    failed_count: int
    started_at: datetime
    completed_at: datetime | None
    discovery_exhausted_providers: list[str] = Field(default_factory=list)
    discovery_error_code: str | None = None
    discovery_error_message: str | None = None
    discovery_rounds_run: int = 0
    discovery_pool_exhausted: bool = False
    discovery_strategy: dict | None = None
    # Phase 10 continuation: the cached DiscoveryStrategy (see
    # app/schemas/discovery_strategy.py), exposed read-only for
    # audit/UI — e.g. surfacing strategy["unsupported_intent"] or
    # strategy["reasoning"] to the user. Never written back through this
    # schema; only app/services/batch_orchestration.py ever sets it.
    hermes_job_id: str | None = None
    hermes_job_status: str | None = None
    # Phase 34: read-only visibility into Hermes's async secondary-discovery
    # job (see app/models/batch.py's own fields and
    # app/services/batch_orchestration.py::_maybe_advance_hermes_job) — a
    # non-null hermes_job_id with a non-terminal hermes_job_status (queued/
    # running) tells the caller a job is still in flight and a LATER resume
    # call may pick up new records; never written back through this schema.

    @field_validator("discovery_exhausted_providers", mode="before")
    @classmethod
    def _default_none_to_empty_list(cls, value: list[str] | None) -> list[str]:
        # The ORM column is nullable (older/never-touched rows genuinely
        # have NULL here) — normalize that to the schema's empty-list
        # default rather than letting pydantic reject a None value.
        return value if value is not None else []


class BatchDetailRead(BatchRead):
    items: list[BatchItemRead]
    # Phase 4 (AI/UX + live-safety audit) — the raw provider identity for
    # every item already exists (BatchItemModel.source_candidate_id ->
    # DiscoveryCandidateModel.provider_id), but nothing surfaced it as a
    # single, obvious signal: a batch run entirely on
    # MockCompanyDataProvider/MockPeopleDataProvider (e.g. EXPLORIUM_API_KEY
    # unset) returns a response structurally identical to a real one —
    # same fields, same shape, no indication the "leads" are fabricated
    # (see app/providers/mocks.py's own module docstring: "Example Test
    # Co"/"Sample Widgets Inc"). Computed once, in app/api/batch.py::
    # _to_detail, from each item's own already-loaded DiscoveryCandidateModel
    # row — never a new DB column, never a redesign of discovery/batch
    # orchestration itself.
    used_mock_company_data: bool = False
    provider_call_counts: dict[str, int] = Field(default_factory=dict)
    # A simple, honest count of real provider calls this batch made,
    # keyed by provider_id — e.g. {"explorium-company-discovery-v1": 2,
    # "tavily-company-discovery-v1": 1, "gemini-llm-v1": 1}. Deliberately
    # NOT a dollar/credit/token estimate: none of Explorium/Tavily/Serper/
    # Gemini's real per-call pricing is tracked anywhere in this codebase
    # (see RequestCost on app/providers/contracts.py::ProviderResponse,
    # which exists but nothing populates), so a fabricated cost number
    # would be a guess, not a fact — this surfaces only what's already
    # verifiably true: how many real calls were made to which provider.
    # Computed in app/api/batch.py::_to_detail from the SAME
    # provider_call_outcomes data (COMPANY_DISCOVERY) plus
    # discovery_strategy.provider_id (the one LLM interpretation call,
    # cached and never repeated per batch — see BatchModel.discovery_strategy's
    # own docstring) — never a new counter tracked separately, so it can
    # never drift from what actually happened.
    estimated_explorium_credits: float | None = None
    # A rough ESTIMATE only, never an authoritative figure: Explorium
    # exposes no API endpoint to check real credit consumption (confirmed
    # against Explorium's own docs — see app/core/config.py::Settings.
    # explorium_estimated_credits_per_company's own comment) — this is
    # (real companies Explorium returned this batch) *
    # settings.explorium_estimated_credits_per_company, a user-configured
    # observed rate, not a vendor-confirmed one. None when Explorium made
    # no calls this batch (never a fabricated 0 that could be misread as
    # "confirmed zero cost").
    # Phase 5 (benchmark + live-readiness audit) — root cause: the raw
    # per-provider call data (provider_id, success, requested vs.
    # returned, latency_ms) already existed on DiscoveryRunModel, but
    # DiscoveryRunModel carries no batch_id (a discovery run is scoped to
    # an ICP, shared across batches — see app/services/hard_icp_validation.py's
    # own P2-fix comment on cross-round provenance for the identical
    # "not batch-scoped" fact about DiscoveryCandidateModel) and there was
    # no "list discovery runs for a batch" endpoint at all — a caller
    # could not answer "how many real provider calls did this batch make,
    # and how long did they take" without already knowing the discovery
    # run ids, which were never exposed here either. Computed once, in
    # app/api/batch.py::_to_detail, by tracing this batch's own items ->
    # their source DiscoveryCandidateModel rows -> the DISTINCT
    # DiscoveryRunModel rows those rows point to — never a new DB column,
    # never a redesign of discovery/batch orchestration itself. Deduplicated
    # by discovery run: a round that queried 3 branches in ONE
    # run_company_discovery call already merges those 3 branches' outcomes
    # into ONE DiscoveryRunModel.provider_outcomes list (see
    # app/services/batch_orchestration.py's own run_row construction), so
    # this list has one entry per (discovery round, provider) pair actually
    # attempted for this batch, not per company.
    provider_call_outcomes: list["ProviderOutcomeRead"] = Field(default_factory=list)

    deep_prescreen_homepage_fetch_count: int = 0
    deep_prescreen_llm_call_count: int = 0
    # Deep mode only — always 0 for a fast/safe/hard batch
    # (_run_deep_prescreen_for_items is a no-op for every other mode). Both
    # are DERIVED counts,
    # computed in app/api/batch.py::_to_detail from this batch's own items'
    # persisted deep_prescreen_* columns — never a live incrementing
    # counter, the same discipline provider_call_counts above already
    # uses, so this can never drift from what actually happened.
    # homepage_fetch_count counts items with deep_prescreen_homepage_fetched
    # True; llm_call_count counts items whose deep_prescreen_verdict is not
    # None (a real LLM response was parsed, success or not) OR whose stage
    # reached DEEP_PRESCREENED with a recorded error status — i.e. every
    # item deep mode actually attempted to judge, regardless of outcome.
    # This is the ONE new cost-visibility signal deep mode adds (see the
    # deep-mode implementation plan's own §12 design-risk note: no LLM
    # token/dollar ledger exists anywhere in this codebase yet — this is a
    # call-COUNT only, exactly like provider_call_counts above, never a
    # dollar estimate).
