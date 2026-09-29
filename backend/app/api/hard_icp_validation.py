"""Phase 12 API — evidence-backed hard ICP validation.

Reuses the unchanged Phase 3 Hard ICP Rule Engine via
app/services/hard_icp_validation.py. No commercial scoring, business-model
classification, or lead acceptance happens here — only PASS/FAIL/HOLD on
the ICP's configured hard rules, backed by whatever Phase 11 evidence
currently exists.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.models.hard_icp_validation import HardIcpValidationModel
from app.models.icp import ICPModel
from app.models.person import CanonicalPersonModel
from app.schemas.evidence import EntityType, EvidenceRecord
from app.schemas.hard_icp_validation import HardIcpValidationRead, HardIcpValidationRequest
from app.services.hard_icp_validation import validate_against_icp
from app.services.icp_normalization import IcpNormalizationError, normalize_icp

router = APIRouter(prefix="/api/v1/hard-icp-validations", tags=["hard-icp-validation"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


@router.post("", response_model=HardIcpValidationRead, status_code=201)
def create_validation(payload: HardIcpValidationRequest, db: Session = Depends(get_db)) -> HardIcpValidationModel:
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

    result = validate_against_icp(
        canonical_icp, payload.company_id, company_evidence, payload.person_id, person_evidence
    )
    evaluation = result.evaluation

    row = HardIcpValidationModel(
        id=str(uuid4()),
        icp_id=evaluation.icp_id,
        icp_version=evaluation.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        overall_result=evaluation.overall_result.value,
        rule_results=[r.model_dump(mode="json") for r in evaluation.rule_results],
        failed_rules=[r.model_dump(mode="json") for r in evaluation.failed_rules],
        unresolved_rules=[r.model_dump(mode="json") for r in evaluation.unresolved_rules],
        reason_codes=[code.value for code in evaluation.reason_codes],
        explanation=evaluation.explanation,
        evidence_ids={rule: list(ids) for rule, ids in result.evidence_ids.items()},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{validation_id}", response_model=HardIcpValidationRead)
def get_validation(validation_id: str, db: Session = Depends(get_db)) -> HardIcpValidationModel:
    row = db.get(HardIcpValidationModel, validation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Validation not found")
    return row


@router.get("", response_model=list[HardIcpValidationRead])
def list_validations(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[HardIcpValidationModel]:
    if icp_id is None and company_id is None and person_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id, person_id")

    query = db.query(HardIcpValidationModel)
    if icp_id is not None:
        query = query.filter(HardIcpValidationModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(HardIcpValidationModel.company_id == company_id)
    if person_id is not None:
        query = query.filter(HardIcpValidationModel.person_id == person_id)
    return query.order_by(HardIcpValidationModel.validated_at).all()
