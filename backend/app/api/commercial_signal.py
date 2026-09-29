"""Phase 14 API — commercial signal extraction and retrieval.

Operates only on Phase 11 evidence already collected for a canonical
company (Phase 7) — no new provider calls, no ICP involvement, and no
scoring/qualification decision. See
app/services/commercial_signal_extractor.py for the extraction logic
itself, called here unchanged.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.commercial_signal import CommercialSignalModel
from app.models.company import CanonicalCompanyModel
from app.models.evidence import EvidenceModel
from app.schemas.commercial_signal import CommercialSignalRead
from app.schemas.evidence import EntityType, EvidenceRecord
from app.services.commercial_signal_extractor import extract_commercial_signals

router = APIRouter(prefix="/api/v1/companies", tags=["commercial-signals"])


def _load_company_evidence(db: Session, company_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == EntityType.COMPANY.value, EvidenceModel.entity_id == company_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


@router.post(
    "/{company_id}/extract-commercial-signals",
    response_model=list[CommercialSignalRead],
    status_code=201,
)
def extract_company_commercial_signals(
    company_id: str, db: Session = Depends(get_db)
) -> list[CommercialSignalModel]:
    if db.get(CanonicalCompanyModel, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")

    evidence = _load_company_evidence(db, company_id)
    results = extract_commercial_signals(company_id, evidence)

    rows: list[CommercialSignalModel] = []
    for result in results:
        row = CommercialSignalModel(
            id=str(uuid4()),
            company_id=result.company_id,
            signal_type=result.signal_type,
            status=result.status.value,
            confidence=result.confidence.value,
            value=result.value,
            provider_ids=list(result.provider_ids),
            evidence_ids=list(result.evidence_ids),
            first_seen=result.first_seen,
            last_seen=result.last_seen,
            explanation=result.explanation,
        )
        db.add(row)
        rows.append(row)

    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


@router.get("/{company_id}/commercial-signals", response_model=list[CommercialSignalRead])
def list_company_commercial_signals(
    company_id: str,
    signal_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[CommercialSignalModel]:
    if db.get(CanonicalCompanyModel, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")

    query = db.query(CommercialSignalModel).filter(CommercialSignalModel.company_id == company_id)
    if signal_type is not None:
        query = query.filter(CommercialSignalModel.signal_type == signal_type)
    return query.order_by(CommercialSignalModel.created_at).all()
