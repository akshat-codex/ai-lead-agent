"""Phase 20 API — human review.

Loads a Phase 19 canonical lead plus the latest Phase 11/12/15/16/17/18
rows for its (company, person), builds a compact PipelineSnapshot, runs
the deterministic review decision service, forwards ACCEPT/REJECT/HOLD
through the UNCHANGED Phase 4 feedback contract (never a second feedback
system), and persists an append-only HumanReviewModel row. Never mutates
the ICP, evidence, score, qualification, adversarial-review, or
verification rows it reads.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.adversarial_review import AdversarialReviewModel
from app.models.evidence import EvidenceModel
from app.models.feedback import FeedbackModel
from app.models.field_verification import FieldVerificationModel
from app.models.hard_icp_validation import HardIcpValidationModel
from app.models.human_review import HumanReviewModel
from app.models.icp import ICPModel
from app.models.lead import CanonicalLeadModel, LeadIcpMembershipModel
from app.models.llm_qualification import LLMQualificationModel
from app.models.scoring import LeadScoreModel
from app.schemas.evidence import EntityType, EvidenceRecord
from app.schemas.feedback import FeedbackCreate
from app.schemas.human_review import (
    ReviewDecision,
    ReviewQueueItem,
    ReviewRead,
    ReviewRequest,
    feedback_decision_for,
)
from app.services.human_review import decide_review
from app.services.review_snapshot import build_pipeline_snapshot

router = APIRouter(prefix="/api/v1/human-reviews", tags=["human-review"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


def _latest_hard_validation(db: Session, icp_id: str, company_id: str, person_id: str | None) -> dict | None:
    query = db.query(HardIcpValidationModel).filter(
        HardIcpValidationModel.icp_id == icp_id, HardIcpValidationModel.company_id == company_id
    )
    query = query.filter(HardIcpValidationModel.person_id == person_id) if person_id else query.filter(HardIcpValidationModel.person_id.is_(None))
    row = query.order_by(HardIcpValidationModel.validated_at.desc()).first()
    if row is None:
        return None
    return {"overall_result": row.overall_result, "reason_codes": row.reason_codes, "rule_results": row.rule_results}


def _latest_score(db: Session, icp_id: str, company_id: str, person_id: str | None) -> dict | None:
    query = db.query(LeadScoreModel).filter(LeadScoreModel.icp_id == icp_id, LeadScoreModel.company_id == company_id)
    query = query.filter(LeadScoreModel.person_id == person_id) if person_id else query.filter(LeadScoreModel.person_id.is_(None))
    row = query.order_by(LeadScoreModel.scored_at.desc()).first()
    if row is None:
        return None
    return {
        "final_score": row.final_score,
        "icp_score": row.icp_score,
        "commercial_score": row.commercial_score,
        "evidence_score": row.evidence_score,
    }


def _latest_qualification(db: Session, icp_id: str, company_id: str, person_id: str | None) -> LLMQualificationModel | None:
    query = db.query(LLMQualificationModel).filter(
        LLMQualificationModel.icp_id == icp_id, LLMQualificationModel.company_id == company_id
    )
    query = (
        query.filter(LLMQualificationModel.person_id == person_id)
        if person_id
        else query.filter(LLMQualificationModel.person_id.is_(None))
    )
    return query.order_by(LLMQualificationModel.created_at.desc()).first()


def _latest_adversarial_review(db: Session, qualification_id: str | None) -> dict | None:
    if qualification_id is None:
        return None
    row = (
        db.query(AdversarialReviewModel)
        .filter(AdversarialReviewModel.qualification_id == qualification_id)
        .order_by(AdversarialReviewModel.created_at.desc())
        .first()
    )
    if row is None:
        return None
    return {"adversarial_result": row.adversarial_result, "confidence": row.confidence}


def _latest_verification_outcome(db: Session, company_id: str, person_id: str | None) -> str | None:
    query = db.query(FieldVerificationModel).filter(FieldVerificationModel.entity_id == company_id)
    if person_id:
        person_query = db.query(FieldVerificationModel).filter(FieldVerificationModel.entity_id == person_id)
        row = person_query.order_by(FieldVerificationModel.created_at.desc()).first() or query.order_by(
            FieldVerificationModel.created_at.desc()
        ).first()
    else:
        row = query.order_by(FieldVerificationModel.created_at.desc()).first()
    return row.outcome if row is not None else None


def _assemble_snapshot(db: Session, icp_id: str, company_id: str, person_id: str | None):
    company_evidence = _load_evidence(db, EntityType.COMPANY, company_id)
    person_evidence = _load_evidence(db, EntityType.PERSON, person_id) if person_id else []

    hard_validation = _latest_hard_validation(db, icp_id, company_id, person_id)
    score = _latest_score(db, icp_id, company_id, person_id)
    qualification_row = _latest_qualification(db, icp_id, company_id, person_id)
    qualification = (
        {
            "decision": qualification_row.decision,
            "confidence": qualification_row.confidence,
            "summary": qualification_row.summary,
        }
        if qualification_row is not None
        else None
    )
    adversarial = _latest_adversarial_review(db, qualification_row.id if qualification_row is not None else None)
    verification_outcome = _latest_verification_outcome(db, company_id, person_id)

    return build_pipeline_snapshot(
        company_id=company_id,
        person_id=person_id,
        company_evidence=company_evidence,
        person_evidence=person_evidence,
        latest_hard_validation=hard_validation,
        latest_score=score,
        latest_qualification=qualification,
        latest_adversarial_review=adversarial,
        latest_verification_outcome=verification_outcome,
    )


@router.post("", response_model=ReviewRead, status_code=201)
def submit_human_review(payload: ReviewRequest, db: Session = Depends(get_db)) -> HumanReviewModel:
    lead_row = db.get(CanonicalLeadModel, payload.lead_id)
    if lead_row is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    icp_row = db.get(ICPModel, payload.icp_id)
    if icp_row is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    if payload.decision == ReviewDecision.DUPLICATE and db.get(CanonicalLeadModel, payload.duplicate_of_lead_id) is None:
        raise HTTPException(status_code=404, detail="duplicate_of_lead_id does not reference an existing lead")

    snapshot = _assemble_snapshot(db, payload.icp_id, lead_row.company_id, lead_row.person_id)
    result = decide_review(payload, icp_row.version, snapshot)

    forwarded_feedback_id: str | None = None
    feedback_decision = feedback_decision_for(result.effective_decision)
    if feedback_decision is not None:
        feedback = FeedbackModel(
            id=str(uuid4()),
            lead_ref=result.lead_id,
            icp_id=icp_row.id,
            icp_version=icp_row.version,
            decision=feedback_decision.value,
            reason_codes=list(result.reason_codes),
            reviewer_note=result.reviewer_note,
            reviewer_id=result.reviewer_id,
        )
        # Reuses FeedbackCreate's own validation (non-GOOD_FIT requires a
        # reason code) so a review can never bypass that Phase 4 rule.
        FeedbackCreate(
            lead_ref=feedback.lead_ref,
            icp_id=feedback.icp_id,
            decision=feedback_decision,
            reason_codes=list(result.reason_codes),
            reviewer_note=result.reviewer_note,
            reviewer_id=result.reviewer_id,
        )
        db.add(feedback)
        db.flush()
        db.refresh(feedback)
        forwarded_feedback_id = feedback.id

    row = HumanReviewModel(
        id=str(uuid4()),
        lead_id=result.lead_id,
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        requested_decision=result.requested_decision.value,
        effective_decision=result.effective_decision.value,
        status=result.status.value,
        reason_codes=list(result.reason_codes),
        reviewer_note=result.reviewer_note,
        reviewer_id=result.reviewer_id,
        duplicate_of_lead_id=result.duplicate_of_lead_id,
        snapshot=result.snapshot.model_dump(mode="json"),
        forwarded_feedback_id=forwarded_feedback_id,
        explanation=result.explanation,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{review_id}", response_model=ReviewRead)
def get_human_review(review_id: str, db: Session = Depends(get_db)) -> HumanReviewModel:
    row = db.get(HumanReviewModel, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return row


@router.get("", response_model=list[ReviewRead])
def list_human_reviews(
    icp_id: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    effective_decision: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[HumanReviewModel]:
    if icp_id is None and lead_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, lead_id")

    query = db.query(HumanReviewModel)
    if icp_id is not None:
        query = query.filter(HumanReviewModel.icp_id == icp_id)
    if lead_id is not None:
        query = query.filter(HumanReviewModel.lead_id == lead_id)
    if effective_decision is not None:
        query = query.filter(HumanReviewModel.effective_decision == effective_decision)
    if status is not None:
        query = query.filter(HumanReviewModel.status == status)
    return query.order_by(HumanReviewModel.created_at).all()


@router.get("/queue/{icp_id}", response_model=list[ReviewQueueItem])
def get_review_queue(icp_id: str, db: Session = Depends(get_db)) -> list[ReviewQueueItem]:
    """Every lead ever seen under this ICP (via Phase 19's membership
    table), each with its current pipeline snapshot and whether it has
    already been reviewed under this ICP — a read model, not a queue that
    consumes/locks anything."""
    icp_row = db.get(ICPModel, icp_id)
    if icp_row is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    memberships = db.query(LeadIcpMembershipModel).filter(LeadIcpMembershipModel.icp_id == icp_id).all()

    items: list[ReviewQueueItem] = []
    for membership in memberships:
        lead_row = db.get(CanonicalLeadModel, membership.lead_id)
        if lead_row is None:
            continue
        snapshot = _assemble_snapshot(db, icp_id, lead_row.company_id, lead_row.person_id)
        latest_review = (
            db.query(HumanReviewModel)
            .filter(HumanReviewModel.lead_id == lead_row.id, HumanReviewModel.icp_id == icp_id)
            .order_by(HumanReviewModel.created_at.desc())
            .first()
        )
        items.append(
            ReviewQueueItem(
                lead_id=lead_row.id,
                icp_id=icp_id,
                icp_version=membership.icp_version,
                snapshot=snapshot,
                already_reviewed=latest_review is not None,
                latest_review_decision=latest_review.effective_decision if latest_review else None,
            )
        )
    return items
