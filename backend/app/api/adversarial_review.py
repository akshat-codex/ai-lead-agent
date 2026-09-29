"""Phase 17 API — adversarial second pass.

Loads an existing Phase 16 qualification by id plus the same real Phase
2/7/10-15 data that qualification was built from, builds a compact
AdversarialContext (reusing Phase 16's context builder for everything
except the first-pass summary), calls the injected LLMProvider, validates
the response, and persists an append-only AdversarialReviewModel row.
Never mutates the ICP, evidence, score, or first-pass qualification rows
it reads — this endpoint only ever inserts a new adversarial-review row.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.adversarial_review import AdversarialReviewModel
from app.models.business_model import BusinessModelClassificationModel
from app.models.commercial_signal import CommercialSignalModel
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.models.icp import ICPModel
from app.models.llm_qualification import LLMQualificationModel
from app.models.person import CanonicalPersonModel
from app.schemas.adversarial_review import AdversarialReviewRead, AdversarialReviewRequest
from app.schemas.business_model import BusinessModel, BusinessModelClassificationResult, ClassificationStatus
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import LLMQualificationResult, QualificationDecision, QualificationExecutionStatus
from app.services.adversarial_context import build_adversarial_context
from app.services.adversarial_review import run_adversarial_review
from app.services.hard_icp_validation import validate_against_icp
from app.services.icp_normalization import IcpNormalizationError, normalize_icp
from app.services.lead_scoring import score_lead
from app.services.llm_providers.base import LLMProvider
from app.services.llm_providers.default_registry import get_llm_provider

router = APIRouter(prefix="/api/v1/adversarial-reviews", tags=["adversarial-review"])


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


def _qualification_result_from_row(row: LLMQualificationModel) -> LLMQualificationResult:
    """Rehydrates the Phase 16 in-memory result schema from its persisted
    row, exactly as it was produced — never re-derives or re-validates it."""
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


@router.post("", response_model=AdversarialReviewRead, status_code=201)
def create_adversarial_review(
    payload: AdversarialReviewRequest,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> AdversarialReviewModel:
    qualification_row = db.get(LLMQualificationModel, payload.qualification_id)
    if qualification_row is None:
        raise HTTPException(status_code=404, detail="Qualification not found")

    icp_record = db.get(ICPModel, qualification_row.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    company_record = db.get(CanonicalCompanyModel, qualification_row.company_id)
    if company_record is None:
        raise HTTPException(status_code=404, detail="Company not found")

    person_id = qualification_row.person_id
    if person_id is not None and db.get(CanonicalPersonModel, person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")

    try:
        canonical_icp = normalize_icp(
            icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences
        )
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc

    company_id = qualification_row.company_id
    company_evidence = _load_evidence(db, EntityType.COMPANY, company_id)
    person_evidence = _load_evidence(db, EntityType.PERSON, person_id) if person_id else []
    business_model = _latest_business_model(db, company_id)
    commercial_signals = _latest_commercial_signals(db, company_id)

    validation = validate_against_icp(canonical_icp, company_id, company_evidence, person_id, person_evidence)

    score = score_lead(
        icp=canonical_icp,
        company_id=company_id,
        company_evidence=company_evidence,
        company_resolutions=[],
        business_model=business_model,
        commercial_signals=commercial_signals,
        person_id=person_id,
        person_evidence=person_evidence,
        person_resolutions=None,
        now=datetime.now(timezone.utc),
    )

    qualification = _qualification_result_from_row(qualification_row)

    context = build_adversarial_context(
        icp=canonical_icp,
        company_id=company_id,
        company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals,
        score=score,
        qualification_id=qualification_row.id,
        qualification=qualification,
        person_id=person_id,
        person_evidence=person_evidence,
    )

    result = run_adversarial_review(context, provider)

    row = AdversarialReviewModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        qualification_id=result.qualification_id,
        hard_rule_result=result.hard_rule_result.value,
        status=result.status.value,
        adversarial_result=result.adversarial_result.value if result.adversarial_result else None,
        confidence=result.confidence,
        contradictions=list(result.contradictions),
        risk_codes=list(result.risk_codes),
        supporting_evidence_ids=list(result.supporting_evidence_ids),
        contradicting_evidence_ids=list(result.contradicting_evidence_ids),
        unsupported_claims=list(result.unsupported_claims),
        missing_evidence=list(result.missing_evidence),
        reasoning_summary=result.reasoning_summary,
        recommendation=result.recommendation,
        provider_id=result.provider_id,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        error_message=result.error_message,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{review_id}", response_model=AdversarialReviewRead)
def get_adversarial_review(review_id: str, db: Session = Depends(get_db)) -> AdversarialReviewModel:
    row = db.get(AdversarialReviewModel, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Adversarial review not found")
    return row


@router.get("", response_model=list[AdversarialReviewRead])
def list_adversarial_reviews(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    qualification_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[AdversarialReviewModel]:
    if icp_id is None and company_id is None and person_id is None and qualification_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id, person_id, qualification_id")

    query = db.query(AdversarialReviewModel)
    if icp_id is not None:
        query = query.filter(AdversarialReviewModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(AdversarialReviewModel.company_id == company_id)
    if person_id is not None:
        query = query.filter(AdversarialReviewModel.person_id == person_id)
    if qualification_id is not None:
        query = query.filter(AdversarialReviewModel.qualification_id == qualification_id)
    return query.order_by(AdversarialReviewModel.created_at).all()
