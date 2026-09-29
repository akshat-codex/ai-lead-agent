"""Phase 11 API — record, retrieve, and summarize evidence.

No qualification, scoring, or business-model classification happens here.
Importing evidence from existing Phase 6-10 records never calls a
provider and never re-runs discovery, enrichment, or entity resolution —
it only reads what those phases already persisted.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.models.person import CanonicalPersonModel
from app.schemas.evidence import EntityEvidenceSummary, EntityType, EvidenceCreate, EvidenceRecord
from app.services.evidence_engine import summarize_entity
from app.services.evidence_import import collect_company_evidence, collect_person_evidence

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])


class EvidenceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: EntityType
    entity_id: str = Field(min_length=1)


class EvidenceImportResult(BaseModel):
    added: list[EvidenceRecord]
    skipped_duplicates: int


def _row_to_record(row: EvidenceModel) -> EvidenceRecord:
    return EvidenceRecord.model_validate(row, from_attributes=True)


def _ensure_entity_exists(db: Session, entity_type: EntityType, entity_id: str) -> None:
    if entity_type == EntityType.COMPANY:
        exists = db.get(CanonicalCompanyModel, entity_id) is not None
    else:
        exists = db.get(CanonicalPersonModel, entity_id) is not None
    if not exists:
        raise HTTPException(status_code=404, detail=f"{entity_type.value.title()} not found")


def _is_duplicate(db: Session, candidate: EvidenceCreate) -> bool:
    """A best-effort natural key for the same underlying observation, so
    re-importing already-collected data is safely idempotent — it never
    guards against two genuinely different observations that happen to
    share a value, only exact re-imports of the same source row."""
    existing = (
        db.query(EvidenceModel)
        .filter(
            EvidenceModel.entity_type == candidate.entity_type.value,
            EvidenceModel.entity_id == candidate.entity_id,
            EvidenceModel.field == candidate.field,
            EvidenceModel.source_provider_id == candidate.source_provider_id,
            EvidenceModel.external_id == candidate.external_id,
            EvidenceModel.retrieved_at == candidate.retrieved_at,
        )
        .first()
    )
    return existing is not None


def _persist(db: Session, candidate: EvidenceCreate) -> EvidenceModel:
    row = EvidenceModel(
        id=str(uuid4()),
        entity_type=candidate.entity_type.value,
        entity_id=candidate.entity_id,
        field=candidate.field,
        value=candidate.value,
        source_provider_id=candidate.source_provider_id,
        source_type=candidate.source_type.value,
        source_url=candidate.source_url,
        external_id=candidate.external_id,
        retrieved_at=candidate.retrieved_at,
        evidence_text=candidate.evidence_text,
        confidence=candidate.confidence.value,
    )
    db.add(row)
    return row


@router.post("", response_model=EvidenceRecord, status_code=201)
def add_evidence(payload: EvidenceCreate, db: Session = Depends(get_db)) -> EvidenceModel:
    """Records a single evidence observation directly — for a source that
    isn't one of the existing Phase 6-10 records app/services/evidence_import.py
    already knows how to import."""
    _ensure_entity_exists(db, payload.entity_type, payload.entity_id)

    row = _persist(db, payload)
    db.commit()
    db.refresh(row)
    return row


@router.post("/import", response_model=EvidenceImportResult)
def import_evidence(payload: EvidenceImportRequest, db: Session = Depends(get_db)) -> EvidenceImportResult:
    """Converts existing Phase 6-10 records for one entity into evidence.

    Safe to call repeatedly: already-imported observations are skipped
    rather than duplicated (see _is_duplicate).
    """
    _ensure_entity_exists(db, payload.entity_type, payload.entity_id)

    if payload.entity_type == EntityType.COMPANY:
        candidates = collect_company_evidence(db, payload.entity_id)
    else:
        candidates = collect_person_evidence(db, payload.entity_id)

    added_rows: list[EvidenceModel] = []
    skipped = 0
    for candidate in candidates:
        if _is_duplicate(db, candidate):
            skipped += 1
            continue
        added_rows.append(_persist(db, candidate))

    db.commit()
    for row in added_rows:
        db.refresh(row)

    return EvidenceImportResult(added=[_row_to_record(row) for row in added_rows], skipped_duplicates=skipped)


@router.get("", response_model=list[EvidenceRecord])
def list_evidence(
    entity_type: EntityType = Query(...),
    entity_id: str = Query(..., min_length=1),
    field: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[EvidenceModel]:
    """Retrieves evidence for an entity — optionally scoped to one field."""
    query = db.query(EvidenceModel).filter(
        EvidenceModel.entity_type == entity_type.value,
        EvidenceModel.entity_id == entity_id,
    )
    if field is not None:
        query = query.filter(EvidenceModel.field == field)
    return query.order_by(EvidenceModel.retrieved_at).all()


@router.get("/summary", response_model=EntityEvidenceSummary)
def get_evidence_summary(
    entity_type: EntityType = Query(...),
    entity_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> EntityEvidenceSummary:
    """The aggregated status/completeness view: every field grouped, with
    agreement/conflict detected and nothing resolved automatically."""
    _ensure_entity_exists(db, entity_type, entity_id)

    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .order_by(EvidenceModel.retrieved_at)
        .all()
    )
    records = [_row_to_record(row) for row in rows]
    return summarize_entity(entity_type, entity_id, records)
