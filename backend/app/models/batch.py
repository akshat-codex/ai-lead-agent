"""Phase 21 — batch orchestration persistence.

BatchModel is a single, updatable coordination record — deliberately NOT
append-only, unlike every audit-trail model in Phases 6-20: a batch is a
live run whose status/counts change as it progresses (PENDING -> RUNNING
-> COMPLETED[_WITH_ERRORS]), and re-running/resuming the same batch id
updates the SAME row rather than creating a new one. This does not violate
any earlier phase's append-only rule — those tables (evidence, resolutions,
scores, qualifications, reviews, ...) are untouched by this file and remain
exactly as append-only as before; only the NEW batch-coordination state
introduced by this phase is mutable, because it describes a currently-in-
progress run rather than a permanent decision or observation.

BatchItemModel is one row per discovered candidate the batch is tracking.
It IS updated in place as the candidate advances through pipeline stages
(see BatchItemStage) — again, this is new coordination bookkeeping, not a
rewrite of any Phase 6-20 audit trail; the actual discovery candidate,
resolution, evidence, score, qualification, etc. rows those phases create
are never touched or duplicated here. uq_batch_item_candidate keeps
re-processing the same discovery candidate within the same batch safe: a
second attempt updates the existing item row rather than creating a
duplicate tracking record.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class BatchModel(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)

    requested_target_count: Mapped[int] = mapped_column(Integer)
    discovery_limit: Mapped[int] = mapped_column(Integer)
    people_limit_per_company: Mapped[int] = mapped_column(Integer)
    discovery_mode: Mapped[str] = mapped_column(String(16), default="fast", server_default="fast")
    # Which COMPANY_DISCOVERY provider(s) this batch calls each round — see
    # app/schemas/batch.py::DiscoveryMode. Fixed at batch creation, reused
    # unchanged on every later resume/"Find More" round, exactly like
    # discovery_limit/people_limit_per_company above. server_default keeps
    # existing rows (created before this column existed) valid as "fast" —
    # i.e. exactly today's pre-existing Explorium-only behavior — without
    # a backfill migration.

    status: Mapped[str] = mapped_column(String(32), default="PENDING")

    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    deduplicated_lead_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    held_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- progressive discovery / "Find More Leads" continuation state ---
    # Persisted server-side (not held only in a request or the frontend) so
    # a second/third/... discovery round always continues from exactly
    # where the last one left off, survives a page refresh, and never
    # restarts a real provider query from page 1.
    discovery_cursors: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # One opaque continuation token PER PROVIDER, keyed by provider_id, e.g.
    # {"explorium-company-discovery-v1": "..."} — a dict, not a single
    # string, because every registered COMPANY_DISCOVERY provider is called
    # each round (see app/providers/default_registry.py: mock + Explorium
    # can both be registered simultaneously); a flat field would silently
    # lose or misapply one provider's continuation state. Absent/empty
    # means "first page" for that provider.
    discovery_exhausted_providers: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # provider_ids that have reported ProviderResponse.exhausted=True for
    # this ICP query — never asked again on subsequent rounds.
    discovery_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # The most recent non-retryable provider error code from the LAST
    # discovery round attempt (e.g. EXPLORIUM_CREDITS_EXHAUSTED,
    # EXPLORIUM_AUTH_FAILED). Cleared at the START of each new round
    # attempt, so a since-resolved failure never permanently blocks future
    # rounds. Distinct from discovery_exhausted_providers: an error means
    # something went wrong; exhaustion means the provider legitimately has
    # nothing more — never conflate the two.
    discovery_error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True, default=None)
    # Phase 7I — the actual ProviderError.message (e.g. the real exception
    # text from a failed Explorium HTTP call) alongside discovery_error_code.
    # Before this field, only the machine code (e.g. "PROVIDER_ERROR") was
    # ever persisted — the underlying reason was computed in-memory by
    # ProviderAdapter.run() (see app/providers/base.py) and then silently
    # discarded, making a completed-but-empty batch undiagnosable after the
    # fact (confirmed: the Phase 7H live test produced discovered_count=0,
    # discovery_error_code="PROVIDER_ERROR", and no way to determine why).
    # Same 2000-char cap and same "cleared at the start of each new round
    # attempt" lifecycle as discovery_error_code, kept in lockstep with it
    # for the same reason (a since-resolved failure must not permanently
    # block future rounds, and a message from an old failure must never be
    # displayed alongside a current success).
    discovery_rounds_run: Mapped[int] = mapped_column(Integer, default=0)
    # How many discovery rounds have actually executed for this batch
    # (across create + every resume call) — observability only.
    discovery_pool_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    # True only once EVERY registered COMPANY_DISCOVERY provider is in
    # discovery_exhausted_providers. Computed and stored server-side so a
    # caller never has to know which/how many providers are registered.

    # --- Phase 10 continuation: AI discovery strategy, computed ONCE ---
    # A serialized app.schemas.discovery_strategy.DiscoveryStrategy
    # (model_dump(mode="json")), same JSON-column pattern as
    # discovery_cursors above. Set on the FIRST discovery round this batch
    # ever runs (see app/services/batch_orchestration.py::
    # _get_or_build_discovery_strategy) and reused, never recomputed, on
    # every later round/"Find More" call — this is what keeps the LLM call
    # to exactly one per batch regardless of how many discovery rounds run.
    # A failed/degraded interpretation (status != SUCCESS) is cached here
    # too, not just a successful one: without that, a persistently failing
    # OpenAI call would otherwise be retried every single round, which is
    # both wasteful and unnecessary — discovery already degrades safely to
    # the ICP's own unexpanded terms whether the strategy was never
    # attempted or was attempted and failed; caching the failure just
    # avoids repeating a call already known not to help this batch.
    discovery_strategy: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Phase 34: Hermes secondary discovery job state ---
    # Hermes (app/providers/hermes.py) is an ASYNC, minutes-long research
    # job, not a synchronous provider call — its state must survive across
    # multiple separate HTTP requests (submit on one resume call, check on
    # a later one; see app/services/batch_orchestration.py::
    # _maybe_advance_hermes_job). hermes_job_id is None whenever no Hermes
    # job has ever been submitted for this batch, OR the last one already
    # reached a terminal state and its records (if any) were already
    # imported — a fresh None means "safe to submit a new one if still
    # short," never "a job is silently still running."
    hermes_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    hermes_job_status: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # The vendor's own status string (queued/running/completed/failed/
    # timeout/rate_limited/cancelled) verbatim — never re-interpreted into
    # a different vocabulary, so a status value here always means exactly
    # what app/providers/hermes.py's own docstring documents.
    hermes_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class BatchItemModel(Base):
    __tablename__ = "batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "source_candidate_id", name="uq_batch_item_candidate"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)

    source_candidate_id: Mapped[str] = mapped_column(String(36), index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    stage: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    hard_rule_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    qualification_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)

    deep_prescreen_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    deep_prescreen_reason: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
    deep_prescreen_homepage_fetched: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    deep_prescreen_status: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # Deep discovery mode only (see app/schemas/batch.py::DiscoveryMode.DEEP
    # / BatchItemStage.DEEP_PRESCREENED) — set only by
    # app/services/batch_orchestration.py::_run_deep_prescreen_for_items,
    # never for a fast/safe/hard batch item, which leaves all four NULL
    # exactly as they always were before this feature existed.
    # deep_prescreen_verdict is "RELEVANT" | "NOT_RELEVANT" | None (None
    # covers every degraded/skipped pre-screen outcome). deep_prescreen_status
    # is the raw DeepPrescreenResult.status verbatim ("SUCCESS" |
    # "SKIPPED_NO_CONTENT" | "PROVIDER_UNAVAILABLE" | "MALFORMED_OUTPUT" |
    # "SCHEMA_INVALID" — see app/schemas/deep_prescreen.py::
    # DeepPrescreenResult's own docstring) — kept so a real LLM-call attempt
    # (any status except SKIPPED_NO_CONTENT, which never calls the provider
    # at all) is honestly distinguishable from "no content was ever
    # available to judge," matching this codebase's own "failure is always
    # recorded, never silently collapsed" discipline (e.g. DiscoveryStrategy.
    # status/error_message). This column's own vocabulary is deliberately
    # disjoint from hard_rule_result/qualification_decision above: it can
    # never be confused with either at the database level, only ever
    # written by the one pre-screen step, and never read by hard-rule
    # validation or qualification themselves.

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
