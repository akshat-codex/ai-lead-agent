"""Phase 28 API — evidence-backed lead confidence/readiness.

Reuses, UNCHANGED and imported directly (the same in-process pattern
established by Phase 22-27): app/api/human_review._assemble_snapshot()
for the Phase 11/12/15/16/17/18 PipelineSnapshot, app/api/ranking's own
_latest_human_review()/_latest_batch_item() for Phase 20/21 facts, and
Phase 19's CanonicalLeadModel for identity. No second snapshot builder, no
second hard-rule/scoring/qualification/ranking implementation anywhere in
this file.

Never mutates any ICP, evidence, feedback, score, qualification,
adversarial-review, verification, lead, human-review, or batch row it
reads — this endpoint only computes a readiness classification and
optionally persists an append-only snapshot of that computation.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.human_review import _assemble_snapshot, _load_evidence
from app.api.ranking import _latest_batch_item, _latest_human_review
from app.db.session import get_db
from app.models.icp import ICPModel
from app.models.lead import CanonicalLeadModel
from app.models.lead_confidence import LeadConfidenceSnapshotModel
from app.schemas.evidence import EntityType
from app.schemas.lead_confidence import LeadConfidenceResult, LeadConfidenceSnapshotRead
from app.services.lead_confidence import classify_readiness

router = APIRouter(prefix="/api/v1/lead-confidence", tags=["lead-confidence"])


def _find_lead(db: Session, company_id: str, person_id: str | None) -> CanonicalLeadModel | None:
    query = db.query(CanonicalLeadModel).filter(CanonicalLeadModel.company_id == company_id)
    query = query.filter(CanonicalLeadModel.person_id == person_id) if person_id else query.filter(CanonicalLeadModel.person_id.is_(None))
    return query.one_or_none()


def _build_result(db: Session, icp_id: str, company_id: str, person_id: str | None) -> LeadConfidenceResult:
    icp_record = db.get(ICPModel, icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    snapshot = _assemble_snapshot(db, icp_id, company_id, person_id)
    company_evidence = _load_evidence(db, EntityType.COMPANY, company_id)
    person_evidence = _load_evidence(db, EntityType.PERSON, person_id) if person_id else []

    lead_row = _find_lead(db, company_id, person_id)
    lead_id = lead_row.id if lead_row is not None else None

    human_review_decision = None
    is_duplicate_occurrence = False
    ranking_tier = None
    if lead_id is not None:
        human_review = _latest_human_review(db, icp_id, lead_id)
        human_review_decision = human_review.effective_decision if human_review is not None else None

        batch_item = _latest_batch_item(db, lead_id, None)
        is_duplicate_occurrence = batch_item is not None and batch_item.outcome == "DUPLICATE"

    return classify_readiness(
        icp_id=icp_id,
        icp_version=icp_record.version,
        snapshot=snapshot,
        lead_id=lead_id,
        company_evidence=company_evidence,
        person_evidence=person_evidence,
        human_review_decision=human_review_decision,
        is_duplicate_occurrence=is_duplicate_occurrence,
        ranking_tier=ranking_tier,
        generated_at=datetime.now(timezone.utc),
    )


@router.get("", response_model=LeadConfidenceResult)
def get_lead_confidence(
    icp_id: str = Query(..., min_length=1),
    company_id: str = Query(..., min_length=1),
    person_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> LeadConfidenceResult:
    return _build_result(db, icp_id, company_id, person_id)


@router.post("/snapshots", response_model=LeadConfidenceSnapshotRead, status_code=201)
def create_lead_confidence_snapshot(
    icp_id: str = Query(..., min_length=1),
    company_id: str = Query(..., min_length=1),
    person_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> LeadConfidenceSnapshotModel:
    result = _build_result(db, icp_id, company_id, person_id)

    row = LeadConfidenceSnapshotModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        lead_id=result.lead_id,
        readiness=result.readiness.value,
        result=result.model_dump(mode="json"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/snapshots/{snapshot_id}", response_model=LeadConfidenceSnapshotRead)
def get_lead_confidence_snapshot(snapshot_id: str, db: Session = Depends(get_db)) -> LeadConfidenceSnapshotModel:
    row = db.get(LeadConfidenceSnapshotModel, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Lead confidence snapshot not found")
    return row


@router.get("/snapshots", response_model=list[LeadConfidenceSnapshotRead])
def list_lead_confidence_snapshots(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[LeadConfidenceSnapshotModel]:
    if icp_id is None and company_id is None and lead_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id, lead_id")

    query = db.query(LeadConfidenceSnapshotModel)
    if icp_id is not None:
        query = query.filter(LeadConfidenceSnapshotModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(LeadConfidenceSnapshotModel.company_id == company_id)
    if lead_id is not None:
        query = query.filter(LeadConfidenceSnapshotModel.lead_id == lead_id)
    return query.order_by(LeadConfidenceSnapshotModel.created_at).all()
