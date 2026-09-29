"""Phase 30 API — company-quality scoring.

Loads real Phase 2/11/12/15/18 data for one company (no person_id — this
endpoint is deliberately company-only, see app/services/company_quality.py's
own docstring for why), builds the same QualificationContext Phase 16/17
already build, calls the pure app/services/company_quality.score_company_quality(),
and persists an append-only CompanyQualityModel row. Never calls an LLM
provider and never invokes Unipile/people discovery.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.business_model import BusinessModelClassificationModel
from app.models.commercial_signal import CommercialSignalModel
from app.models.company import CanonicalCompanyModel
from app.models.company_quality import CompanyQualityModel
from app.models.evidence import EvidenceModel
from app.models.icp import ICPModel
from app.models.llm_qualification import LLMQualificationModel
from app.schemas.business_model import BusinessModel, BusinessModelClassificationResult, ClassificationStatus
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.company_quality import CompanyQualityRead, CompanyQualityRequest
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import LLMQualificationResult, QualificationDecision, QualificationExecutionStatus
from app.services.company_quality import score_company_quality
from app.services.hard_icp_validation import validate_against_icp
from app.services.icp_normalization import IcpNormalizationError, normalize_icp
from app.services.lead_scoring import score_lead
from app.services.qualification_context import build_qualification_context

router = APIRouter(prefix="/api/v1/company-quality", tags=["company-quality"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = db.query(EvidenceModel).filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id).all()
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


def _latest_successful_qualification(db: Session, icp_id: str, company_id: str) -> LLMQualificationResult | None:
    """Only a company-only (person_id IS NULL) qualification is eligible —
    this endpoint never reads a person-scoped qualification row, since it
    never receives a person_id itself. Only a SUCCESS status is used (a
    real LLM judgment) — STRUCTURED_MATCH_TRUSTED/HARD_REJECTED/HARD_HOLD/
    any failure status carries no semantic-verification content to add
    beyond what discovery_provenance/icp_fit already surface."""
    row = (
        db.query(LLMQualificationModel)
        .filter(
            LLMQualificationModel.icp_id == icp_id,
            LLMQualificationModel.company_id == company_id,
            LLMQualificationModel.person_id.is_(None),
            LLMQualificationModel.status == QualificationExecutionStatus.SUCCESS.value,
        )
        .order_by(LLMQualificationModel.created_at.desc())
        .first()
    )
    if row is None:
        return None
    return LLMQualificationResult(
        icp_id=row.icp_id,
        icp_version=row.icp_version,
        company_id=row.company_id,
        person_id=row.person_id,
        hard_rule_result=OverallResult(row.hard_rule_result),
        status=QualificationExecutionStatus(row.status),
        decision=QualificationDecision(row.decision) if row.decision else None,
        confidence=row.confidence,
        reason_codes=tuple(row.reason_codes),
        summary=row.summary,
        supporting_evidence_ids=tuple(row.supporting_evidence_ids),
        risk_evidence_ids=tuple(row.risk_evidence_ids),
        missing_evidence=tuple(row.missing_evidence),
        commercial_fit_explanation=row.commercial_fit_explanation,
        hard_rule_acknowledgement=row.hard_rule_acknowledgement,
        uncertainties=tuple(row.uncertainties),
        provider_id=row.provider_id,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        score_snapshot=row.score_snapshot,
        error_message=row.error_message,
    )


@router.post("", response_model=CompanyQualityRead, status_code=201)
def create_company_quality(payload: CompanyQualityRequest, db: Session = Depends(get_db)) -> CompanyQualityModel:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    company_record = db.get(CanonicalCompanyModel, payload.company_id)
    if company_record is None:
        raise HTTPException(status_code=404, detail="Company not found")

    try:
        canonical_icp = normalize_icp(icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences)
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc

    company_id = payload.company_id
    company_evidence = _load_evidence(db, EntityType.COMPANY, company_id)
    business_model = _latest_business_model(db, company_id)
    commercial_signals = _latest_commercial_signals(db, company_id)

    validation = validate_against_icp(canonical_icp, company_id, company_evidence, None, [])

    score = score_lead(
        icp=canonical_icp,
        company_id=company_id,
        company_evidence=company_evidence,
        company_resolutions=[],
        business_model=business_model,
        commercial_signals=commercial_signals,
        person_id=None,
        person_evidence=[],
        person_resolutions=None,
        now=datetime.now(timezone.utc),
    )

    context = build_qualification_context(
        icp=canonical_icp,
        company_id=company_id,
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals,
        score=score,
    )

    qualification = _latest_successful_qualification(db, payload.icp_id, company_id)
    result = score_company_quality(context, qualification)

    row = CompanyQualityModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        hard_rule_result=result.hard_rule_result,
        label=result.label.value,
        score=result.score,
        signals=[s.model_dump(mode="json") for s in result.signals],
        explanation=result.explanation,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{quality_id}", response_model=CompanyQualityRead)
def get_company_quality(quality_id: str, db: Session = Depends(get_db)) -> CompanyQualityModel:
    row = db.get(CompanyQualityModel, quality_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Company quality score not found")
    return row


@router.get("", response_model=list[CompanyQualityRead])
def list_company_quality(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[CompanyQualityModel]:
    if icp_id is None and company_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id")

    query = db.query(CompanyQualityModel)
    if icp_id is not None:
        query = query.filter(CompanyQualityModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(CompanyQualityModel.company_id == company_id)
    return query.order_by(CompanyQualityModel.created_at).all()
