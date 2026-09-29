from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.feedback import FeedbackModel
from app.models.icp import ICPModel
from app.schemas.feedback import FeedbackCreate, FeedbackRead

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackRead, status_code=201)
def submit_feedback(payload: FeedbackCreate, db: Session = Depends(get_db)) -> FeedbackModel:
    """Records a manager's decision about a lead.

    Always inserts a new row — feedback is append-only and immutable, so a
    later decision about the same lead never overwrites an earlier one.
    icp_version is copied from the referenced ICP now, not supplied by the
    caller, so it can never drift from what icp_id actually pointed to.
    """
    icp = db.get(ICPModel, payload.icp_id)
    if icp is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    record = FeedbackModel(
        id=str(uuid4()),
        lead_ref=payload.lead_ref,
        icp_id=icp.id,
        icp_version=icp.version,
        decision=payload.decision.value,
        reason_codes=payload.reason_codes,
        reviewer_note=payload.reviewer_note,
        reviewer_id=payload.reviewer_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("", response_model=list[FeedbackRead])
def list_feedback(
    lead_ref: str | None = Query(default=None),
    icp_id: str | None = Query(default=None),
    icp_version: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[FeedbackModel]:
    """Retrieves feedback for a lead and/or an ICP (optionally a specific version)."""
    if lead_ref is None and icp_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of lead_ref or icp_id")
    if icp_version is not None and icp_id is None:
        raise HTTPException(status_code=400, detail="icp_version requires icp_id")

    query = db.query(FeedbackModel)
    if lead_ref is not None:
        query = query.filter(FeedbackModel.lead_ref == lead_ref)
    if icp_id is not None:
        query = query.filter(FeedbackModel.icp_id == icp_id)
    if icp_version is not None:
        query = query.filter(FeedbackModel.icp_version == icp_version)

    return query.order_by(FeedbackModel.created_at).all()


@router.get("/{feedback_id}", response_model=FeedbackRead)
def get_feedback(feedback_id: str, db: Session = Depends(get_db)) -> FeedbackModel:
    record = db.get(FeedbackModel, feedback_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return record
