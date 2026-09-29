"""Phase 16 API — LLM qualification.

Loads real Phase 2/7/10-15 data, builds a compact QualificationContext,
calls the injected LLMProvider, validates the response, and persists an
append-only LLMQualificationModel row. Never mutates the ICP, evidence,
score, or manager-feedback rows it reads — this endpoint only ever
inserts a new qualification row.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.business_model import BusinessModelClassificationModel
from app.models.commercial_signal import CommercialSignalModel
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.models.icp import ICPModel
from app.models.llm_qualification import LLMQualificationModel
from app.models.person import CanonicalPersonModel
from app.schemas.business_model import BusinessModel, BusinessModelClassificationResult, ClassificationStatus
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.llm_qualification import LeadQualificationRead, LeadQualificationRequest
from app.services.hard_icp_validation import validate_against_icp
from app.services.icp_normalization import IcpNormalizationError, normalize_icp
from app.services.lead_scoring import score_lead
from app.services.llm_providers.base import LLMProvider
from app.services.llm_providers.default_registry import get_llm_provider
from app.services.llm_qualification import qualify_lead
from app.services.qualification_context import build_qualification_context

router = APIRouter(prefix="/api/v1/lead-qualifications", tags=["llm-qualification"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


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
        latest_by_type.setdefault(row.signal_type, row)

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


@router.post("", response_model=LeadQualificationRead, status_code=201)
def create_lead_qualification(
    payload: LeadQualificationRequest,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> LLMQualificationModel:
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
    business_model = _latest_business_model(db, payload.company_id)
    commercial_signals = _latest_commercial_signals(db, payload.company_id)

    validation = validate_against_icp(
        canonical_icp, payload.company_id, company_evidence, payload.person_id, person_evidence
    )

    score = score_lead(
        icp=canonical_icp,
        company_id=payload.company_id,
        company_evidence=company_evidence,
        company_resolutions=[],
        business_model=business_model,
        commercial_signals=commercial_signals,
        person_id=payload.person_id,
        person_evidence=person_evidence,
        person_resolutions=None,
        now=datetime.now(timezone.utc),
    )

    context = build_qualification_context(
        icp=canonical_icp,
        company_id=payload.company_id,
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals,
        score=score,
        person_id=payload.person_id,
        person_evidence=person_evidence,
    )

    result = qualify_lead(context, provider)

    row = LLMQualificationModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        hard_rule_result=result.hard_rule_result.value,
        status=result.status.value,
        decision=result.decision.value if result.decision else None,
        confidence=result.confidence,
        reason_codes=list(result.reason_codes),
        summary=result.summary,
        supporting_evidence_ids=list(result.supporting_evidence_ids),
        risk_evidence_ids=list(result.risk_evidence_ids),
        missing_evidence=list(result.missing_evidence),
        commercial_fit_explanation=result.commercial_fit_explanation,
        hard_rule_acknowledgement=result.hard_rule_acknowledgement,
        uncertainties=list(result.uncertainties),
        provider_id=result.provider_id,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        score_snapshot=result.score_snapshot,
        error_message=result.error_message,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{qualification_id}", response_model=LeadQualificationRead)
def get_lead_qualification(qualification_id: str, db: Session = Depends(get_db)) -> LLMQualificationModel:
    row = db.get(LLMQualificationModel, qualification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Qualification not found")
    return row


@router.get("", response_model=list[LeadQualificationRead])
def list_lead_qualifications(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[LLMQualificationModel]:
    if icp_id is None and company_id is None and person_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id, person_id")

    query = db.query(LLMQualificationModel)
    if icp_id is not None:
        query = query.filter(LLMQualificationModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(LLMQualificationModel.company_id == company_id)
    if person_id is not None:
        query = query.filter(LLMQualificationModel.person_id == person_id)
    return query.order_by(LLMQualificationModel.created_at).all()
