"""Phase 29 API — end-to-end pipeline integration.

Wraps Phase 21's own BatchModel/run_batch (unchanged) for Discovery
through Deduplication, then reuses Phase 22's _compute_ranking, Phase 23's
_build_export_result, and Phase 28's _build_result (confidence) exactly
as they already exist — no second scoring, qualification, identity,
evidence, ranking, or deduplication system is introduced here. Human
review stays a separate, manual endpoint (Phase 20); this module only
reports how many leads are awaiting one.

Synchronous, in-process, no new job/queue system - identical execution
model to Phase 21's own batch endpoint.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.export import _build_export_result
from app.api.lead_confidence import _build_result
from app.db.session import get_db
from app.models.batch import BatchModel
from app.models.icp import ICPModel
from app.models.pipeline import PipelineRunModel
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.export import ExportFormat
from app.schemas.lead_confidence import LeadConfidenceResult
from app.schemas.pipeline import PipelineRunCreate, PipelineRunDetail, PipelineRunRead
from app.services.lead_export import render_export
from app.services.pipeline_orchestration import run_pipeline

router = APIRouter(prefix="/api/v1/pipeline-runs", tags=["pipeline"])


def _to_detail(run: PipelineRunModel) -> PipelineRunDetail:
    return PipelineRunDetail(
        id=run.id,
        icp_id=run.icp_id,
        icp_version=run.icp_version,
        batch_id=run.batch_id,
        status=run.status,
        lead_count=run.lead_count,
        accepted_count=run.accepted_count,
        held_count=run.held_count,
        rejected_count=run.rejected_count,
        duplicate_count=run.duplicate_count,
        failed_count=run.failed_count,
        human_review_pending_count=run.human_review_pending_count,
        started_at=run.started_at,
        completed_at=run.completed_at,
        stage_statuses=tuple(run.stage_statuses or []),
        leads=tuple(run.leads or []),
    )


@router.post("", response_model=PipelineRunDetail, status_code=201)
def create_pipeline_run(
    payload: PipelineRunCreate,
    db: Session = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> PipelineRunDetail:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    batch = BatchModel(
        id=str(uuid4()),
        icp_id=icp_record.id,
        icp_version=icp_record.version,
        requested_target_count=payload.target_count,
        discovery_limit=payload.discovery_limit,
        people_limit_per_company=payload.people_limit_per_company,
        status="PENDING",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    run = PipelineRunModel(
        id=str(uuid4()),
        icp_id=icp_record.id,
        icp_version=icp_record.version,
        batch_id=batch.id,
        status="PENDING",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    run_pipeline(db, registry, run, batch)
    return _to_detail(run)


@router.post("/{run_id}/resume", response_model=PipelineRunDetail)
def resume_pipeline_run(
    run_id: str,
    db: Session = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> PipelineRunDetail:
    """Safe to call on any run, including one already COMPLETED — it
    delegates to Phase 21's own resume-safe run_batch(), so already-done
    work is skipped rather than redone or duplicated, and the downstream
    summary is simply recomputed from whatever is now persisted."""
    run = db.get(PipelineRunModel, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    batch = db.get(BatchModel, run.batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Underlying batch not found")

    run_pipeline(db, registry, run, batch)
    return _to_detail(run)


@router.get("/{run_id}", response_model=PipelineRunDetail)
def get_pipeline_run(run_id: str, db: Session = Depends(get_db)) -> PipelineRunDetail:
    run = db.get(PipelineRunModel, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return _to_detail(run)


@router.get("", response_model=list[PipelineRunRead])
def list_pipeline_runs(
    icp_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[PipelineRunModel]:
    query = db.query(PipelineRunModel)
    if icp_id is not None:
        query = query.filter(PipelineRunModel.icp_id == icp_id)
    return query.order_by(PipelineRunModel.started_at).all()


@router.get("/{run_id}/confidence", response_model=list[LeadConfidenceResult])
def get_pipeline_run_confidence(run_id: str, db: Session = Depends(get_db)) -> list[LeadConfidenceResult]:
    """Recomputes Phase 28 confidence live for every lead this run
    produced, rather than trusting the JSON snapshot stored on the run row
    — this always reflects the current state of the underlying evidence
    and pipeline rows."""
    run = db.get(PipelineRunModel, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    return [
        _build_result(db, run.icp_id, lead["company_id"], lead["person_id"])
        for lead in (run.leads or [])
    ]


@router.get("/{run_id}/export")
def get_pipeline_run_export(
    run_id: str,
    format: ExportFormat = Query(default=ExportFormat.JSON),
    db: Session = Depends(get_db),
):
    run = db.get(PipelineRunModel, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    result = _build_export_result(db, run.icp_id, run.batch_id)
    body = render_export(result, format)

    if format == ExportFormat.CSV:
        return Response(content=body, media_type="text/csv")
    return Response(content=body, media_type="application/json")
