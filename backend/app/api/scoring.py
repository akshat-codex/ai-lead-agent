"""Phase 15 API — adaptive lead scoring.

Loads real Phase 2/7/10/11/13/14 data, calls the unchanged
app/services/lead_scoring.score_lead(), and persists the result as a new,
append-only LeadScoreModel row. No qualification decision, no LLM call,
and no manager-feedback lookup happens here — this endpoint exposes the
six independently-inspectable scores and nothing more.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.business_model import BusinessModelClassificationModel
from app.models.commercial_signal import CommercialSignalModel
from app.models.company import CanonicalCompanyModel, CompanyResolutionModel
from app.models.evidence import EvidenceModel
from app.models.icp import ICPModel
from app.models.person import CanonicalPersonModel, PersonResolutionModel
from app.models.scoring import LeadScoreModel
from app.schemas.business_model import BusinessModelClassificationResult, BusinessModel, ClassificationStatus
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.scoring import DEFAULT_SCORING_WEIGHTS, LeadScoreRead, LeadScoreRequest, ResolutionSignal
from app.services.icp_normalization import IcpNormalizationError, normalize_icp
from app.services.lead_scoring import score_lead

router = APIRouter(prefix="/api/v1/lead-scores", tags=["lead-scoring"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


def _company_resolutions(db: Session, company_id: str) -> list[ResolutionSignal]:
    rows = (
        db.query(CompanyResolutionModel)
        .filter(CompanyResolutionModel.canonical_company_id == company_id)
        .all()
    )
    return [
        ResolutionSignal(
            status=row.status,
            confidence=row.confidence,
            has_conflict=bool(row.conflicting_signals),
        )
        for row in rows
    ]


def _person_resolutions(db: Session, person_id: str) -> list[ResolutionSignal]:
    rows = (
        db.query(PersonResolutionModel)
        .filter(PersonResolutionModel.canonical_person_id == person_id)
        .all()
    )
    return [
        ResolutionSignal(
            status=row.status,
            confidence=row.confidence,
            has_conflict=bool(row.conflicting_signals),
        )
        for row in rows
    ]


def _latest_business_model(db: Session, company_id: str) -> BusinessModelClassificationResult | None:
    row = (
        db.query(BusinessModelClassificationModel)
        .filter(BusinessModelClassificationModel.company_id == company_id)
        .order_by(BusinessModelClassificationModel.classified_at.desc())
        .first()
    )
    if row is None:
        return None
    return BusinessModelClassificationResult(
        company_id=row.company_id,
        primary_model=BusinessModel(row.primary_model),
        secondary_models=tuple(BusinessModel(m) for m in row.secondary_models),
        status=ClassificationStatus(row.status),
        confidence=ConfidenceLevel(row.confidence),
        supporting_evidence_ids=tuple(row.supporting_evidence_ids),
        conflicting_evidence_ids=tuple(row.conflicting_evidence_ids),
        explanation=row.explanation,
    )


def _latest_commercial_signals(db: Session, company_id: str) -> list[CommercialSignalResult]:
    rows = (
        db.query(CommercialSignalModel)
        .filter(CommercialSignalModel.company_id == company_id)
        .order_by(CommercialSignalModel.created_at.desc())
        .all()
    )
    latest_by_type: dict[str, CommercialSignalModel] = {}
    for row in rows:
        latest_by_type.setdefault(row.signal_type, row)  # first hit per type is the most recent, desc order

    return [
        CommercialSignalResult(
            company_id=row.company_id,
            signal_type=row.signal_type,
            status=EvidenceStatus(row.status),
            confidence=ConfidenceLevel(row.confidence),
            value=row.value,
            provider_ids=tuple(row.provider_ids),
            evidence_ids=tuple(row.evidence_ids),
            first_seen=row.first_seen,
            last_seen=row.last_seen,
            explanation=row.explanation,
        )
        for row in latest_by_type.values()
    ]


@router.post("", response_model=LeadScoreRead, status_code=201)
def create_lead_score(payload: LeadScoreRequest, db: Session = Depends(get_db)) -> LeadScoreModel:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    company_record = db.get(CanonicalCompanyModel, payload.company_id)
    if company_record is None:
        raise HTTPException(status_code=404, detail="Company not found")

    if payload.person_id is not None and db.get(CanonicalPersonModel, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")

    try:
        canonical_icp = normalize_icp(
            icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences
        )
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc

    company_evidence = _load_evidence(db, EntityType.COMPANY, payload.company_id)
    person_evidence = _load_evidence(db, EntityType.PERSON, payload.person_id) if payload.person_id else []
    company_resolutions = _company_resolutions(db, payload.company_id)
    person_resolutions = _person_resolutions(db, payload.person_id) if payload.person_id else None
    business_model = _latest_business_model(db, payload.company_id)
    commercial_signals = _latest_commercial_signals(db, payload.company_id)

    result = score_lead(
        icp=canonical_icp,
        company_id=payload.company_id,
        company_evidence=company_evidence,
        company_resolutions=company_resolutions,
        business_model=business_model,
        commercial_signals=commercial_signals,
        person_id=payload.person_id,
        person_evidence=person_evidence,
        person_resolutions=person_resolutions,
        now=datetime.now(timezone.utc),
        weights=DEFAULT_SCORING_WEIGHTS,
    )

    row = LeadScoreModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        hard_icp_result=result.hard_icp_result.value,
        eligible_for_scoring=result.eligible_for_scoring,
        icp_score=result.icp_score,
        commercial_score=result.commercial_score,
        evidence_score=result.evidence_score,
        freshness_score=result.freshness_score,
        identity_confidence=result.identity_confidence,
        final_score=result.final_score,
        weights_version=result.weights.version,
        weights=result.weights.model_dump(mode="json"),
        evidence_ids=list(result.evidence_ids),
        reason_codes=[code.value for code in result.reason_codes],
        explanation=result.explanation,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{score_id}", response_model=LeadScoreRead)
def get_lead_score(score_id: str, db: Session = Depends(get_db)) -> LeadScoreModel:
    row = db.get(LeadScoreModel, score_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Lead score not found")
    return row


@router.get("", response_model=list[LeadScoreRead])
def list_lead_scores(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[LeadScoreModel]:
    if icp_id is None and company_id is None and person_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id, person_id")

    query = db.query(LeadScoreModel)
    if icp_id is not None:
        query = query.filter(LeadScoreModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(LeadScoreModel.company_id == company_id)
    if person_id is not None:
        query = query.filter(LeadScoreModel.person_id == person_id)
    return query.order_by(LeadScoreModel.scored_at).all()
