"""Phase 18 API — multi-source verification.

Loads existing Phase 11 evidence plus real Phase 7/10 identity for one
entity+field, calls the injected provider registry only for genuinely
independent providers, and persists BOTH the new evidence rows (via the
exact Phase 11 EvidenceModel pattern — appended, never overwritten) and an
append-only FieldVerificationModel row for the attempt itself. Never
mutates the ICP, never touches Phase 3/12's hard-rule tables, and never
deletes or edits an existing evidence row.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.models.field_verification import FieldVerificationModel
from app.models.icp import ICPModel
from app.models.person import CanonicalPersonModel
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.evidence import EntityType, EvidenceRecord
from app.schemas.verification import VerificationRead, VerificationRequest
from app.services.field_verification import verify_field

router = APIRouter(prefix="/api/v1/field-verifications", tags=["field-verification"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


def _persist_evidence(db: Session, record: EvidenceRecord) -> None:
    db.add(
        EvidenceModel(
            id=record.id,
            entity_type=record.entity_type.value,
            entity_id=record.entity_id,
            field=record.field,
            value=record.value,
            source_provider_id=record.source_provider_id,
            source_type=record.source_type.value,
            source_url=record.source_url,
            external_id=record.external_id,
            retrieved_at=record.retrieved_at,
            evidence_text=record.evidence_text,
            confidence=record.confidence.value,
        )
    )


@router.post("", response_model=VerificationRead, status_code=201)
def create_field_verification(
    payload: VerificationRequest,
    db: Session = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> FieldVerificationModel:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    entity_name: str | None = None
    domain: str | None = None
    provider_identities: dict[str, str] = {}

    if payload.entity_type == EntityType.COMPANY:
        company_row = db.get(CanonicalCompanyModel, payload.entity_id)
        if company_row is None:
            raise HTTPException(status_code=404, detail="Company not found")
        entity_name = company_row.canonical_name
        domain = company_row.canonical_domain
        provider_identities = dict(company_row.provider_identities)
    else:
        person_row = db.get(CanonicalPersonModel, payload.entity_id)
        if person_row is None:
            raise HTTPException(status_code=404, detail="Person not found")
        entity_name = person_row.canonical_name
        provider_identities = dict(person_row.provider_identities)

    existing_records = _load_evidence(db, payload.entity_type, payload.entity_id)

    result = verify_field(
        icp_id=icp_record.id,
        icp_version=icp_record.version,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        field=payload.field,
        existing_records=existing_records,
        registry=registry,
        entity_name=entity_name,
        domain=domain,
        provider_identities=provider_identities,
    )

    # New evidence is appended, never overwritten — every record here is a
    # brand-new id from verify_field(), so this can never collide with or
    # replace an existing row.
    for record in result.new_evidence_records:
        _persist_evidence(db, record)

    verification_row = FieldVerificationModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        entity_type=result.entity_type.value,
        entity_id=result.entity_id,
        field=result.field,
        trigger=result.trigger.value if result.trigger else None,
        execution_status=result.execution_status.value,
        outcome=result.outcome.value if result.outcome else None,
        original_status=result.original_status.value,
        original_evidence_ids=list(result.original_evidence_ids),
        resulting_status=result.resulting_status.value,
        new_evidence_ids=list(result.new_evidence_ids),
        all_evidence_ids=list(result.all_evidence_ids),
        providers_consulted=[a.model_dump() for a in result.providers_consulted],
        explanation=result.explanation,
    )
    db.add(verification_row)
    db.commit()
    db.refresh(verification_row)
    return verification_row


@router.get("/{verification_id}", response_model=VerificationRead)
def get_field_verification(verification_id: str, db: Session = Depends(get_db)) -> FieldVerificationModel:
    row = db.get(FieldVerificationModel, verification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Verification not found")
    return row


@router.get("", response_model=list[VerificationRead])
def list_field_verifications(
    icp_id: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    field: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[FieldVerificationModel]:
    if icp_id is None and entity_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, entity_id")

    query = db.query(FieldVerificationModel)
    if icp_id is not None:
        query = query.filter(FieldVerificationModel.icp_id == icp_id)
    if entity_id is not None:
        query = query.filter(FieldVerificationModel.entity_id == entity_id)
    if field is not None:
        query = query.filter(FieldVerificationModel.field == field)
    return query.order_by(FieldVerificationModel.created_at).all()
