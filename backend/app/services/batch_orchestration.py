"""Phase 21 — batch orchestration.

Drives the existing pipeline for many candidates under one ICP/version by
calling each earlier phase's OWN endpoint function directly, in-process,
with a real `db: Session` and `registry: ProviderRegistry` — never a second
implementation of discovery, resolution, enrichment, evidence, hard
validation, business-model/signal classification, scoring, qualification,
adversarial review, verification, deduplication, or human review. Every
function imported below is the exact one app/main.py wires into an HTTP
route; FastAPI's `Depends(...)` markers are just ordinary default
parameter values at the Python level, so calling these functions directly
with real `db=`/`registry=` arguments reuses their behavior byte-for-byte.
This file adds no persistence logic of its own for any pipeline stage —
the only new persistence here is the batch/batch-item coordination
bookkeeping itself (app/models/batch.py).

PIPELINE PER CANDIDATE (mirrors the task's own ordering):
  discovery (once, for the whole batch)
    -> company resolution (once, for the whole discovery run)
    -> [per resolved company] enrichment
    -> people discovery + resolution (best-effort; a company-only lead is
       valid when no person is found)
    -> evidence import
    -> hard validation
    -> verification (only "where needed": a HOLD result with a specific
       rule field to escalate)
    -> business-model classification + commercial signals
    -> scoring
    -> LLM qualification
    -> adversarial review (only when qualification itself succeeded)
    -> deduplication (always attempted once a company_id exists)

RETRY / RESUME: re-invoking a batch never starts a candidate over. Each
BatchItemModel row records the last stage reached; the orchestrator skips
any per-item work whose output already exists (company_id/person_id/
lead_id already set, or outcome already terminal), so a process
interrupted mid-batch and resumed by calling it again continues where it
left off — WITHOUT creating a duplicate discovery candidate, canonical
company/person, or canonical lead. This falls directly out of Phase
7/10/19's OWN idempotency (a discovery candidate is resolved at most once;
CanonicalLeadModel has a real unique constraint) — nothing new is invented
here to make retries safe.

ONE FAILED ITEM NEVER FAILS THE BATCH: `_process_one_company` never
raises — an unexpected exception marks only that item FAILED and the loop
continues. The batch itself always reaches a terminal status (COMPLETED
or COMPLETED_WITH_ERRORS).

UNDER-DELIVERY IS EXPECTED, NEVER FORCED: `target_count` bounds how many
discovered candidates are considered; it is never padded, retried into
existence, or force-filled. A batch that discovers or resolves fewer
usable candidates than requested simply reports fewer items — nothing
here invents a candidate to make the numbers up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.api.adversarial_review import create_adversarial_review
from app.core.config import get_settings
from app.api.business_model import classify_company_business_model
from app.api.commercial_signal import extract_company_commercial_signals
from app.api.companies import enrich_company, resolve_discovery_run
from app.api.company_quality import create_company_quality
from app.api.evidence import EvidenceImportRequest, import_evidence
from app.api.field_verification import create_field_verification
from app.api.hard_icp_validation import create_validation
from app.api.lead_deduplication import create_lead_deduplication
from app.api.llm_qualification import create_lead_qualification
from app.api.people import resolve_people_discovery_run
from app.api.people_discovery import start_people_discovery_run
from app.api.scoring import create_lead_score
from app.models.batch import BatchItemModel, BatchModel
from app.models.company import CompanyResolutionModel
from app.models.discovery import DiscoveryCandidateModel, DiscoveryRunModel
from app.models.icp import ICPModel
from app.providers.contracts import ProviderCapability
from app.providers.explorium import _CROSS_BRANCH_TERM_LIST_KEYS
from app.providers.hermes import HERMES_PROVIDER_ID, check_hermes_job, submit_hermes_job
from app.providers.registry import ProviderRegistry
from app.schemas.adversarial_review import AdversarialReviewRequest
from app.schemas.batch import BatchItemOutcome, BatchItemStage, BatchStatus, DiscoveryMode
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.company_quality import CompanyQualityLabel, CompanyQualityRequest
from app.schemas.company_resolution import CompanyResolveRequest, ResolutionReasonCode, ResolutionStatus
from app.schemas.discovery_strategy import DiscoveryStrategy
from app.schemas.evidence import EntityType as EvidenceEntityType
from app.schemas.hard_icp_validation import HardIcpValidationRequest
from app.schemas.hard_rule_result import OverallResult
from app.schemas.lead_deduplication import LeadDeduplicationDecision, LeadDeduplicationRequest, LeadSourceReference
from app.schemas.llm_qualification import LeadQualificationRequest, QualificationDecision, QualificationExecutionStatus
from app.schemas.people_discovery import PeopleDiscoveryRunCreate
from app.schemas.person_resolution import PersonResolveRequest
from app.schemas.scoring import LeadScoreRequest
from app.schemas.verification import VerificationRequest
from app.services.company_discovery import run_company_discovery
from app.services.deep_prescreen import PrescreenCandidate, build_icp_summary, run_prescreen_batch
from app.services.discovery_strategy import build_term_origin_map, interpret_icp, merge_strategy_into_hard_rules
from app.services.icp_normalization import normalize_icp
from app.services.llm_providers.default_registry import (
    get_deep_prescreen_llm_provider,
    get_discovery_strategy_llm_provider,
    get_llm_provider,
)

_MAX_VERIFICATION_FIELDS_PER_ITEM = 2  # a hard ceiling so one pathological candidate can never dominate a batch run

# Phase 25: a distinct, honest terminal code for "this batch hit its own
# round ceiling with zero ACCEPTED results" — deliberately NOT reusing
# EXPLORIUM_CREDITS_EXHAUSTED/EXPLORIUM_AUTH_FAILED (app/providers/
# explorium.py), which are Explorium-specific and would misreport this as
# a provider failure when it may not be one; this can trip even against a
# provider with plenty of remaining credits, purely because the ICP's own
# criteria never produced an ACCEPTED candidate within the round budget.
DISCOVERY_ROUND_LIMIT_REACHED = "DISCOVERY_ROUND_LIMIT_REACHED"

# The open-web-search COMPANY_DISCOVERY provider_ids (see
# app/providers/tavily.py / app/providers/serper.py) — the ONLY ids
# discovery_mode filtering needs to know about explicitly. Deliberately
# an EXCLUSION set for Fast mode (below), not an inclusion allowlist of
# every possible non-web-search provider_id: this way any other
# registered COMPANY_DISCOVERY provider — Explorium, a mock, a future
# structured-database provider, or a test double standing in for one —
# is automatically treated as a "database/structured" source for Fast
# mode without this set ever needing to list it by name.
_WEB_SEARCH_PROVIDER_IDS = frozenset({"tavily-company-discovery-v1", "serper-company-discovery-v1"})


def _providers_allowed_for_mode(discovery_mode: str, provider_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Filters an already-computed active-provider-id tuple down to the
    ones this batch's discovery_mode permits — never adds a provider that
    wasn't already active (e.g. already exhausted or not registered). See
    app/schemas/batch.py::DiscoveryMode's own docstring for what each mode
    means.

    FAST excludes the web-search providers (Explorium, mocks, and any
    other registered structured/database-style provider are unaffected —
    this is exactly today's pre-existing behavior, since neither Tavily
    nor Serper existed before this feature).
    SAFE keeps ONLY the web-search providers.
    HARD (or any unrecognized value) keeps every already-active provider
    unfiltered — never silently discovers nothing because of a typo'd or
    future mode value."""
    if discovery_mode == DiscoveryMode.FAST.value:
        return tuple(pid for pid in provider_ids if pid not in _WEB_SEARCH_PROVIDER_IDS)
    if discovery_mode == DiscoveryMode.SAFE.value:
        return tuple(pid for pid in provider_ids if pid in _WEB_SEARCH_PROVIDER_IDS)
    return provider_ids


_STAGE_ORDER = list(BatchItemStage)


def _stage_index(stage_value: str) -> int:
    for i, stage in enumerate(_STAGE_ORDER):
        if stage.value == stage_value:
            return i
    return -1


def _at_least(item: BatchItemModel, stage: BatchItemStage) -> bool:
    return _stage_index(item.stage) >= _stage_index(stage.value)


def _touch_stage(db: Session, item: BatchItemModel, stage: BatchItemStage) -> None:
    item.stage = stage.value
    db.flush()


def _mark_failed(db: Session, item: BatchItemModel, message: str) -> None:
    item.outcome = BatchItemOutcome.FAILED.value
    item.error_message = message[:2000]
    db.flush()


def _seed_items_from_discovery(db: Session, registry: ProviderRegistry, batch: BatchModel, icp_id: str) -> list[BatchItemModel]:
    """Runs discovery for the batch's FIRST seed and creates one
    BatchItemModel per discovered candidate (capped at target_count — the
    original single-round cap this function has always had). Resumable: if
    this batch already has items from a prior invocation, they are reused
    untouched rather than re-discovered or duplicated.

    Delegates to _run_one_discovery_round for the actual discovery call and
    persistence (so the FIRST round's provider cursor/exhaustion signal is
    captured onto the batch exactly like every later round's — without
    this, a provider's real pagination state from round 1 would be
    silently discarded, and "Find More" would incorrectly re-request page 1
    every time). The only difference from a later round: this call's
    result is truncated to `target_count`, matching this function's
    original, pre-existing cap — later rounds are never truncated this
    way, since a target above what one round returns is exactly what
    "Find More"/target-count continuation exists to keep pursuing."""
    existing_items = db.query(BatchItemModel).filter(BatchItemModel.batch_id == batch.id).all()
    if existing_items:
        return existing_items

    items = _run_one_discovery_round(db, registry, batch, icp_id)
    if len(items) <= batch.requested_target_count:
        return items

    # Truncate to the original cap: any items beyond target_count from this
    # very first round are removed from tracking (never processed through
    # the pipeline) — they remain as real DiscoveryCandidateModel rows
    # (an honest historical record of what was discovered), just not
    # BatchItemModel-tracked in this batch. A later "Find More" round can
    # still re-discover distinct candidates via the cursor normally.
    kept, dropped = items[: batch.requested_target_count], items[batch.requested_target_count :]
    for item in dropped:
        db.delete(item)
    db.flush()
    return kept


def _already_seen_provider_external_ids(db: Session, batch_id: str) -> set[tuple[str, str]]:
    """(provider_id, external_id) pairs already present in this batch —
    used to pre-filter a new discovery round BEFORE creating any
    BatchItemModel row for a repeat candidate. This matters because
    BatchItemModel's unique constraint is on (batch_id, source_candidate_id),
    and source_candidate_id is always a fresh uuid4 per discovery call — a
    naive re-seed would NOT be blocked by that constraint and would waste a
    full evidence-import + hard-validation + LLM-qualification pass on a
    company already seen in this batch, before Phase 19 dedup finally
    caught it at the very last stage. Pre-filtering by the provider's own
    stable external_id is strictly cheaper."""
    rows = (
        db.query(DiscoveryCandidateModel.provider_id, DiscoveryCandidateModel.external_id)
        .join(BatchItemModel, BatchItemModel.source_candidate_id == DiscoveryCandidateModel.id)
        .filter(BatchItemModel.batch_id == batch_id)
        .all()
    )
    return {(provider_id, external_id) for provider_id, external_id in rows}


def _carries_new_cross_branch_provenance(existing_attributes: dict, incoming_attributes: dict) -> bool:
    """P2 fix — root cause: _already_seen_provider_external_ids (above)
    correctly avoids a redundant full pipeline pass for a repeat
    candidate, but it ALSO silently discarded that repeat sighting's own
    discovery attributes entirely — including a compound-ICP's second
    branch tag (e.g. "SaaS" found via a keyword branch in round 2, for a
    company already discovered via the "Healthcare" structured branch in
    round 1). Since app/services/evidence_import.py::collect_company_evidence
    only ever reads evidence from DiscoveryCandidateModel rows that
    actually exist, and app/services/hard_icp_validation.py::
    _cross_branch_corroborated_terms (Phase 37/38) was specifically built
    to union such cross-round provenance, a real corroborating company
    could reach round 2 and have its second branch's proof thrown away
    before that mechanism ever saw it — round 1's item, already
    processed to a terminal HOLD (COMPANY_TYPE_UNKNOWN-style HOLD on
    industry, in this scenario) before round 2 even runs, then had no way
    to ever learn about it.

    Uses the SAME fixed, generic key list app/providers/explorium.py's
    own _merge_cross_branch_attributes already established for exactly
    this "did a later branch add real provenance a former one didn't
    have" question (never a term VALUE, never ICP-specific) — reused
    here, not reimplemented, so there is exactly one place that decides
    which attribute keys mean "a discovery branch proved something."

    Returns True only when the incoming candidate's OWN provenance
    attributes name at least one covered value the existing sighting(s)
    for this external_id did not already have — a pure repeat within the
    SAME branch (e.g. re-paginating into the same company on the same
    keyword query) returns False, correctly staying a cheap no-op exactly
    as before this fix."""
    for key in _CROSS_BRANCH_TERM_LIST_KEYS:
        incoming_values = incoming_attributes.get(key)
        if not isinstance(incoming_values, list) or not incoming_values:
            continue
        existing_values = existing_attributes.get(key)
        existing_set = set(existing_values) if isinstance(existing_values, list) else set()
        if any(v not in existing_set for v in incoming_values):
            return True
    return False


def _persist_repeat_sightings_with_new_provenance(
    db: Session, batch: BatchModel, repeat_candidates: list
) -> set[str]:
    """P2 fix, continued — for each repeat (provider_id, external_id)
    candidate _already_seen_provider_external_ids filtered out of
    new_candidates, checks whether THIS round's sighting carries genuinely
    new cross-branch provenance compared to every DiscoveryCandidateModel
    row already persisted for that (provider_id, external_id) in this
    batch (there may be more than one, if this is itself a third+ round
    rediscovery). When it does, persists it as its own NEW
    DiscoveryCandidateModel row — deliberately a new row, not an in-place
    attribute merge into the existing one: app/api/evidence.py::
    _is_duplicate's natural key is (field, source_provider_id, external_id,
    retrieved_at), so mutating the existing row's attributes while reusing
    its id/discovered_at would make a later import_evidence() call
    silently treat the changed value as already-imported and skip it —
    a new row gets a genuinely new discovered_at (this round's real
    wall-clock time), so the new evidence is correctly recognized as an
    additional, honest sighting rather than a duplicate. This mirrors
    exactly how tests/test_discovery_qualification_bridge.py's own
    cross-round scenarios already model two separate discovery
    candidates for the same canonical company — this function is what
    makes that scenario actually arise from real orchestration, not just
    from hand-built test evidence.

    Deliberately creates NO BatchItemModel for these rows — the
    "don't waste a redundant pipeline pass" reason
    _already_seen_provider_external_ids exists for is completely
    unaffected; only the missing evidence is restored, never a second
    evidence-import/validation/scoring/LLM pass for its own sake.

    Returns the set of canonical_company_id values (via
    CompanyResolutionModel, already resolved when the ORIGINAL sighting
    was processed) that gained new provenance this round — the caller
    uses this to decide which HELD items are worth reopening."""
    affected_company_ids: set[str] = set()
    for candidate in repeat_candidates:
        # Deliberately NOT scoped to this batch: (provider_id, external_id)
        # names a real-world provider record — the same real company —
        # independent of which batch happened to discover it, and
        # CanonicalCompanyModel/CompanyResolutionModel are themselves
        # already global, never batch-scoped (see app/models/company.py —
        # canonical_domain is globally unique). Batch scoping matters for
        # deciding WHICH item to reopen (_reopen_held_items_for_companies,
        # below, correctly restricts THAT to batch.id) — never for reading
        # what discovery provenance already exists for a real-world
        # record. A row created by an earlier round of a DIFFERENT batch
        # for the same real company is genuine, correct, already-trusted
        # provenance to union against here, not a leak.
        existing_rows = (
            db.query(DiscoveryCandidateModel)
            .filter(
                DiscoveryCandidateModel.provider_id == candidate.provider_id,
                DiscoveryCandidateModel.external_id == candidate.external_id,
            )
            .all()
        )
        if not existing_rows:
            continue  # defensive only — every repeat candidate matched a row by construction
        combined_existing_attributes: dict = {}
        for row in existing_rows:
            for key in _CROSS_BRANCH_TERM_LIST_KEYS:
                values = (row.attributes or {}).get(key)
                if isinstance(values, list):
                    merged = combined_existing_attributes.setdefault(key, [])
                    merged.extend(v for v in values if v not in merged)
        if not _carries_new_cross_branch_provenance(combined_existing_attributes, candidate.attributes):
            continue

        # Any one of existing_rows may be the one an earlier
        # _resolve_companies call actually produced a CompanyResolutionModel
        # for — not necessarily the first (arbitrary DB order, and a
        # repeat row this same function persisted on an earlier round has
        # its own separate resolution row, added below). Check every one
        # rather than assuming index 0 is resolved.
        canonical_company_id = None
        for row in existing_rows:
            canonical_company_id = (
                db.query(CompanyResolutionModel.canonical_company_id)
                .filter(CompanyResolutionModel.candidate_id == row.id)
                .scalar()
            )
            if canonical_company_id:
                break
        if canonical_company_id is None:
            continue  # no resolved row among any prior sighting — nothing to link the new evidence to

        candidate_row = DiscoveryCandidateModel(
            id=candidate.id,
            run_id=existing_rows[0].run_id,
            icp_id=candidate.icp_id,
            icp_version=candidate.icp_version,
            provider_id=candidate.provider_id,
            external_id=candidate.external_id,
            name=candidate.name,
            domain=candidate.domain,
            attributes=candidate.attributes,
            discovered_at=candidate.discovered_at,
        )
        db.add(candidate_row)

        # app/services/evidence_import.py::collect_company_evidence only
        # ever reads a DiscoveryCandidateModel row via a
        # CompanyResolutionModel row pointing at it — without this, the
        # new row above is real but permanently invisible to evidence
        # import, since nothing links it to the canonical company.
        # Reusing ResolutionReasonCode.TRUSTED_PROVIDER_IDENTITY is
        # honest, not a shortcut: this candidate is a REPEAT sighting of
        # the exact same (provider_id, external_id) an earlier resolution
        # already trusted — see app/services/company_resolution.py's own
        # docstring naming a provider's own external id as its strongest
        # signal. This never re-runs Phase 7's own identity-resolution
        # decision logic; the identity question was already correctly
        # answered the first time this external_id was seen.
        db.add(
            CompanyResolutionModel(
                id=str(uuid4()),
                candidate_id=candidate_row.id,
                discovery_run_id=existing_rows[0].run_id,
                status=ResolutionStatus.MATCH.value,
                canonical_company_id=canonical_company_id,
                matched_company_id=canonical_company_id,
                reason_code=ResolutionReasonCode.TRUSTED_PROVIDER_IDENTITY.value,
                explanation=(
                    f"Repeat sighting of the same (provider_id={candidate.provider_id!r}, "
                    f"external_id={candidate.external_id!r}) already resolved to this canonical "
                    "company in an earlier round."
                ),
            )
        )
        affected_company_ids.add(canonical_company_id)
    db.flush()
    return affected_company_ids


def _reopen_held_items_for_companies(db: Session, batch: BatchModel, company_ids: set[str]) -> list[BatchItemModel]:
    """P2 fix, continued — the ONLY state this function ever reopens is
    BatchItemOutcome.HELD: per docs/lead-decision-policy.md's own
    contract, "HOLD is never terminal... it must always have a defined
    path back into the pipeline (more research, a later batch run, or a
    human decision)" — new corroborating evidence discovered in a later
    round of the SAME batch run is exactly that path. ACCEPTED, REJECTED,
    and DUPLICATE are explicitly terminal for the automated pipeline (same
    policy document) and are never touched here, matching every other
    resume-safety guarantee in this module (see BatchItemStage's own
    docstring). Rolling stage back to EVIDENCE_COLLECTED (not DISCOVERED)
    is deliberate: company_id/resolution already exist and must not be
    re-resolved; only evidence-import onward needs to re-run so the new
    provenance is actually collected and re-validated.

    Returns the reopened items so the caller (_run_one_discovery_round)
    can include them in what it hands back to run_batch's own processing
    loop — without this, a reopened item would sit at COMPANY_RESOLVED
    until some LATER round or resume call happened to process it, which
    could be never; run_batch's "stop this call if nothing new happened"
    check (see its own new_items/reopened_items handling) would otherwise
    also incorrectly treat a round whose ONLY effect was reopening a HELD
    item as empty and stop early, silently defeating this fix for exactly
    the case it exists for."""
    if not company_ids:
        return []
    items = (
        db.query(BatchItemModel)
        .filter(
            BatchItemModel.batch_id == batch.id,
            BatchItemModel.company_id.in_(company_ids),
            BatchItemModel.outcome == BatchItemOutcome.HELD.value,
        )
        .all()
    )
    for item in items:
        item.outcome = None
        item.qualification_decision = None
        item.hard_rule_result = None
        item.stage = BatchItemStage.COMPANY_RESOLVED.value
    db.flush()
    return items


def _get_or_build_discovery_strategy(db: Session, batch: BatchModel, canonical: CanonicalICP) -> DiscoveryStrategy:
    """Phase 10 continuation. Interprets the ICP into a DiscoveryStrategy
    EXACTLY ONCE per batch, ever — the persisted batch.discovery_strategy
    column (set here, on whichever round first reaches this function) is
    the cache; every later round, including every "Find More" call,
    deserializes and reuses it instead of calling the LLM provider again.

    A failed/degraded strategy (status != SUCCESS) is cached too, for the
    same reason a successful one is: this function's contract is "call the
    provider at most once," not "call it until it succeeds." Discovery
    remains fully correct either way, because merge_strategy_into_hard_rules
    is always a safe additive no-op for a non-SUCCESS strategy (see
    app/services/discovery_strategy.py's own DiscoveryStrategy validator:
    a non-SUCCESS strategy structurally cannot carry any proposed terms).

    Never raises: interpret_icp() itself already converts any provider
    exception into a PROVIDER_UNAVAILABLE DiscoveryStrategy (see
    app/services/llm_providers/base.py::LLMProvider.qualify), so there is
    no failure mode here that could abort a discovery round."""
    if batch.discovery_strategy is not None:
        return DiscoveryStrategy.model_validate(batch.discovery_strategy)

    # Phase 23: discovery-strategy interpretation resolves its OWN provider
    # (separate from get_llm_provider(), still used unchanged everywhere
    # else — Phase 16 qualification, Phase 17 adversarial review) so a
    # temporary Gemini A/B test here can never affect either of those.
    strategy = interpret_icp(canonical, get_discovery_strategy_llm_provider())
    batch.discovery_strategy = strategy.model_dump(mode="json")
    db.flush()
    return strategy


def _merged_canonical_icp(db: Session, batch: BatchModel, icp_id: str) -> tuple[CanonicalICP, dict[str, str]]:
    """Builds the SAME canonical ICP (user hard rules + the batch's
    once-per-batch-cached AI discovery strategy merged in) that
    _run_one_discovery_round has always built for every Explorium round —
    extracted unchanged (Phase 34) so Hermes (app/providers/hermes.py) can
    build its own search criteria from EXACTLY the same Gemini-interpreted,
    compound-aware terms Explorium gets, never a second, diverging
    interpretation. Returns (canonical_icp, term_origin) — term_origin is
    the same Phase 13D user-vs-AI-term provenance map _run_one_discovery_round
    already threaded through to Explorium's optional internal tagging."""
    icp_record = db.get(ICPModel, icp_id)
    canonical = normalize_icp(icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences)

    # Phase 10 continuation: the AI discovery strategy is interpreted once
    # per batch (cached on batch.discovery_strategy) and merged additively
    # into the ICP's own hard rules before EVERY round — the merged
    # industries/company_types/exclusions terms are what actually reach
    # run_company_discovery below, but Explorium's own matching cascade
    # (app/providers/explorium.py) never changes: a strategy-proposed term
    # is indistinguishable, once merged, from a term the user typed
    # directly. A failed/unavailable strategy merges as a no-op (see
    # _get_or_build_discovery_strategy), so discovery always proceeds using
    # at least the ICP's own original terms, exactly as before this phase.
    strategy = _get_or_build_discovery_strategy(db, batch, canonical)
    # Phase 13D: captured BEFORE the merge below collapses user and
    # AI-proposed terms into one indistinguishable tuple — this is the
    # only point in the whole pipeline where both are still simultaneously
    # knowable (see build_term_origin_map's own docstring). Purely
    # additive: passed through to run_company_discovery below only for
    # Explorium's optional internal tagging (app/providers/explorium.py::
    # execute()'s term_origin), never sent to Explorium's real API and
    # never changing merge_strategy_into_hard_rules's own unchanged
    # behavior on the line right after this.
    term_origin = build_term_origin_map(canonical.hard_rules, strategy)
    canonical = canonical.model_copy(update={"hard_rules": merge_strategy_into_hard_rules(canonical.hard_rules, strategy)})
    return canonical, term_origin


def _run_one_discovery_round(db: Session, registry: ProviderRegistry, batch: BatchModel, icp_id: str) -> list[BatchItemModel]:
    """Seeds ONE additional discovery round into an already-seeded batch —
    the operation behind "Find More Leads" and target-count continuation.
    Calls every registered COMPANY_DISCOVERY provider once each, using that
    provider's stored cursor (batch.discovery_cursors) so a real, paginating
    provider (Explorium) continues its query instead of restarting from
    page 1 — never invents pagination for a provider that doesn't report
    one. Creates new BatchItemModel rows ONLY for candidates not already
    seen anywhere in this batch (see _already_seen_provider_external_ids).
    Persists batch.discovery_cursors / discovery_exhausted_providers /
    discovery_pool_exhausted / discovery_rounds_run / discovery_error_code
    on the batch itself (not just returned in-memory) so this continuation
    state survives a page refresh and is never lost between requests.

    P2 fix: a repeat candidate (same (provider_id, external_id) already
    seen this batch) is never re-seeded as a new BatchItemModel — but when
    it carries genuinely new cross-branch discovery provenance (e.g. the
    same real company found via a DIFFERENT Explorium branch this round —
    see _persist_repeat_sightings_with_new_provenance's own docstring),
    that provenance is persisted and any HELD item for the affected
    canonical company is reopened (_reopen_held_items_for_companies).
    Returns the newly created items PLUS any reopened items — everything
    from this round worth advancing through the pipeline, never just the
    genuinely-new ones alone."""
    batch.discovery_error_code = None
    batch.discovery_error_message = None

    canonical, term_origin = _merged_canonical_icp(db, batch, icp_id)

    already_seen = _already_seen_provider_external_ids(db, batch.id)
    cursors = dict(batch.discovery_cursors or {})
    exhausted_providers = set(batch.discovery_exhausted_providers or [])

    all_discovery_providers = registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
    # Providers already exhausted are never asked again — this is the
    # "don't repeatedly run the exact same provider query" requirement:
    # once a provider has told us it has nothing more for this ICP, no
    # further round calls it, ever, for this batch. Passing provider_order
    # as exactly the active (non-exhausted) provider ids doesn't just
    # reorder — run_company_discovery iterates provider_order itself, so an
    # id left out of it is never called at all.
    active_providers = tuple(pid for pid in (p.provider_id for p in all_discovery_providers) if pid not in exhausted_providers)
    # Phase — discovery_mode filtering (fast/safe/hard, see
    # app/schemas/batch.py::DiscoveryMode). Applied AFTER the exhaustion
    # filter above, never before: a provider already exhausted for this
    # ICP stays excluded regardless of mode, and this filter only ever
    # narrows active_providers further, never re-includes an exhausted one.
    active_providers = _providers_allowed_for_mode(batch.discovery_mode, active_providers)

    result = run_company_discovery(
        canonical,
        registry,
        limit=batch.discovery_limit,
        provider_order=active_providers,
        cursors=cursors,
        term_origin=term_origin,
    )

    for provider_id, error_code, error_message in (
        (outcome.provider_id, outcome.error.code, outcome.error.message)
        for outcome in result.provider_outcomes
        if not outcome.success and outcome.error and not outcome.error.retryable
    ):
        # A non-retryable failure (e.g. EXPLORIUM_CREDITS_EXHAUSTED,
        # EXPLORIUM_AUTH_FAILED) is surfaced honestly and stops further
        # automatic rounds — never silently retried in a loop. Its cursor
        # is left untouched so the next resume retries from the same point
        # once the underlying cause is resolved.
        batch.discovery_error_code = error_code
        # Phase 7I: previously only the code was kept — the real message
        # (e.g. the actual exception text from a failed HTTP call, see
        # app/providers/base.py's ProviderAdapter.run()) was computed but
        # then discarded, leaving a completed-but-empty batch
        # undiagnosable. Truncated to the column's 2000-char cap; a
        # provider error message has never been observed anywhere close to
        # that length, so this is a safety bound, not an expected case.
        batch.discovery_error_message = error_message[:2000] if error_message else None

    cursors.update(result.next_cursors)
    exhausted_providers.update(result.exhausted_providers)

    new_candidates = [c for c in result.candidates if (c.provider_id, c.external_id) not in already_seen]

    # P2 fix — cross-round compound-ICP corroboration: a candidate NOT in
    # new_candidates (same (provider_id, external_id) already seen this
    # batch) is the same real company rediscovered via a possibly
    # DIFFERENT branch this round (e.g. round 1's Healthcare structured
    # branch, round 2's SaaS keyword branch) — see
    # _persist_repeat_sightings_with_new_provenance's own docstring for
    # the full root cause. Restoring that provenance and reopening any
    # HELD item it affects happens here, once per round, BEFORE this
    # round's own new_candidates are processed — so a company reopened
    # this way is re-validated together with everything else this round,
    # never as a separate pass.
    repeat_candidates = [c for c in result.candidates if (c.provider_id, c.external_id) in already_seen]
    reopened_items: list[BatchItemModel] = []
    if repeat_candidates:
        affected_company_ids = _persist_repeat_sightings_with_new_provenance(db, batch, repeat_candidates)
        reopened_items = _reopen_held_items_for_companies(db, batch, affected_company_ids)

    run_row = DiscoveryRunModel(
        id=result.run_id,
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        status=result.status.value,
        requested_limit=batch.discovery_limit,
        total_returned=len(new_candidates),
        provider_outcomes=[
            {
                "provider_id": outcome.provider_id,
                "success": outcome.success,
                "requested": outcome.requested,
                "returned": outcome.returned,
                "latency_ms": outcome.latency_ms,
                "error_code": outcome.error.code if outcome.error else None,
                "error_message": outcome.error.message if outcome.error else None,
            }
            for outcome in result.provider_outcomes
        ],
    )
    db.add(run_row)

    items: list[BatchItemModel] = []
    for candidate in new_candidates:
        candidate_row = DiscoveryCandidateModel(
            id=candidate.id,
            run_id=result.run_id,
            icp_id=candidate.icp_id,
            icp_version=candidate.icp_version,
            provider_id=candidate.provider_id,
            external_id=candidate.external_id,
            name=candidate.name,
            domain=candidate.domain,
            attributes=candidate.attributes,
            discovered_at=candidate.discovered_at,
        )
        db.add(candidate_row)

        item = BatchItemModel(
            id=str(uuid4()),
            batch_id=batch.id,
            icp_id=batch.icp_id,
            icp_version=batch.icp_version,
            source_candidate_id=candidate.id,
            stage=BatchItemStage.DISCOVERED.value,
        )
        db.add(item)
        items.append(item)
    db.flush()

    batch.discovered_count += len(new_candidates)
    batch.discovery_cursors = cursors
    batch.discovery_exhausted_providers = sorted(exhausted_providers)
    batch.discovery_pool_exhausted = bool(all_discovery_providers) and exhausted_providers.issuperset(
        p.provider_id for p in all_discovery_providers
    )
    batch.discovery_rounds_run += 1

    if items:
        _resolve_companies(db, result.run_id, items)
    db.flush()
    # P2 fix: reopened_items are already company-resolved (COMPANY_RESOLVED
    # stage, real company_id) — appended, never merged/deduped with items,
    # since a reopened item's source_candidate_id already differs from
    # every genuinely-new item's by construction (it was seeded in an
    # earlier round). See run_batch's own processing loop, which treats
    # this return value as "everything from this round worth advancing
    # through the pipeline," new or reopened alike.
    return items + reopened_items


def _accepted_count(db: Session, batch_id: str) -> int:
    """The live count of ACCEPTED BatchItemModel rows for a batch — never
    total/discovered candidate count, matching _should_run_another_discovery_round's
    own "only ACCEPTED counts toward the target" rule. Shared by that
    function and by run_batch's per-item processing loop (see its own
    early-stop comment) so both ends of round-bounding agree on the exact
    same definition of "enough.\""""
    return (
        db.query(BatchItemModel)
        .filter(BatchItemModel.batch_id == batch_id, BatchItemModel.outcome == BatchItemOutcome.ACCEPTED.value)
        .count()
    )


def _should_run_another_discovery_round(db: Session, batch: BatchModel) -> bool:
    """Whether to seed and process one more discovery round. Counts ONLY
    ACCEPTED outcomes toward the target — never total/discovered candidate
    count — so a target is never satisfied by padding with HELD/REJECTED
    candidates: "43 genuinely qualify" means 43 ACCEPTED items, and this
    function keeps returning True until that many ACCEPTED items actually
    exist, the provider pool is exhausted, a real error stops things, or
    (Phase 25) this batch's own round ceiling is reached.

    Phase 25: settings.max_discovery_rounds_per_batch is a hard, backend-
    enforced ceiling on batch.discovery_rounds_run (already persisted,
    already incremented once per round in _run_one_discovery_round — this
    function is the first and only place it is ever CHECKED as a limit).
    This is deliberately independent of any per-call max_discovery_rounds
    argument a caller passes (app/api/batch.py's resume endpoint,
    frontend "Find More"/target-count auto-continue): a batch with zero
    ACCEPTED results can never exceed this many rounds in its lifetime, no
    matter how many times or with what arguments resume is called. When
    tripped, discovery_error_code is set to DISCOVERY_ROUND_LIMIT_REACHED
    — an honest, distinct, non-retryable terminal state (mirrors the
    already-existing pattern for a real Explorium error) rather than a
    silent stop or a crash."""
    if batch.discovery_error_code is not None:
        return False  # a real error (auth failure, credits exhausted) must never be silently retried in a loop
    if batch.discovery_pool_exhausted:
        return False  # every registered provider has told us it has nothing more

    accepted_so_far = _accepted_count(db, batch.id)

    max_rounds = get_settings().max_discovery_rounds_per_batch
    rounds_run = batch.discovery_rounds_run or 0  # the column default (0) only applies on INSERT; an unflushed in-memory BatchModel can still be None
    if rounds_run >= max_rounds:
        # batch.accepted_count itself is only refreshed by _finalize_counts
        # at the very end of run_batch, so it can be stale mid-loop — the
        # live accepted_so_far query above (already computed for the
        # target-count check below) is what this message actually reports.
        batch.discovery_error_code = DISCOVERY_ROUND_LIMIT_REACHED
        batch.discovery_error_message = (
            f"Reached the maximum of {max_rounds} discovery rounds for this batch with "
            f"{accepted_so_far} of {batch.requested_target_count} requested leads accepted. "
            "Stopping rather than continuing to search indefinitely."
        )
        return False

    return accepted_so_far < batch.requested_target_count


def _hermes_criteria_from_canonical_icp(canonical: CanonicalICP) -> dict[str, object]:
    """Maps a CanonicalICP's own real hard-rule fields into Hermes's
    schema-free /search_icp criteria dict — generically, the exact same
    fields regardless of what industry/geography/etc. they happen to
    contain. No industry-specific branching of any kind: this function
    would produce the identical shape for "Healthcare SaaS", "Fintech
    SaaS", "FMCG", or any other ICP, per the task's own "do not hardcode"
    requirement. Only fields the ICP actually states are included — an
    empty/unset hard rule contributes nothing, exactly like every other
    provider adapter in this codebase (see app/providers/explorium.py's
    own "never filled with None to complete the shape" discipline)."""
    hard = canonical.hard_rules
    criteria: dict[str, object] = {}

    if hard.industries:
        # A single joined string, not a list — matches the vendor's own
        # example requests (e.g. "medical spa / aesthetic clinics"), and
        # keeps the compound-ICP intent (e.g. "Healthcare" AND "SaaS")
        # legible to the agent as ONE combined criterion rather than two
        # independent ones it might treat as alternatives.
        criteria["industry"] = " / ".join(hard.industries)
        criteria["keywords"] = list(hard.industries)

    geography_terms = [entry.label for entry in hard.geography.countries] + list(hard.geography.unrecognized)
    if geography_terms:
        criteria["geography"] = ", ".join(geography_terms)

    if hard.employee_range.min is not None or hard.employee_range.max is not None:
        lo = hard.employee_range.min if hard.employee_range.min is not None else ""
        hi = hard.employee_range.max if hard.employee_range.max is not None else ""
        criteria["employee_size"] = f"{lo}-{hi}" if lo or hi else None

    if hard.company_types:
        criteria["company_type"] = list(hard.company_types)

    if hard.exclusions:
        criteria["exclude"] = list(hard.exclusions)

    return {k: v for k, v in criteria.items() if v}


def _import_hermes_records(db: Session, batch: BatchModel, icp_id: str, icp_version: int, records: tuple[dict, ...]) -> list[BatchItemModel]:
    """Ingests Hermes's completed-job `records` through the EXACT SAME
    DiscoveryCandidateModel -> DiscoveryRunModel -> BatchItemModel ->
    _resolve_companies pipeline _run_one_discovery_round uses for
    Explorium — never a second, parallel candidate/evidence
    representation. This is what makes dedup-by-domain (Phase 7,
    app/services/company_resolution.py — completely unmodified), evidence
    field-reading (app/services/evidence_import.py, also unmodified), and
    provenance (a real, distinct source_provider_id/external_id per
    record) all work identically for a Hermes-sourced candidate as for an
    Explorium one, with zero new code in either of those modules.

    Every field is honestly mapped 1:1 from Hermes's own documented
    response shape (company_name, website, linkedin_url, employee_size,
    industry, location, description, source_url) — never invented,
    never re-interpreted, and a null/missing Hermes field simply means
    that attribute key is absent, exactly like every other provider
    adapter's "never fabricate a field the provider didn't actually
    return" discipline (see app/providers/explorium.py's own
    _BUSINESS_ATTRIBUTE_MAP comment). Deliberately does NOT carry any
    _TRUSTED_STRUCTURED provenance tag of any kind — Hermes's industry/
    employee_size/location are unverified agent-read web text, never a
    verified taxonomy match, so they must never reach SUPPORTED_STRUCTURED
    off a single sighting the way a real Explorium record can (see
    app/services/evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS, which
    this deliberately never adds "hermes-icp-search-v1" to)."""
    already_seen = _already_seen_provider_external_ids(db, batch.id)

    new_candidates = []
    for index, record in enumerate(records):
        company_name = record.get("company_name")
        if not company_name:
            # A record with no company name at all cannot be meaningfully
            # tracked as a candidate — skip it rather than inventing a
            # placeholder name (the vendor's own docs say any field may be
            # null; company_name is the one this pipeline cannot do
            # without, since app/services/evidence_import.py reads it as
            # the company_identity evidence field).
            continue
        external_id = f"hermes:{batch.id}:{index}:{(record.get('website') or company_name)[:120]}"
        if (HERMES_PROVIDER_ID, external_id) in already_seen:
            continue

        attributes: dict[str, object] = {}
        if record.get("industry"):
            attributes["industry"] = record["industry"]
        if record.get("location"):
            attributes["country"] = record["location"]
        if record.get("employee_size"):
            attributes["employee_range"] = record["employee_size"]
        if record.get("linkedin_url"):
            attributes["linkedin_id"] = record["linkedin_url"]
        if record.get("description"):
            attributes["description"] = record["description"]
        if record.get("source_url"):
            attributes["hermes_source_url"] = record["source_url"]

        new_candidates.append(
            {
                "id": str(uuid4()),
                "external_id": external_id,
                "name": company_name,
                "domain": record.get("website"),
                "attributes": attributes,
            }
        )

    if not new_candidates:
        return []

    run_id = str(uuid4())
    run_row = DiscoveryRunModel(
        id=run_id,
        icp_id=icp_id,
        icp_version=icp_version,
        status="COMPLETED",
        requested_limit=len(records),
        total_returned=len(new_candidates),
        provider_outcomes=[
            {
                "provider_id": HERMES_PROVIDER_ID,
                "success": True,
                "requested": len(records),
                "returned": len(new_candidates),
                "latency_ms": None,
                "error_code": None,
                "error_message": None,
            }
        ],
    )
    db.add(run_row)

    items: list[BatchItemModel] = []
    for candidate in new_candidates:
        candidate_row = DiscoveryCandidateModel(
            id=candidate["id"],
            run_id=run_id,
            icp_id=icp_id,
            icp_version=icp_version,
            provider_id=HERMES_PROVIDER_ID,
            external_id=candidate["external_id"],
            name=candidate["name"],
            domain=candidate["domain"],
            attributes=candidate["attributes"],
            discovered_at=datetime.now(timezone.utc),
        )
        db.add(candidate_row)

        item = BatchItemModel(
            id=str(uuid4()),
            batch_id=batch.id,
            icp_id=batch.icp_id,
            icp_version=batch.icp_version,
            source_candidate_id=candidate["id"],
            stage=BatchItemStage.DISCOVERED.value,
        )
        db.add(item)
        items.append(item)
    db.flush()

    batch.discovered_count += len(new_candidates)
    if items:
        _resolve_companies(db, run_id, items)
    db.flush()
    return items


def _maybe_advance_hermes_job(db: Session, batch: BatchModel, icp_id: str, allow_submit: bool) -> list[BatchItemModel]:
    """Phase 36 — Hermes and Explorium are BOTH real company-discovery
    sources for a batch, not "Hermes as a fallback that only exists
    because Explorium failed": Explorium's own round loop (structured,
    always run first — see run_batch) and Hermes are both consulted
    within the SAME run_batch call whenever the batch is still short of
    its target — regardless of WHY: Explorium simply not returning
    enough genuinely-matching candidates, or Explorium being entirely
    unavailable this round (a real, established error like
    EXPLORIUM_CREDITS_EXHAUSTED/EXPLORIUM_AUTH_FAILED — see
    app/providers/explorium.py — which stops the Explorium round loop for
    THIS call but never blocks this function, called unconditionally
    afterward). Both sources' candidates flow through the exact same
    resolution/evidence/hard-validation/ranking pipeline — there is no
    separate "Hermes path." Called once per run_batch invocation, AFTER
    the Explorium round loop finishes (never interleaved mid-round with
    it — see run_batch's own docstring for why Hermes's async, minutes-
    long job shape can't share Explorium's per-round synchronous loop).

    Never blocks: exactly ONE check_hermes_job call (never a poll loop)
    when a job is already pending, and exactly ONE submit_hermes_job call
    (never more than one job in flight per batch, matching the vendor's
    own MAX_CONCURRENT_JOBS=1) when nothing is pending and this call is
    allowed to submit. `allow_submit=False` on the very first (create_batch)
    call keeps that call exactly as fast as before this phase — Hermes
    only ever gets submitted from a resume/"Add More Leads" call, never
    the initial synchronous request.

    Degrades safely on any failure (auth, timeout, queue-full, a terminal
    non-completed job status): batch.hermes_job_id is cleared and
    batch.discovery_error_code/message are left untouched (a Hermes
    problem is never reported as a discovery_error_code, which is
    Explorium's own field — see DISCOVERY_ROUND_LIMIT_REACHED's own
    comment on keeping distinct failure sources distinct) so Explorium's
    own results are never affected or hidden by a Hermes outage."""
    settings = get_settings()
    if not settings.hermes_api_token:
        return []  # Hermes not configured — Explorium-only, unchanged

    if batch.hermes_job_id:
        status_result = check_hermes_job(
            base_url=settings.hermes_base_url,
            api_token=settings.hermes_api_token,
            job_id=batch.hermes_job_id,
        )
        if not status_result.success:
            # Auth failure, network error, unknown job, or a terminal
            # non-completed status (failed/timeout/rate_limited/cancelled)
            # — clear job state so a later call may try again if still
            # short, and never raise or affect Explorium's own results.
            batch.hermes_job_id = None
            batch.hermes_job_status = status_result.status or "error"
            db.flush()
            return []
        if not status_result.done:
            # Still queued/running — leave state exactly as-is for a later
            # call to check again. Never submit a second job while one is
            # already in flight.
            batch.hermes_job_status = status_result.status
            db.flush()
            return []

        # done=True and success=True only ever means status == "completed"
        # (see check_hermes_job's own contract) — safe to import records.
        icp_record = db.get(ICPModel, icp_id)
        new_items = _import_hermes_records(db, batch, icp_id, icp_record.version, status_result.records)
        batch.hermes_job_id = None
        batch.hermes_job_status = status_result.status
        db.flush()
        return new_items

    if not allow_submit:
        return []

    remaining = batch.requested_target_count - _accepted_count(db, batch.id)
    if remaining <= 0:
        return []  # already at target — never submit a Hermes job nobody needs

    canonical, _ = _merged_canonical_icp(db, batch, icp_id)
    criteria = _hermes_criteria_from_canonical_icp(canonical)
    # Sized to the ACTUAL remaining gap, never a padded/round number, and
    # never above the vendor's own documented sane job size — see
    # settings.hermes_max_records_per_job's own docstring.
    num_records = min(remaining, settings.hermes_max_records_per_job)

    submit_result = submit_hermes_job(
        base_url=settings.hermes_base_url,
        api_token=settings.hermes_api_token,
        criteria=criteria,
        num_records=num_records,
    )
    if not submit_result.success:
        # A failed submit (auth, queue full, transient network error)
        # degrades silently — Explorium's own results are already final
        # for this call by the time this function runs, so there is
        # nothing to roll back. batch.hermes_job_id stays None, so a later
        # call may try submitting again.
        return []

    batch.hermes_job_id = submit_result.job_id
    batch.hermes_job_status = submit_result.status
    batch.hermes_submitted_at = datetime.now(timezone.utc)
    db.flush()
    return []


def _resolve_companies(db: Session, discovery_run_id: str, items: list[BatchItemModel]) -> None:
    """Resolves the whole discovery run at once (Phase 7's own batch
    shape) and maps each result back onto its BatchItemModel by candidate
    id. An UNRESOLVED candidate is not treated as a pipeline bug — Phase 7
    deliberately refuses to merge on weak signals — but it cannot proceed
    further in this batch, so it is recorded as FAILED with an honest,
    specific reason rather than silently dropped or force-merged."""
    resolutions = resolve_discovery_run(CompanyResolveRequest(discovery_run_id=discovery_run_id), db=db)
    by_candidate = {r.candidate_id: r for r in resolutions}

    for item in items:
        if item.company_id is not None or item.outcome is not None:
            continue
        resolution = by_candidate.get(item.source_candidate_id)
        if resolution is None:
            _mark_failed(db, item, "No resolution result was produced for this discovery candidate.")
            continue
        if resolution.status == ResolutionStatus.UNRESOLVED.value:
            _mark_failed(
                db, item, f"Company identity is unresolved ({resolution.reason_code}); cannot proceed without a confirmed company."
            )
            continue
        item.company_id = resolution.canonical_company_id
        _touch_stage(db, item, BatchItemStage.COMPANY_RESOLVED)


def _attempt_verification(db: Session, registry: ProviderRegistry, icp_id: str, company_id: str, validation) -> None:
    """Escalates at most _MAX_VERIFICATION_FIELDS_PER_ITEM HOLD-causing
    rule fields via Phase 18 — never unconditional, and never more than a
    bounded number of fields for one candidate. Failures here are
    swallowed: verification is a best-effort escalation, never a
    requirement for the batch to proceed."""
    hold_fields = [r["rule"] for r in validation.rule_results if r["status"] == "HOLD"]
    for field in hold_fields[:_MAX_VERIFICATION_FIELDS_PER_ITEM]:
        try:
            create_field_verification(
                VerificationRequest(icp_id=icp_id, entity_type=EvidenceEntityType.COMPANY, entity_id=company_id, field=field),
                db=db,
                registry=registry,
            )
        except Exception:
            pass


def _classify_outcome(item: BatchItemModel, validation, dedup, is_self_match: bool = False) -> None:
    # Phase 19 calls this MATCHED_EXISTING_LEAD, not "DUPLICATE" — this is
    # the one vocabulary translation this module performs: a candidate
    # whose (company_id, person_id) pair already maps to a lead created
    # earlier (in this batch or a previous one) is a duplicate CANDIDATE
    # from the batch's point of view, even though the canonical lead
    # itself is neither new nor wrong.
    #
    # P2 fix — is_self_match: when a HELD item is reopened after gaining
    # new corroborating evidence (see _reopen_held_items_for_companies)
    # and re-processed, it correctly "MATCHED_EXISTING_LEAD" against ITS
    # OWN lead_id from before this pass — that is not a duplicate
    # candidate, it is this exact candidate's own already-established
    # lead being correctly re-found. Only the caller
    # (_advance_company_pipeline) can know that distinction (it alone
    # knows the item's lead_id BEFORE this pass's own dedup call ran);
    # this function trusts that signal rather than re-deriving it, the
    # same "caller owns what only the caller can know" discipline this
    # module already applies elsewhere (e.g. _attempt_verification's own
    # bounded hold_fields list).
    if dedup.decision == LeadDeduplicationDecision.MATCHED_EXISTING_LEAD.value and not is_self_match:
        item.outcome = BatchItemOutcome.DUPLICATE.value
        return
    if validation.overall_result == OverallResult.FAIL.value:
        item.outcome = BatchItemOutcome.REJECTED.value
        return
    if validation.overall_result == OverallResult.HOLD.value:
        item.outcome = BatchItemOutcome.HELD.value
        return
    # PASS: classify by the LLM qualification decision when one exists,
    # otherwise HOLD honestly rather than guessing an outcome.
    if item.qualification_decision in (QualificationDecision.GOOD_FIT.value, QualificationDecision.WEAK_FIT.value):
        item.outcome = BatchItemOutcome.ACCEPTED.value
    elif item.qualification_decision == QualificationDecision.NOT_FIT.value:
        item.outcome = BatchItemOutcome.REJECTED.value
    else:
        item.outcome = BatchItemOutcome.HELD.value


def _run_deep_prescreen_for_items(db: Session, batch: BatchModel, icp_id: str, items: list[BatchItemModel]) -> None:
    """Deep discovery mode only (see app/schemas/batch.py::DiscoveryMode.DEEP)
    — a complete no-op for every other mode, checked FIRST so a fast/safe/
    hard batch never even builds an ICP summary or resolves a pre-screen
    LLM provider. Called once per already-resolved item list (the initial
    seed, and each later discovery round's new_items), BEFORE that list's
    own _process_one_company loop — deliberately NOT inside
    _advance_company_pipeline itself, so the bounded-concurrency pass
    (app/services/deep_prescreen.py::run_prescreen_batch) judges the whole
    group of candidates together instead of degrading to one-at-a-time
    concurrency of exactly 1. This runs BEFORE the per-item loop's own
    target-count early-exit check (see run_batch's own comment on that
    check) because pre-screen cost is bounded by discovery_limit alone
    (independent of target_count — see the deep-mode implementation plan's
    own §6 cost model), not by how many more accepted leads are still
    needed; the EXPENSIVE downstream steps (evidence, enrichment,
    qualification) still only ever run for items the early-exit check lets
    through, completely unaffected by this function's own timing.

    Only items already at exactly COMPANY_RESOLVED with no company_id gap
    and no terminal outcome are eligible — mirrors _at_least/_touch_stage's
    own resume-safety discipline: an item already past DEEP_PRESCREENED
    (from a prior run of this same batch) is never re-judged, and an item
    that never resolved a company_id is skipped exactly like
    _advance_company_pipeline already skips it (via its own
    "no company_id -> FAILED" branch, still reached normally for such
    items since this function performs no per-item failure handling of its
    own).

    NEVER raises: run_prescreen_batch's own contract already guarantees
    every input candidate gets a result and no worker exception escapes;
    this function additionally never lets a missing/degraded result block
    an item — an item this function fails to judge for any reason is left
    with deep_prescreen_verdict=None, which _advance_company_pipeline
    treats exactly like a RELEVANT verdict (let it through)."""
    if batch.discovery_mode != DiscoveryMode.DEEP.value:
        return

    eligible = [
        item
        for item in items
        if item.outcome is None and item.company_id is not None and item.stage == BatchItemStage.COMPANY_RESOLVED.value
    ]
    if not eligible:
        return

    settings = get_settings()
    candidate_ids = [item.source_candidate_id for item in eligible]
    discovery_rows = {
        row.id: row
        for row in db.query(DiscoveryCandidateModel).filter(DiscoveryCandidateModel.id.in_(candidate_ids)).all()
    }

    canonical, _ = _merged_canonical_icp(db, batch, icp_id)
    icp_summary = build_icp_summary(canonical)

    prescreen_candidates: list[PrescreenCandidate] = []
    for item in eligible:
        discovery_row = discovery_rows.get(item.source_candidate_id)
        if discovery_row is None:
            continue  # no discovery provenance to judge from; leave verdict=None (let it through)
        attributes = discovery_row.attributes or {}
        homepage_url = f"https://{discovery_row.domain}" if discovery_row.domain else None
        snippet = attributes.get("description") or ""
        prescreen_candidates.append(
            PrescreenCandidate(
                batch_item_id=item.id, candidate_name=discovery_row.name, homepage_url=homepage_url, search_snippet=snippet
            )
        )
    if not prescreen_candidates:
        return

    provider = get_deep_prescreen_llm_provider()
    results = run_prescreen_batch(prescreen_candidates, icp_summary, provider, settings)

    items_by_id = {item.id: item for item in eligible}
    for batch_item_id, result in results.items():
        item = items_by_id.get(batch_item_id)
        if item is None:
            continue
        item.deep_prescreen_verdict = result.verdict
        item.deep_prescreen_reason = result.reason[:500] if result.reason else None
        item.deep_prescreen_homepage_fetched = result.homepage_fetched
        item.deep_prescreen_status = result.status
        _touch_stage(db, item, BatchItemStage.DEEP_PRESCREENED)
    db.commit()


def _advance_company_pipeline(
    db: Session, registry: ProviderRegistry, item: BatchItemModel, icp_id: str, people_limit: int
) -> None:
    """PHASE 30 ORDERING (company-first, person-discovery last): evidence
    -> hard validation -> business-model/signals -> Phase 15 scoring ->
    company-quality gate -> [only if not hard-rejected] Unipile/person
    discovery -> LLM qualification -> adversarial review -> dedup.

    Before Phase 30, people discovery (Unipile) ran immediately after
    enrichment, BEFORE hard validation or scoring ever happened — every
    resolved company got a real Unipile call, including ones that would
    go on to hard-FAIL. That contradicted the product's own company-first
    architecture (discover -> qualify companies -> THEN find people at the
    surviving companies) and spent person-discovery calls on companies
    that were never going to be used. This function now runs the
    deterministic hard-rule gate, scoring, and the new company-quality
    layer (app/services/company_quality.py) FIRST, and only calls Unipile
    for a company whose quality label is not REJECT (REJECT is assigned
    only for a hard-rule FAIL — see CompanyQualityResult's own contract).
    A HOLD or PASS company (REVIEW/STRONG) still gets a person-discovery
    attempt exactly as before, preserving today's "a company-only lead is
    valid when no person is found" behavior for every candidate that was
    never going to be hard-rejected.

    Deep mode: if item.deep_prescreen_verdict == "NOT_RELEVANT" (set by
    _run_deep_prescreen_for_items, ALWAYS called before this function for
    every item — see run_batch), this function stops immediately after
    marking a REJECTED outcome, before evidence import or hard validation
    ever run — the entire point of the pre-screen: no evidence-import/
    hard-rule/qualification cycle is spent on a candidate it judged
    irrelevant. Every OTHER value (None, or "RELEVANT") is treated
    identically to a fast/safe/hard item: this function proceeds exactly
    as it always has, completely unaware whether deep mode ran at all.
    A REJECTED outcome from this branch is a discovery-stage filter
    decision, NOT a hard-rule FAIL — it is recorded as REJECTED (the
    existing, correct BatchItemOutcome vocabulary for "this candidate does
    not become a lead"), never as a fabricated hard_rule_result value; item
    .hard_rule_result stays None, exactly like every other outcome this
    function never reaches hard validation for."""
    if item.outcome is not None:
        return  # already terminal from a prior run of this batch - resume skips it entirely

    if item.deep_prescreen_verdict == "NOT_RELEVANT":
        item.outcome = BatchItemOutcome.REJECTED.value
        _touch_stage(db, item, BatchItemStage.DONE)
        return

    company_id = item.company_id
    if company_id is None:
        _mark_failed(db, item, "Item has no resolved company_id; cannot continue.")
        return

    if not _at_least(item, BatchItemStage.EVIDENCE_COLLECTED):
        import_evidence(EvidenceImportRequest(entity_type=EvidenceEntityType.COMPANY, entity_id=company_id), db=db)
        _touch_stage(db, item, BatchItemStage.EVIDENCE_COLLECTED)

    # Hard validation, verification, business-model/signals, and scoring
    # are all still company-only here (person_id=None) — a person has not
    # been discovered yet at this point in the pipeline, exactly the
    # "strong companies first" ordering this phase exists to enforce.
    validation = create_validation(HardIcpValidationRequest(icp_id=icp_id, company_id=company_id, person_id=None), db=db)
    item.hard_rule_result = validation.overall_result
    _touch_stage(db, item, BatchItemStage.HARD_VALIDATED)

    if validation.overall_result == OverallResult.HOLD.value:
        _attempt_verification(db, registry, icp_id, company_id, validation)
        # Verification may have added evidence; re-validate once more so
        # the batch reflects the freshest hard-rule result before scoring.
        validation = create_validation(HardIcpValidationRequest(icp_id=icp_id, company_id=company_id, person_id=None), db=db)
        item.hard_rule_result = validation.overall_result

    # Business model + commercial signals are diagnostic and unconditional
    # regardless of hard-rule outcome, exactly like Phase 15's own scores.
    classify_company_business_model(company_id, db=db)
    extract_company_commercial_signals(company_id, db=db)
    _touch_stage(db, item, BatchItemStage.CLASSIFIED)

    create_lead_score(LeadScoreRequest(icp_id=icp_id, company_id=company_id, person_id=None), db=db)
    _touch_stage(db, item, BatchItemStage.SCORED)

    quality = create_company_quality(CompanyQualityRequest(icp_id=icp_id, company_id=company_id), db=db)
    _touch_stage(db, item, BatchItemStage.COMPANY_QUALITY_SCORED)

    # Phase 32: enrichment (like person discovery below) is only ever
    # spent on a company that survived deterministic validation — see
    # BatchItemStage's own docstring for the full rationale. Discovery's
    # own evidence (already imported above, from the real Explorium
    # candidate attributes) is what hard validation/scoring/quality all
    # just ran on; enrichment adds MORE data for a company worth pursuing
    # further, it was never required to reach a PASS/HOLD/FAIL verdict.
    if not _at_least(item, BatchItemStage.ENRICHED):
        if quality.label != CompanyQualityLabel.REJECT.value:
            enrich_company(company_id, registry=registry, db=db)
        _touch_stage(db, item, BatchItemStage.ENRICHED)

    person_id: str | None = None
    if not _at_least(item, BatchItemStage.PERSON_RESOLVED):
        if quality.label != CompanyQualityLabel.REJECT.value:
            try:
                people_result = start_people_discovery_run(
                    PeopleDiscoveryRunCreate(icp_id=icp_id, company_id=company_id, limit=people_limit), registry=registry, db=db
                )
                if people_result.candidates:
                    person_resolutions = resolve_people_discovery_run(
                        PersonResolveRequest(people_discovery_run_id=people_result.id), db=db
                    )
                    confirmed = [r for r in person_resolutions if r.status != ResolutionStatus.UNRESOLVED.value]
                    if confirmed:
                        person_id = confirmed[0].canonical_person_id
                        item.person_id = person_id
            except Exception:
                pass  # people discovery is best-effort; a company-only lead remains valid
        # A REJECT (hard-FAIL) company never reaches Unipile at all — this
        # is the concrete enforcement of "Unipile only after strong
        # companies": _touch_stage still records PERSON_RESOLVED so resume
        # never retries a person-discovery call this item was correctly
        # never meant to make.
        _touch_stage(db, item, BatchItemStage.PERSON_RESOLVED)
    else:
        person_id = item.person_id

    if person_id is not None:
        import_evidence(EvidenceImportRequest(entity_type=EvidenceEntityType.PERSON, entity_id=person_id), db=db)
        # A person was discovered after hard validation already ran
        # company-only; re-validate once more so the persisted hard-rule
        # result (and any person-scoped rule, e.g. allowed_titles) reflects
        # the person evidence too, exactly as validation always has.
        validation = create_validation(HardIcpValidationRequest(icp_id=icp_id, company_id=company_id, person_id=person_id), db=db)
        item.hard_rule_result = validation.overall_result

    llm_provider = get_llm_provider()
    qualification = create_lead_qualification(
        LeadQualificationRequest(icp_id=icp_id, company_id=company_id, person_id=person_id), db=db, provider=llm_provider
    )
    item.qualification_decision = qualification.decision
    _touch_stage(db, item, BatchItemStage.QUALIFIED)

    if qualification.status == QualificationExecutionStatus.SUCCESS.value:
        create_adversarial_review(AdversarialReviewRequest(qualification_id=qualification.id), db=db, provider=llm_provider)
    _touch_stage(db, item, BatchItemStage.ADVERSARIALLY_REVIEWED)

    # P2 fix: an item being RE-processed after _reopen_held_items_for_companies
    # (its own prior lead_id, if any, was deliberately preserved — see
    # that function's own docstring) will correctly re-match ITS OWN
    # existing lead here — a genuine MATCHED_EXISTING_LEAD by Phase 19's
    # own unmodified logic, but not a "duplicate candidate" the way that
    # decision means for every OTHER caller of this function (a
    # DIFFERENT candidate matching a lead created earlier — see
    # _classify_outcome's own comment). Captured BEFORE the call so this
    # only ever recognizes the item's own prior lead, never a
    # coincidentally-matching one from a genuinely different candidate.
    lead_id_before_this_pass = item.lead_id

    dedup = create_lead_deduplication(
        LeadDeduplicationRequest(
            icp_id=icp_id,
            company_id=company_id,
            person_id=person_id,
            source=LeadSourceReference(company_candidate_id=item.source_candidate_id),
        ),
        db=db,
    )
    item.lead_id = dedup.lead_id
    _touch_stage(db, item, BatchItemStage.DEDUPLICATED)

    is_self_match = (
        dedup.decision == LeadDeduplicationDecision.MATCHED_EXISTING_LEAD.value
        and lead_id_before_this_pass is not None
        and dedup.lead_id == lead_id_before_this_pass
    )
    _classify_outcome(item, validation, dedup, is_self_match=is_self_match)
    _touch_stage(db, item, BatchItemStage.DONE)


def _process_one_company(db: Session, registry: ProviderRegistry, item: BatchItemModel, icp_id: str, people_limit: int) -> None:
    """Advances one already-resolved candidate as far through the
    pipeline as it will go. Never raises: an unexpected exception here is
    caught and recorded as a FAILED outcome for this item only, so it can
    never abort the rest of the batch."""
    try:
        _advance_company_pipeline(db, registry, item, icp_id, people_limit)
        db.commit()
    except Exception as exc:
        db.rollback()
        fresh_item = db.get(BatchItemModel, item.id)
        _mark_failed(db, fresh_item, f"Unexpected error while processing this candidate: {exc}")
        db.commit()


def _finalize_counts(db: Session, batch: BatchModel) -> None:
    items = db.query(BatchItemModel).filter(BatchItemModel.batch_id == batch.id).all()
    batch.deduplicated_lead_count = len({i.lead_id for i in items if i.lead_id})
    batch.accepted_count = sum(1 for i in items if i.outcome == BatchItemOutcome.ACCEPTED.value)
    batch.held_count = sum(1 for i in items if i.outcome == BatchItemOutcome.HELD.value)
    batch.rejected_count = sum(1 for i in items if i.outcome == BatchItemOutcome.REJECTED.value)
    batch.duplicate_count = sum(1 for i in items if i.outcome == BatchItemOutcome.DUPLICATE.value)
    batch.failed_count = sum(1 for i in items if i.outcome == BatchItemOutcome.FAILED.value)
    batch.status = BatchStatus.COMPLETED_WITH_ERRORS.value if batch.failed_count else BatchStatus.COMPLETED.value
    batch.completed_at = datetime.now(timezone.utc)
    db.flush()


def run_batch(
    db: Session,
    registry: ProviderRegistry,
    batch: BatchModel,
    icp_id: str,
    max_new_discovery_rounds: int = 1,
    is_resume: bool = False,
) -> None:
    """The batch entry point — safe to call more than once for the same
    batch id (retry/resume): already-seeded items are reused, already-
    terminal items are skipped, and only genuinely unfinished work is
    (re)attempted. Never raises: every per-item failure is isolated by
    `_process_one_company`, so this function always drives the batch to a
    terminal status.

    `is_resume` is an explicit signal from the caller (see app/api/batch.py:
    create_batch never passes it, resume_batch always passes True) — Phase
    34 needs this rather than inferring "is this a resume call" from
    whether the batch already has items, because a batch whose very first
    round discovers zero candidates would otherwise never be distinguishable
    from a fresh create_batch call on a later resume, which would
    incorrectly and permanently block Hermes (app/providers/hermes.py) from
    ever being consulted for such a batch. See _maybe_advance_hermes_job's
    own allow_submit parameter, the only thing this flag currently gates.

    `max_new_discovery_rounds` bounds how many ADDITIONAL discovery rounds
    (beyond the initial seed, or beyond whatever this batch already had
    before this call) this one call may run before returning — this is
    the "Find More Leads" / target-count-continuation mechanism.
    Defaulting to 1 preserves the exact original behavior for the very
    first `create_batch` call (which never passes this argument): seeding
    for the first time always counts as using up the round budget, so the
    loop below never runs an extra round on that call unless a caller
    explicitly asks for more via a higher value. On a RESUME call, the
    batch already has items (the seed was "used up" by a prior call), so
    the full `max_new_discovery_rounds` budget is available for genuinely
    new rounds — this is what makes a bodyless resume behave exactly as
    before (default 1, one round attempted, matching today's contract)
    while a resume with a higher max_discovery_rounds can seed several new
    rounds in one call (see app/api/batch.py's resume endpoint). Existing
    results are always appended to, never replaced — a round that adds new
    BatchItemModel rows never touches or removes items from a prior round,
    and `_finalize_counts` recomputes totals across every item currently in
    the batch, prior and new alike."""
    batch.status = BatchStatus.RUNNING.value
    db.flush()

    had_existing_items = db.query(BatchItemModel).filter(BatchItemModel.batch_id == batch.id).limit(1).first() is not None
    items = _seed_items_from_discovery(db, registry, batch, icp_id)
    db.commit()

    _run_deep_prescreen_for_items(db, batch, icp_id, items)

    for item in items:
        _process_one_company(db, registry, item, icp_id, batch.people_limit_per_company)

    # The very first seed of a batch (this call created the batch's only
    # items) counts as having used one round of the budget — this is what
    # keeps create_batch's default max_new_discovery_rounds=1 a true no-op
    # extra round, exactly matching pre-existing behavior. A resume call
    # against an already-seeded batch did NOT just consume a round by
    # reusing existing items, so its full round budget is available for
    # genuinely new discovery rounds.
    rounds_used = 0 if had_existing_items else (1 if items else 0)
    while rounds_used < max_new_discovery_rounds and _should_run_another_discovery_round(db, batch):
        new_items = _run_one_discovery_round(db, registry, batch, icp_id)
        db.commit()
        rounds_used += 1
        if not new_items:
            # Nothing to advance this round (every candidate was already
            # seen with no new provenance to restore, or the provider(s)
            # returned nothing) — stop rather than burning the remaining
            # round budget on empty rounds. new_items includes any P2-fix
            # reopened HELD items (see _run_one_discovery_round's own
            # docstring), so a round whose ONLY effect was reopening one
            # is correctly NOT treated as empty here.
            break
        _run_deep_prescreen_for_items(db, batch, icp_id, new_items)
        for item in new_items:
            # A round is never truncated at discovery time (see
            # _seed_items_from_discovery's own docstring — only the first
            # seed round is), so a single round's raw candidate count is
            # bounded by batch.discovery_limit (up to 100), not by how many
            # more ACCEPTED leads this batch actually still needs. Without
            # this check, a round that discovers e.g. 20 candidates when
            # only 2 more ACCEPTED leads are needed would still run every
            # one of those 20 through the full pipeline (evidence import,
            # LLM qualification, enrichment) — real per-company provider
            # spend for leads nobody asked for. Stopping here as soon as
            # the target is met leaves any unprocessed items exactly as
            # _seed_items_from_discovery already leaves its own dropped
            # items: real, untouched BatchItemModel/DiscoveryCandidateModel
            # rows at BatchItemStage.DISCOVERED, never force-failed or
            # deleted, simply not advanced further in this call.
            if _accepted_count(db, batch.id) >= batch.requested_target_count:
                break
            _process_one_company(db, registry, item, icp_id, batch.people_limit_per_company)

    # Phase 34 — Hermes secondary discovery: consulted at most once per
    # run_batch call, ONLY after Explorium's own round loop above has
    # already run to completion for this call. allow_submit is gated on
    # the caller's own explicit is_resume signal: True on every RESUME
    # call (a job may be submitted), False on the very first create_batch
    # call (never submit here — a multi-minute Hermes job must never delay
    # the initial synchronous response). See _maybe_advance_hermes_job's
    # own docstring for the full submit-vs-check state machine, and
    # run_batch's own docstring for why is_resume (not had_existing_items)
    # is the correct signal.
    hermes_items = _maybe_advance_hermes_job(db, batch, icp_id, allow_submit=is_resume)
    db.commit()
    _run_deep_prescreen_for_items(db, batch, icp_id, hermes_items)
    for item in hermes_items:
        if _accepted_count(db, batch.id) >= batch.requested_target_count:
            break
        _process_one_company(db, registry, item, icp_id, batch.people_limit_per_company)

    _finalize_counts(db, batch)
    db.commit()
