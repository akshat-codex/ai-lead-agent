"""Phase 24 API — feedback learning analysis.

Loads Phase 4 feedback rows UNCHANGED (no second feedback schema), resolves
each `lead_ref` to a real Phase 19 canonical lead when possible, and looks
up the latest Phase 12 (hard validation)/13 (business model)/14 (commercial
signal) rows for that lead's company as its correlated "signals" bundle.
Calls the unchanged, pure app/services/feedback_learning.analyze_feedback().

Never mutates any feedback, ICP, evidence, classification, signal, score,
or lead row it reads — this endpoint only computes descriptive statistics
and optionally persists an append-only snapshot of that computation. It
never writes a hard-rule result, a score, or a qualification decision, and
it never resolves ACCEPT/REJECT for anything.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.business_model import BusinessModelClassificationModel
from app.models.commercial_signal import CommercialSignalModel
from app.models.feedback import FeedbackModel
from app.models.feedback_learning import FeedbackLearningSnapshotModel
from app.models.hard_icp_validation import HardIcpValidationModel
from app.models.icp import ICPModel
from app.models.lead import CanonicalLeadModel
from app.schemas.feedback import FeedbackDecision
from app.schemas.feedback_learning import FeedbackLearningResult, FeedbackLearningSnapshotRead
from app.services.feedback_learning import FeedbackObservation, analyze_feedback

router = APIRouter(prefix="/api/v1/feedback-learning", tags=["feedback-learning"])


def _last(rows: list) -> object | None:
    """Same tie-safe "latest row" pattern used by Phase 22/23 — see their
    own docstrings for why `.order_by(col.desc()).first()` is unsafe under
    SQLite's timestamp resolution when several rows share a moment."""
    return rows[-1] if rows else None


def _latest_hard_validation(db: Session, icp_id: str, company_id: str) -> HardIcpValidationModel | None:
    rows = (
        db.query(HardIcpValidationModel)
        .filter(HardIcpValidationModel.icp_id == icp_id, HardIcpValidationModel.company_id == company_id)
        .order_by(HardIcpValidationModel.validated_at)
        .all()
    )
    return _last(rows)


def _latest_business_model(db: Session, company_id: str) -> BusinessModelClassificationModel | None:
    rows = (
        db.query(BusinessModelClassificationModel)
        .filter(BusinessModelClassificationModel.company_id == company_id)
        .order_by(BusinessModelClassificationModel.classified_at)
        .all()
    )
    return _last(rows)


def _latest_commercial_signals(db: Session, company_id: str) -> list[CommercialSignalModel]:
    rows = (
        db.query(CommercialSignalModel)
        .filter(CommercialSignalModel.company_id == company_id)
        .order_by(CommercialSignalModel.created_at)
        .all()
    )
    latest_by_type: dict[str, CommercialSignalModel] = {}
    for row in rows:
        latest_by_type[row.signal_type] = row  # ascending order -> last write per type wins, matches _last()
    return list(latest_by_type.values())


def _signals_for_lead(db: Session, icp_id: str, company_id: str) -> frozenset[str]:
    """Builds the "namespace:value" signal set for one lead's company —
    every entry traces back to a real Phase 12/13/14 row; nothing here is
    invented. Signals with an unhelpful/unknown value are simply omitted,
    never replaced with a guessed one."""
    signals: set[str] = set()

    hard_validation = _latest_hard_validation(db, icp_id, company_id)
    if hard_validation is not None:
        signals.add(f"hard_rule_result:{hard_validation.overall_result}")

    business_model = _latest_business_model(db, company_id)
    if business_model is not None and business_model.status == "CLASSIFIED":
        signals.add(f"business_model:{business_model.primary_model}")
        for secondary in business_model.secondary_models:
            signals.add(f"business_model:{secondary}")

    for signal_row in _latest_commercial_signals(db, company_id):
        if signal_row.status == "SUPPORTED":
            signals.add(f"commercial_signal:{signal_row.signal_type}")

    return frozenset(signals)


def _build_observations(db: Session, feedback_rows: list[FeedbackModel]) -> list[FeedbackObservation]:
    observations: list[FeedbackObservation] = []
    for row in feedback_rows:
        lead_row = db.get(CanonicalLeadModel, row.lead_ref)
        signals: frozenset[str] = frozenset()
        if lead_row is not None:
            # lead_ref resolves to a real Phase 19 canonical lead -> we can
            # look up real correlated signals. A lead_ref that does not
            # resolve (an older, pre-Phase-19 opaque reference) still
            # contributes its decision/reason-code counts, just with no
            # signal correlation — never fabricated to force a match.
            signals = _signals_for_lead(db, row.icp_id, lead_row.company_id)
        observations.append(
            FeedbackObservation(
                decision=FeedbackDecision(row.decision),
                reason_codes=tuple(row.reason_codes),
                signals=signals,
            )
        )
    return observations


def _run_analysis(db: Session, icp_id: str | None, min_sample_size: int) -> FeedbackLearningResult:
    query = db.query(FeedbackModel)
    icp_version: int | None = None
    if icp_id is not None:
        icp_record = db.get(ICPModel, icp_id)
        if icp_record is None:
            raise HTTPException(status_code=404, detail="ICP not found")
        icp_version = icp_record.version
        query = query.filter(FeedbackModel.icp_id == icp_id)

    feedback_rows = query.order_by(FeedbackModel.created_at).all()
    observations = _build_observations(db, feedback_rows)
    return analyze_feedback(icp_id, icp_version, observations, min_sample_size)


@router.get("", response_model=FeedbackLearningResult)
def get_feedback_learning(
    icp_id: str | None = Query(default=None),
    min_sample_size: int = Query(default=5, ge=1),
    db: Session = Depends(get_db),
) -> FeedbackLearningResult:
    return _run_analysis(db, icp_id, min_sample_size)


@router.post("/snapshots", response_model=FeedbackLearningSnapshotRead, status_code=201)
def create_feedback_learning_snapshot(
    icp_id: str | None = Query(default=None),
    min_sample_size: int = Query(default=5, ge=1),
    db: Session = Depends(get_db),
) -> FeedbackLearningSnapshotModel:
    result = _run_analysis(db, icp_id, min_sample_size)

    row = FeedbackLearningSnapshotModel(
        id=str(uuid4()),
        icp_id=result.scope.icp_id,
        icp_version=result.scope.icp_version,
        min_sample_size=result.min_sample_size,
        total_feedback_count=result.decision_counts.total,
        overall_confidence=result.overall_confidence.value,
        result=result.model_dump(mode="json"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/snapshots/{snapshot_id}", response_model=FeedbackLearningSnapshotRead)
def get_feedback_learning_snapshot(snapshot_id: str, db: Session = Depends(get_db)) -> FeedbackLearningSnapshotModel:
    row = db.get(FeedbackLearningSnapshotModel, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Feedback learning snapshot not found")
    return row


@router.get("/snapshots", response_model=list[FeedbackLearningSnapshotRead])
def list_feedback_learning_snapshots(
    icp_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[FeedbackLearningSnapshotModel]:
    query = db.query(FeedbackLearningSnapshotModel)
    if icp_id is not None:
        query = query.filter(FeedbackLearningSnapshotModel.icp_id == icp_id)
    return query.order_by(FeedbackLearningSnapshotModel.created_at).all()
