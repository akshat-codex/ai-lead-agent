"""Phase 29 — end-to-end pipeline orchestration.

Chains ICP -> Discovery -> Company Resolution -> Enrichment -> People
Discovery/Resolution -> Evidence -> Hard Validation -> Verification ->
Business Model/Signals -> Scoring -> LLM Qualification -> Adversarial
Review -> Deduplication -> Human Review -> Ranking -> Confidence ->
Export by calling each existing phase's own unchanged entry point. This
module does not reimplement any stage:

- Discovery..Deduplication: delegates entirely to Phase 21's
  app.services.batch_orchestration.run_batch(), unchanged. Provider
  routing (Phase 27) and optimization application (Phase 26) are already
  applied automatically inside that call because run_batch itself calls
  the same discovery/people-discovery API functions that already invoke
  compute_routing() internally.
- Human Review: never auto-invoked (it is an explicit human decision,
  Phase 20's own contract) - this module only reports how many leads are
  still awaiting one.
- Ranking: app.api.ranking._compute_ranking(), unchanged.
- Confidence: app.api.lead_confidence._build_result(), unchanged.
- Export: app.api.export._build_export_result(), unchanged.

A hard FAIL/HOLD can never be rescued here because none of the reused
functions ever rescues one - this module only reads their outputs.

Export webhook (additive, opt-in — see app/services/export_webhook.py's own
module docstring): once the run's own status/summary is fully finalized and
committed, the same export JSON is optionally POSTed to
settings.export_webhook_url, HMAC-signed. No-op (not even attempted) when
that setting is unset, which is the default — every pipeline run behaves
exactly as before this was added unless a user explicitly configures it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.api.export import _build_export_result
from app.api.lead_confidence import _build_result
from app.api.ranking import _compute_ranking, _latest_human_review
from app.core.config import get_settings
from app.models.batch import BatchItemModel, BatchModel
from app.models.lead import CanonicalLeadModel, LeadIcpMembershipModel
from app.models.pipeline import PipelineRunModel
from app.schemas.pipeline import (
    PipelineLeadStatus,
    PipelineStageName,
    PipelineStageStatus,
)
from app.services.batch_orchestration import run_batch
from app.services.export_webhook import send_export_webhook

_BATCH_STAGE_NAMES = (
    PipelineStageName.DISCOVERY,
    PipelineStageName.COMPANY_RESOLUTION,
    PipelineStageName.ENRICHMENT,
    PipelineStageName.PEOPLE_DISCOVERY_RESOLUTION,
    PipelineStageName.EVIDENCE,
    PipelineStageName.HARD_VALIDATION,
    PipelineStageName.VERIFICATION,
    PipelineStageName.BUSINESS_MODEL_SIGNALS,
    PipelineStageName.SCORING,
    PipelineStageName.LLM_QUALIFICATION,
    PipelineStageName.ADVERSARIAL_REVIEW,
    PipelineStageName.DEDUPLICATION,
)


def _leads_for_icp(db, icp_id: str) -> list[CanonicalLeadModel]:
    memberships = db.query(LeadIcpMembershipModel).filter(LeadIcpMembershipModel.icp_id == icp_id).all()
    leads: list[CanonicalLeadModel] = []
    seen: set[str] = set()
    for membership in memberships:
        if membership.lead_id in seen:
            continue
        lead_row = db.get(CanonicalLeadModel, membership.lead_id)
        if lead_row is None:
            continue
        seen.add(membership.lead_id)
        leads.append(lead_row)
    return leads


def _batch_stage_statuses(db, batch: BatchModel) -> list[PipelineStageStatus]:
    """Derives one PipelineStageStatus per advertised batch-covered stage
    from the already-persisted BatchItemModel rows for this batch — it does
    not re-run or reinterpret any stage rule, only counts how far each item
    got and whether it ended in FAILED."""
    items = db.query(BatchItemModel).filter(BatchItemModel.batch_id == batch.id).all()
    total = len(items)
    failed = sum(1 for item in items if item.outcome == "FAILED")

    statuses: list[PipelineStageStatus] = []
    for stage_name in _BATCH_STAGE_NAMES:
        statuses.append(
            PipelineStageStatus(
                stage=stage_name,
                attempted=total,
                succeeded=total - failed,
                failed=failed,
                pending=0,
                note="Derived from Phase 21 batch item outcomes (per-item stage detail lives on each BatchItemModel row).",
            )
        )
    return statuses


def _lead_status(db, icp_id: str, lead_row: CanonicalLeadModel, batch: BatchModel) -> PipelineLeadStatus:
    batch_item = (
        db.query(BatchItemModel)
        .filter(BatchItemModel.batch_id == batch.id, BatchItemModel.lead_id == lead_row.id)
        .one_or_none()
    )
    confidence = _build_result(db, icp_id, lead_row.company_id, lead_row.person_id)
    human_review = _latest_human_review(db, icp_id, lead_row.id)

    hard_rule_result = confidence.hard_rule_result
    human_review_pending = human_review is None and hard_rule_result == "PASS"

    ranking_tier: str | None = None
    ranking_result = _compute_ranking(db, icp_id, batch.id)
    for ranked_lead in ranking_result.ranked_leads:
        if ranked_lead.lead_id == lead_row.id:
            ranking_tier = ranked_lead.tier.value
            break

    return PipelineLeadStatus(
        lead_id=lead_row.id,
        company_id=lead_row.company_id,
        person_id=lead_row.person_id,
        batch_outcome=batch_item.outcome if batch_item is not None else None,
        hard_rule_result=hard_rule_result,
        readiness=confidence.readiness.value,
        ranking_tier=ranking_tier,
        human_review_decision=human_review.effective_decision if human_review is not None else None,
        human_review_pending=human_review_pending,
    )


def run_pipeline(db, registry, run: PipelineRunModel, batch: BatchModel) -> None:
    """Advances the Phase 21 batch (discovery..dedup, unchanged) then
    recomputes the downstream read-only summary (human-review pending
    count, ranking tier, Phase 28 confidence) for every lead now under the
    ICP. Safe to call repeatedly on the same run/batch pair: run_batch
    itself skips already-completed batch items, and every downstream call
    here is a pure read + recompute with no side effects of its own beyond
    overwriting this run's own summary row."""
    run.status = "RUNNING"
    db.add(run)
    db.commit()

    run_batch(db, registry, batch, run.icp_id)
    db.refresh(batch)

    stage_statuses = _batch_stage_statuses(db, batch)

    lead_rows = _leads_for_icp(db, run.icp_id)
    lead_statuses = [_lead_status(db, run.icp_id, lead_row, batch) for lead_row in lead_rows]

    human_review_pending_count = sum(1 for status in lead_statuses if status.human_review_pending)

    stage_statuses.append(
        PipelineStageStatus(
            stage=PipelineStageName.HUMAN_REVIEW,
            attempted=len(lead_statuses),
            succeeded=sum(1 for status in lead_statuses if status.human_review_decision is not None),
            failed=0,
            pending=human_review_pending_count,
            note="Human review is a manual decision (Phase 20) and is never submitted automatically by the pipeline.",
        )
    )
    stage_statuses.append(
        PipelineStageStatus(
            stage=PipelineStageName.RANKING,
            attempted=len(lead_statuses),
            succeeded=sum(1 for status in lead_statuses if status.ranking_tier is not None),
            failed=0,
        )
    )
    stage_statuses.append(
        PipelineStageStatus(
            stage=PipelineStageName.CONFIDENCE,
            attempted=len(lead_statuses),
            succeeded=len(lead_statuses),
            failed=0,
        )
    )
    stage_statuses.append(
        PipelineStageStatus(
            stage=PipelineStageName.EXPORT,
            attempted=1,
            succeeded=1,
            failed=0,
            note="Export is computed on demand via GET /api/v1/pipeline-runs/{id}/export, not persisted per run.",
        )
    )

    run.lead_count = len(lead_statuses)
    run.accepted_count = sum(1 for status in lead_statuses if status.batch_outcome == "ACCEPTED")
    run.held_count = sum(1 for status in lead_statuses if status.batch_outcome == "HELD")
    run.rejected_count = sum(1 for status in lead_statuses if status.batch_outcome == "REJECTED")
    run.duplicate_count = sum(1 for status in lead_statuses if status.batch_outcome == "DUPLICATE")
    run.failed_count = batch.failed_count
    run.human_review_pending_count = human_review_pending_count
    run.stage_statuses = [status.model_dump(mode="json") for status in stage_statuses]
    run.leads = [status.model_dump(mode="json") for status in lead_statuses]
    run.status = "COMPLETED_WITH_ERRORS" if batch.failed_count > 0 else "COMPLETED"
    run.completed_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)

    # Export webhook (see app/services/export_webhook.py's own module
    # docstring) — fires once per run, after the run row is fully
    # finalized and committed, so a receiver's payload always reflects the
    # exact same data GET .../export would return if fetched right now.
    # Never raises and never affects run.status: a delivery failure is
    # purely observability (logged inside send_export_webhook itself), not
    # a pipeline failure — the run already succeeded or failed on its own
    # terms before this ever runs.
    settings = get_settings()
    if settings.export_webhook_url:
        export_result = _build_export_result(db, run.icp_id, run.batch_id)
        send_export_webhook(
            export_result,
            webhook_url=settings.export_webhook_url,
            webhook_secret=settings.export_webhook_secret,
            timeout_seconds=settings.export_webhook_timeout_seconds,
        )
