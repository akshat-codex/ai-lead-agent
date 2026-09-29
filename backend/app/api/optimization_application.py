"""Phase 26 API — controlled optimization application.

Reuses Phase 25's `_build_result()` UNCHANGED to get the live, advisory
recommendation set — no second recommendation engine. This module adds
only the explicit human approve/apply/rollback workflow and the effective-
configuration read model, all persisted in Phase 26's own append-only
tables, completely separate from CanonicalICP, ScoringWeights, the
provider registry, and every historical Phase 4/15/22 row.

Rules enforced here (see app/services/optimization_application.py for the
pure logic): a recommendation must be explicitly APPROVED before it can be
applied; INSUFFICIENT_DATA confidence blocks approval outright; applying
an already-active (applied, not rolled back) fingerprint is rejected as a
duplicate; rolling back appends a new event rather than deleting the
original APPLIED row; nothing here ever writes to an ICP, evidence,
feedback, score, or ranking table.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.optimization import _build_result
from app.db.session import get_db
from app.models.icp import ICPModel
from app.models.optimization_application import OptimizationApplicationModel, OptimizationApprovalModel
from app.schemas.feedback_learning import ConfidenceLevel
from app.schemas.optimization_application import (
    ApplicationRead,
    ApplicationRequest,
    ApplicationStatus,
    ApprovalDecision,
    ApprovalRead,
    ApprovalRequest,
    EffectiveConfiguration,
    PendingRecommendation,
    RecommendationStatus,
    RollbackRequest,
)
from app.services.optimization_application import (
    build_effective_configuration,
    build_pending_recommendations,
    eligibility_for,
    recommendation_fingerprint,
)

router = APIRouter(prefix="/api/v1/optimization-applications", tags=["optimization-application"])


def _latest(rows: list) -> object | None:
    """Same tie-safe "latest row" pattern used by Phase 22/23/24/25 — see
    their own docstrings for why `.order_by(col.desc()).first()` is
    unsafe under SQLite's timestamp resolution."""
    return rows[-1] if rows else None


def _latest_approval(db: Session, fingerprint: str) -> OptimizationApprovalModel | None:
    rows = (
        db.query(OptimizationApprovalModel)
        .filter(OptimizationApprovalModel.fingerprint == fingerprint)
        .order_by(OptimizationApprovalModel.created_at)
        .all()
    )
    return _latest(rows)


def _latest_application_event(db: Session, fingerprint: str) -> OptimizationApplicationModel | None:
    """The latest event row for a fingerprint, ordered by whichever
    timestamp is most recent between apply and rollback — a rollback
    row's own `applied_at` mirrors its creation time, so sorting by that
    column alone already reflects true event order."""
    rows = (
        db.query(OptimizationApplicationModel)
        .filter(OptimizationApplicationModel.fingerprint == fingerprint)
        .order_by(OptimizationApplicationModel.applied_at)
        .all()
    )
    return _latest(rows)


def _status_for_fingerprint(db: Session, fingerprint: str) -> RecommendationStatus:
    application_event = _latest_application_event(db, fingerprint)
    if application_event is not None:
        return RecommendationStatus.APPLIED if application_event.status == "APPLIED" else RecommendationStatus.ROLLED_BACK

    approval = _latest_approval(db, fingerprint)
    if approval is not None:
        return RecommendationStatus.APPROVED if approval.decision == "APPROVED" else RecommendationStatus.REJECTED

    return RecommendationStatus.PENDING


@router.get("/pending", response_model=list[PendingRecommendation])
def list_pending_recommendations(
    icp_id: str = Query(..., min_length=1),
    min_sample_size: int = Query(default=5, ge=1),
    include_global_patterns: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[PendingRecommendation]:
    result = _build_result(db, icp_id, None, min_sample_size, include_global_patterns)

    status_by_fingerprint: dict[str, RecommendationStatus] = {}
    fingerprints = [
        recommendation_fingerprint(
            r.icp_id, r.icp_version, r.recommendation_type.value, r.signal_name, r.reason_code, r.is_global
        )
        for r in result.recommendations
    ]
    for fp in fingerprints:
        status_by_fingerprint[fp] = _status_for_fingerprint(db, fp)

    return list(build_pending_recommendations(result.recommendations, status_by_fingerprint))


@router.post("/approvals", response_model=ApprovalRead, status_code=201)
def submit_approval(payload: ApprovalRequest, db: Session = Depends(get_db)) -> OptimizationApprovalModel:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    if payload.decision == ApprovalDecision.APPROVED:
        is_eligible, reason = eligibility_for(payload.recommendation_type.value, payload.confidence)
        if not is_eligible:
            raise HTTPException(status_code=422, detail=f"recommendation cannot be approved: {reason}")

    row = OptimizationApprovalModel(
        id=str(uuid4()),
        fingerprint=payload.fingerprint,
        icp_id=payload.icp_id,
        icp_version=payload.icp_version,
        is_global=payload.is_global,
        decision=payload.decision.value,
        approved_by=payload.approved_by,
        note=payload.note,
        recommendation_type=payload.recommendation_type.value,
        signal_name=payload.signal_name,
        reason_code=payload.reason_code,
        sample_count=payload.sample_count,
        confidence=payload.confidence.value,
        expected_effect=payload.expected_effect.value,
        magnitude_hint=payload.magnitude_hint,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("", response_model=ApplicationRead, status_code=201)
def apply_recommendation(payload: ApplicationRequest, db: Session = Depends(get_db)) -> OptimizationApplicationModel:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    approval = _latest_approval(db, payload.fingerprint)
    if approval is None or approval.decision != "APPROVED":
        raise HTTPException(status_code=422, detail="fingerprint has not been approved; apply requires an explicit prior approval")

    is_eligible, reason = eligibility_for(approval.recommendation_type, ConfidenceLevel(approval.confidence))
    if not is_eligible:
        raise HTTPException(status_code=422, detail=f"recommendation cannot be applied: {reason}")

    latest_event = _latest_application_event(db, payload.fingerprint)
    if latest_event is not None and latest_event.status == "APPLIED":
        raise HTTPException(status_code=409, detail="this recommendation is already actively applied; roll it back before re-applying")

    row = OptimizationApplicationModel(
        id=str(uuid4()),
        fingerprint=payload.fingerprint,
        icp_id=payload.icp_id,
        icp_version=payload.icp_version,
        is_global=approval.is_global,
        status=ApplicationStatus.APPLIED.value,
        approval_id=approval.id,
        applied_by=payload.applied_by,
        apply_note=payload.note,
        rolled_back_by=None,
        rollback_note=None,
        recommendation_type=approval.recommendation_type,
        signal_name=approval.signal_name,
        reason_code=approval.reason_code,
        magnitude_hint=approval.magnitude_hint,
        expected_effect=approval.expected_effect,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/history", response_model=list[ApplicationRead])
def list_application_history(
    icp_id: str | None = Query(default=None),
    fingerprint: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[OptimizationApplicationModel]:
    if icp_id is None and fingerprint is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, fingerprint")

    query = db.query(OptimizationApplicationModel)
    if icp_id is not None:
        query = query.filter(OptimizationApplicationModel.icp_id == icp_id)
    if fingerprint is not None:
        query = query.filter(OptimizationApplicationModel.fingerprint == fingerprint)
    return query.order_by(OptimizationApplicationModel.applied_at).all()


@router.get("/history/{application_id}", response_model=ApplicationRead)
def get_application(application_id: str, db: Session = Depends(get_db)) -> OptimizationApplicationModel:
    row = db.get(OptimizationApplicationModel, application_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return row


@router.post("/{application_id}/rollback", response_model=ApplicationRead, status_code=201)
def rollback_application(application_id: str, payload: RollbackRequest, db: Session = Depends(get_db)) -> OptimizationApplicationModel:
    original = db.get(OptimizationApplicationModel, application_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Application not found")

    latest_event = _latest_application_event(db, original.fingerprint)
    if latest_event is None or latest_event.status != "APPLIED":
        raise HTTPException(status_code=409, detail="this recommendation is not currently active; nothing to roll back")

    row = OptimizationApplicationModel(
        id=str(uuid4()),
        fingerprint=original.fingerprint,
        icp_id=original.icp_id,
        icp_version=original.icp_version,
        is_global=original.is_global,
        status=ApplicationStatus.ROLLED_BACK.value,
        approval_id=original.approval_id,
        applied_by=original.applied_by,
        apply_note=original.apply_note,
        rolled_back_by=payload.rolled_back_by,
        rollback_note=payload.note,
        recommendation_type=original.recommendation_type,
        signal_name=original.signal_name,
        reason_code=original.reason_code,
        magnitude_hint=original.magnitude_hint,
        expected_effect=original.expected_effect,
        rolled_back_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/effective-config", response_model=EffectiveConfiguration)
def get_effective_configuration(
    icp_id: str = Query(..., min_length=1),
    icp_version: int = Query(...),
    db: Session = Depends(get_db),
) -> EffectiveConfiguration:
    icp_record = db.get(ICPModel, icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    # Every application row scoped to this ICP/version OR explicitly
    # global — never the reverse: an ICP-scoped application from a
    # DIFFERENT ICP/version is never pulled in, preserving multi-ICP
    # isolation unless a recommendation was itself marked global at
    # approval/application time.
    all_rows = (
        db.query(OptimizationApplicationModel)
        .filter(
            (
                (OptimizationApplicationModel.icp_id == icp_id) & (OptimizationApplicationModel.icp_version == icp_version)
            )
            | (OptimizationApplicationModel.is_global.is_(True))
        )
        .order_by(OptimizationApplicationModel.applied_at)
        .all()
    )

    latest_by_fingerprint: dict[str, OptimizationApplicationModel] = {}
    for row in all_rows:
        latest_by_fingerprint[row.fingerprint] = row  # ascending order -> last write per fingerprint wins

    active_applications = [
        {
            "id": row.id,
            "recommendation_type": row.recommendation_type,
            "signal_name": row.signal_name,
            "reason_code": row.reason_code,
            "magnitude_hint": row.magnitude_hint,
            "expected_effect": row.expected_effect,
            "applied_at": row.applied_at,
        }
        for row in latest_by_fingerprint.values()
        if row.status == "APPLIED"
    ]

    return build_effective_configuration(icp_id, icp_version, active_applications, datetime.now(timezone.utc))
