"""Phase 22 API — lead ranking and prioritization.

Loads the LATEST already-persisted Phase 12 (hard validation)/15 (score)/
16 (qualification)/17 (adversarial review)/18 (verification)/20 (human
review)/21 (batch item) row for every lead seen under one ICP (via Phase
19's own membership table), builds a compact RankedLeadSignals bundle per
lead, and calls the unchanged, pure app/services/lead_ranking.rank_leads().
Never mutates any ICP, evidence, score, qualification, adversarial-review,
verification, lead-identity, or batch row it reads — this endpoint only
computes an ordering and optionally persists an append-only snapshot of
that computation.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.adversarial_review import AdversarialReviewModel
from app.models.batch import BatchItemModel
from app.models.company_quality import CompanyQualityModel
from app.models.evidence import EvidenceModel
from app.models.field_verification import FieldVerificationModel
from app.models.hard_icp_validation import HardIcpValidationModel
from app.models.human_review import HumanReviewModel
from app.models.icp import ICPModel
from app.models.lead import CanonicalLeadModel, LeadIcpMembershipModel
from app.models.llm_qualification import LLMQualificationModel
from app.models.ranking import RankingSnapshotModel
from app.models.scoring import LeadScoreModel
from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.ranking import RankedLeadSignals, RankingResult, RankingSnapshotRead
from app.services.evidence_engine import summarize_entity
from app.services.lead_ranking import rank_leads

router = APIRouter(prefix="/api/v1/rankings", tags=["ranking"])


def _last(rows: list) -> object | None:
    """Picks the latest row from a list already ordered ascending by
    timestamp. Used instead of `.order_by(col.desc()).first()` because
    SQLite's DateTime column resolution can produce identical timestamps
    for rows inserted within the same pass (e.g. two hard-validation calls
    a batch or a test issues back-to-back) — a DESC tie-break order is not
    guaranteed to match insertion order, but ascending-then-take-last
    reliably returns the row that was actually written most recently."""
    return rows[-1] if rows else None


def _latest_hard_validation(db: Session, icp_id: str, company_id: str, person_id: str | None) -> HardIcpValidationModel | None:
    query = db.query(HardIcpValidationModel).filter(
        HardIcpValidationModel.icp_id == icp_id, HardIcpValidationModel.company_id == company_id
    )
    query = query.filter(HardIcpValidationModel.person_id == person_id) if person_id else query.filter(HardIcpValidationModel.person_id.is_(None))
    return _last(query.order_by(HardIcpValidationModel.validated_at).all())


def _latest_score(db: Session, icp_id: str, company_id: str, person_id: str | None) -> LeadScoreModel | None:
    query = db.query(LeadScoreModel).filter(LeadScoreModel.icp_id == icp_id, LeadScoreModel.company_id == company_id)
    query = query.filter(LeadScoreModel.person_id == person_id) if person_id else query.filter(LeadScoreModel.person_id.is_(None))
    return _last(query.order_by(LeadScoreModel.scored_at).all())


def _latest_qualification(db: Session, icp_id: str, company_id: str, person_id: str | None) -> LLMQualificationModel | None:
    query = db.query(LLMQualificationModel).filter(
        LLMQualificationModel.icp_id == icp_id, LLMQualificationModel.company_id == company_id
    )
    query = (
        query.filter(LLMQualificationModel.person_id == person_id)
        if person_id
        else query.filter(LLMQualificationModel.person_id.is_(None))
    )
    return _last(query.order_by(LLMQualificationModel.created_at).all())


def _latest_company_quality(db: Session, icp_id: str, company_id: str) -> CompanyQualityModel | None:
    """Phase 30's CompanyQualityModel is company-only (no person_id column
    — see app/services/company_quality.py's own "never requires a
    person_id" contract), so this lookup is keyed the same way regardless
    of which person, if any, this specific lead pairs the company with."""
    return _last(
        db.query(CompanyQualityModel)
        .filter(CompanyQualityModel.icp_id == icp_id, CompanyQualityModel.company_id == company_id)
        .order_by(CompanyQualityModel.created_at)
        .all()
    )


def _latest_adversarial_review(db: Session, qualification_id: str | None) -> AdversarialReviewModel | None:
    if qualification_id is None:
        return None
    return _last(
        db.query(AdversarialReviewModel)
        .filter(AdversarialReviewModel.qualification_id == qualification_id)
        .order_by(AdversarialReviewModel.created_at)
        .all()
    )


def _has_unresolved_verification(db: Session, company_id: str, person_id: str | None) -> bool:
    entity_ids = [company_id] + ([person_id] if person_id else [])
    row = (
        db.query(FieldVerificationModel)
        .filter(FieldVerificationModel.entity_id.in_(entity_ids), FieldVerificationModel.outcome.in_(["UNRESOLVED", "HOLD"]))
        .order_by(FieldVerificationModel.created_at.desc())
        .first()
    )
    return row is not None


def _has_evidence_conflicts(db: Session, company_id: str, person_id: str | None) -> bool:
    company_rows = db.query(EvidenceModel).filter(EvidenceModel.entity_type == EntityType.COMPANY.value, EvidenceModel.entity_id == company_id).all()
    company_records = [EvidenceRecord.model_validate(row, from_attributes=True) for row in company_rows]
    summary = summarize_entity(EntityType.COMPANY, company_id, company_records)
    if any(f.status == EvidenceStatus.CONFLICT for f in summary.fields):
        return True

    if person_id is not None:
        person_rows = db.query(EvidenceModel).filter(EvidenceModel.entity_type == EntityType.PERSON.value, EvidenceModel.entity_id == person_id).all()
        person_records = [EvidenceRecord.model_validate(row, from_attributes=True) for row in person_rows]
        person_summary = summarize_entity(EntityType.PERSON, person_id, person_records)
        if any(f.status == EvidenceStatus.CONFLICT for f in person_summary.fields):
            return True
    return False


def _latest_human_review(db: Session, icp_id: str, lead_id: str) -> HumanReviewModel | None:
    return _last(
        db.query(HumanReviewModel)
        .filter(HumanReviewModel.icp_id == icp_id, HumanReviewModel.lead_id == lead_id)
        .order_by(HumanReviewModel.created_at)
        .all()
    )


def _latest_batch_item(db: Session, lead_id: str, batch_id: str | None) -> BatchItemModel | None:
    query = db.query(BatchItemModel).filter(BatchItemModel.lead_id == lead_id)
    if batch_id is not None:
        query = query.filter(BatchItemModel.batch_id == batch_id)
    return _last(query.order_by(BatchItemModel.updated_at).all())


def _build_signals(db: Session, icp_id: str, lead_id: str, company_id: str, person_id: str | None, batch_id: str | None) -> RankedLeadSignals:
    hard_validation = _latest_hard_validation(db, icp_id, company_id, person_id)
    score = _latest_score(db, icp_id, company_id, person_id)
    quality = _latest_company_quality(db, icp_id, company_id)
    qualification = _latest_qualification(db, icp_id, company_id, person_id)
    adversarial = _latest_adversarial_review(db, qualification.id if qualification is not None else None)
    human_review = _latest_human_review(db, icp_id, lead_id)
    batch_item = _latest_batch_item(db, lead_id, batch_id)

    return RankedLeadSignals(
        hard_rule_result=hard_validation.overall_result if hard_validation is not None else None,
        final_score=score.final_score if score is not None else None,
        icp_score=score.icp_score if score is not None else None,
        commercial_score=score.commercial_score if score is not None else None,
        evidence_score=score.evidence_score if score is not None else None,
        freshness_score=score.freshness_score if score is not None else None,
        identity_confidence=score.identity_confidence if score is not None else None,
        qualification_decision=qualification.decision if qualification is not None else None,
        qualification_confidence=qualification.confidence if qualification is not None else None,
        adversarial_result=adversarial.adversarial_result if adversarial is not None else None,
        adversarial_confidence=adversarial.confidence if adversarial is not None else None,
        evidence_has_conflicts=_has_evidence_conflicts(db, company_id, person_id),
        verification_unresolved=_has_unresolved_verification(db, company_id, person_id),
        human_review_decision=human_review.effective_decision if human_review is not None else None,
        batch_outcome=batch_item.outcome if batch_item is not None else None,
        company_quality_score=quality.score if quality is not None else None,
        company_quality_label=quality.label if quality is not None else None,
    )


def _compute_ranking(db: Session, icp_id: str, batch_id: str | None) -> RankingResult:
    icp_record = db.get(ICPModel, icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    membership_query = db.query(LeadIcpMembershipModel).filter(LeadIcpMembershipModel.icp_id == icp_id)
    memberships = membership_query.all()

    lead_tuples: list[tuple[str, str, str | None, RankedLeadSignals]] = []
    for membership in memberships:
        lead_row = db.get(CanonicalLeadModel, membership.lead_id)
        if lead_row is None:
            continue
        if batch_id is not None:
            has_batch_item = (
                db.query(BatchItemModel)
                .filter(BatchItemModel.lead_id == lead_row.id, BatchItemModel.batch_id == batch_id)
                .first()
            )
            if has_batch_item is None:
                continue
        signals = _build_signals(db, icp_id, lead_row.id, lead_row.company_id, lead_row.person_id, batch_id)
        lead_tuples.append((lead_row.id, lead_row.company_id, lead_row.person_id, signals))

    return rank_leads(icp_id, icp_record.version, batch_id, lead_tuples)


@router.get("", response_model=RankingResult)
def get_ranking(
    icp_id: str = Query(..., min_length=1),
    batch_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> RankingResult:
    return _compute_ranking(db, icp_id, batch_id)


@router.post("/snapshots", response_model=RankingSnapshotRead, status_code=201)
def create_ranking_snapshot(
    icp_id: str = Query(..., min_length=1),
    batch_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> RankingSnapshotModel:
    result = _compute_ranking(db, icp_id, batch_id)

    row = RankingSnapshotModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        batch_id=result.batch_id,
        lead_count=len(result.ranked_leads),
        ranked_leads=[lead.model_dump(mode="json") for lead in result.ranked_leads],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/snapshots/{snapshot_id}", response_model=RankingSnapshotRead)
def get_ranking_snapshot(snapshot_id: str, db: Session = Depends(get_db)) -> RankingSnapshotModel:
    row = db.get(RankingSnapshotModel, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Ranking snapshot not found")
    return row


@router.get("/snapshots", response_model=list[RankingSnapshotRead])
def list_ranking_snapshots(
    icp_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[RankingSnapshotModel]:
    if icp_id is None and batch_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, batch_id")

    query = db.query(RankingSnapshotModel)
    if icp_id is not None:
        query = query.filter(RankingSnapshotModel.icp_id == icp_id)
    if batch_id is not None:
        query = query.filter(RankingSnapshotModel.batch_id == batch_id)
    return query.order_by(RankingSnapshotModel.created_at).all()
