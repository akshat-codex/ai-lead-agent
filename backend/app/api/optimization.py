"""Phase 25 API — feedback-driven optimization.

Reuses Phase 24's `_run_analysis()` and Phase 22's `_compute_ranking()`
UNCHANGED — no second learning or ranking implementation. Builds the
per-lead business-model/commercial-signal membership Phase 25's preview
needs (beyond what RankedLeadSignals already carries) from the same
Phase 13/14 rows Phase 24 itself reads, then calls the pure
app/services/feedback_optimization.build_optimization_result().

Never mutates any ICP, feedback, evidence, classification, signal, score,
ranking, or lead row it reads — this endpoint only computes advisory
recommendations and a read-only ranking preview, and optionally persists
an append-only snapshot of that computation. Nothing here ever writes a
hard-rule result, a score, a qualification decision, or an ICP field.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.feedback_learning import _run_analysis
from app.api.ranking import _compute_ranking
from app.db.session import get_db
from app.models.business_model import BusinessModelClassificationModel
from app.models.commercial_signal import CommercialSignalModel
from app.models.icp import ICPModel
from app.models.optimization import OptimizationSnapshotModel
from app.schemas.optimization import OptimizationResult, OptimizationSnapshotRead
from app.services.feedback_optimization import build_optimization_result

router = APIRouter(prefix="/api/v1/optimization", tags=["optimization"])


def _latest(rows: list) -> object | None:
    """Same tie-safe "latest row" pattern used by Phase 22/23/24 — see
    their own docstrings for why `.order_by(col.desc()).first()` is
    unsafe under SQLite's timestamp resolution."""
    return rows[-1] if rows else None


def _lead_extra_signals(db: Session, ranking_result) -> dict[str, frozenset[str]]:
    """Maps lead_id -> the business-model/commercial-signal membership
    for that lead's company, read from the exact same Phase 13/14 rows
    Phase 24 itself correlates against — never a second derivation."""
    extra: dict[str, frozenset[str]] = {}
    for ranked_lead in ranking_result.ranked_leads:
        names: set[str] = set()

        bm_rows = (
            db.query(BusinessModelClassificationModel)
            .filter(BusinessModelClassificationModel.company_id == ranked_lead.company_id)
            .order_by(BusinessModelClassificationModel.classified_at)
            .all()
        )
        latest_bm = _latest(bm_rows)
        if latest_bm is not None and latest_bm.status == "CLASSIFIED":
            names.add(f"business_model:{latest_bm.primary_model}")
            for secondary in latest_bm.secondary_models:
                names.add(f"business_model:{secondary}")

        signal_rows = (
            db.query(CommercialSignalModel)
            .filter(CommercialSignalModel.company_id == ranked_lead.company_id)
            .order_by(CommercialSignalModel.created_at)
            .all()
        )
        latest_by_type: dict[str, CommercialSignalModel] = {}
        for row in signal_rows:
            latest_by_type[row.signal_type] = row
        for row in latest_by_type.values():
            if row.status == "SUPPORTED":
                names.add(f"commercial_signal:{row.signal_type}")

        if names:
            extra[ranked_lead.lead_id] = frozenset(names)
    return extra


def _build_result(db: Session, icp_id: str, batch_id: str | None, min_sample_size: int, include_global_patterns: bool) -> OptimizationResult:
    icp_record = db.get(ICPModel, icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    learning_scope_icp_id = None if include_global_patterns else icp_id
    learning = _run_analysis(db, learning_scope_icp_id, min_sample_size)
    ranking_result = _compute_ranking(db, icp_id, batch_id)
    lead_extra_signals = _lead_extra_signals(db, ranking_result)

    return build_optimization_result(
        learning=learning,
        ranking=ranking_result,
        icp_id=icp_id,
        icp_version=icp_record.version,
        min_sample_size=min_sample_size,
        generated_at=datetime.now(timezone.utc),
        learning_snapshot_id=None,
        include_global_patterns=include_global_patterns,
        lead_extra_signals=lead_extra_signals,
    )


@router.get("", response_model=OptimizationResult)
def get_optimization(
    icp_id: str = Query(..., min_length=1),
    batch_id: str | None = Query(default=None),
    min_sample_size: int = Query(default=5, ge=1),
    include_global_patterns: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> OptimizationResult:
    return _build_result(db, icp_id, batch_id, min_sample_size, include_global_patterns)


@router.post("/snapshots", response_model=OptimizationSnapshotRead, status_code=201)
def create_optimization_snapshot(
    icp_id: str = Query(..., min_length=1),
    batch_id: str | None = Query(default=None),
    min_sample_size: int = Query(default=5, ge=1),
    include_global_patterns: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> OptimizationSnapshotModel:
    result = _build_result(db, icp_id, batch_id, min_sample_size, include_global_patterns)

    row = OptimizationSnapshotModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        min_sample_size=result.min_sample_size,
        is_cold_start=result.is_cold_start,
        recommendation_count=len(result.recommendations),
        result=result.model_dump(mode="json"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/snapshots/{snapshot_id}", response_model=OptimizationSnapshotRead)
def get_optimization_snapshot(snapshot_id: str, db: Session = Depends(get_db)) -> OptimizationSnapshotModel:
    row = db.get(OptimizationSnapshotModel, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Optimization snapshot not found")
    return row


@router.get("/snapshots", response_model=list[OptimizationSnapshotRead])
def list_optimization_snapshots(
    icp_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[OptimizationSnapshotModel]:
    query = db.query(OptimizationSnapshotModel)
    if icp_id is not None:
        query = query.filter(OptimizationSnapshotModel.icp_id == icp_id)
    return query.order_by(OptimizationSnapshotModel.created_at).all()
