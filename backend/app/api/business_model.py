"""Phase 13 API — business model classification.

Operates only on Phase 11 evidence already collected for a canonical
company (Phase 7) — no new provider calls, no ICP involvement, and no
qualification/scoring decision of any kind. See
app/services/business_model_classifier.py for the classification logic
itself, which this endpoint calls unchanged.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.business_model import BusinessModelClassificationModel
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.schemas.business_model import BusinessModelClassificationRead
from app.schemas.evidence import EntityType, EvidenceRecord
from app.services.business_model_classifier import classify_business_model

router = APIRouter(prefix="/api/v1/companies", tags=["business-model"])


def _load_company_evidence(db: Session, company_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == EntityType.COMPANY.value, EvidenceModel.entity_id == company_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


@router.post(
    "/{company_id}/classify-business-model",
    response_model=BusinessModelClassificationRead,
    status_code=201,
)
def classify_company_business_model(
    company_id: str, db: Session = Depends(get_db)
) -> BusinessModelClassificationModel:
    if db.get(CanonicalCompanyModel, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")

    evidence = _load_company_evidence(db, company_id)
    result = classify_business_model(company_id, evidence)

    row = BusinessModelClassificationModel(
        id=str(uuid4()),
        company_id=result.company_id,
        primary_model=result.primary_model.value,
        secondary_models=[model.value for model in result.secondary_models],
        status=result.status.value,
        confidence=result.confidence.value,
        supporting_evidence_ids=list(result.supporting_evidence_ids),
        conflicting_evidence_ids=list(result.conflicting_evidence_ids),
        explanation=result.explanation,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get(
    "/{company_id}/business-model-classifications",
    response_model=list[BusinessModelClassificationRead],
)
def list_business_model_classifications(
    company_id: str, db: Session = Depends(get_db)
) -> list[BusinessModelClassificationModel]:
    if db.get(CanonicalCompanyModel, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")

    return (
        db.query(BusinessModelClassificationModel)
        .filter(BusinessModelClassificationModel.company_id == company_id)
        .order_by(BusinessModelClassificationModel.classified_at)
        .all()
    )
