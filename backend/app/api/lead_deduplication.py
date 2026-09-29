"""Phase 19 API — lead deduplication.

Loads existing canonical leads (global, never ICP-scoped — see
app/services/lead_deduplication.py's own docstring on why deduplication
must not be entity-specific to one ICP) plus the real Phase 10 person
association evidence, calls the deterministic dedup service, and persists:
  1. an append-only LeadDeduplicationModel audit row, always;
  2. a new CanonicalLeadModel row, ONLY for a genuine NEW_LEAD decision —
     never an update, and the table's own unique constraint on
     (company_id, person_id) makes a duplicate insert impossible even if
     this code were ever called concurrently for the same pair;
  3. a LeadIcpMembershipModel row recording that this lead was seen under
     this ICP/version — idempotent via its own unique constraint, so
     reprocessing the same (lead, icp, version) never creates a second
     membership row.

Never mutates the ICP, company, person, or evidence rows it reads.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.company import CanonicalCompanyModel
from app.models.icp import ICPModel
from app.models.lead import CanonicalLeadModel, LeadDeduplicationModel, LeadIcpMembershipModel
from app.models.person import CanonicalPersonModel
from app.schemas.lead_deduplication import (
    CanonicalLeadRead,
    ExistingLead,
    LeadDeduplicationDecision,
    LeadDeduplicationRead,
    LeadDeduplicationRequest,
    LeadIcpMembershipRead,
)
from app.services.lead_deduplication import deduplicate_lead

router = APIRouter(prefix="/api/v1/lead-deduplications", tags=["lead-deduplication"])


def _existing_leads(db: Session) -> list[ExistingLead]:
    rows = db.query(CanonicalLeadModel).all()
    return [ExistingLead(id=row.id, company_id=row.company_id, person_id=row.person_id) for row in rows]


def _record_membership(db: Session, lead_id: str, icp_id: str, icp_version: int) -> None:
    existing = (
        db.query(LeadIcpMembershipModel)
        .filter(
            LeadIcpMembershipModel.lead_id == lead_id,
            LeadIcpMembershipModel.icp_id == icp_id,
            LeadIcpMembershipModel.icp_version == icp_version,
        )
        .one_or_none()
    )
    if existing is not None:
        return
    db.add(LeadIcpMembershipModel(id=str(uuid4()), lead_id=lead_id, icp_id=icp_id, icp_version=icp_version))


@router.post("", response_model=LeadDeduplicationRead, status_code=201)
def create_lead_deduplication(payload: LeadDeduplicationRequest, db: Session = Depends(get_db)) -> LeadDeduplicationModel:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    person_associated_company_ids: tuple[str, ...] | None = None

    if payload.company_id is not None and db.get(CanonicalCompanyModel, payload.company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")

    if payload.person_id is not None:
        person_row = db.get(CanonicalPersonModel, payload.person_id)
        if person_row is None:
            raise HTTPException(status_code=404, detail="Person not found")
        person_associated_company_ids = tuple(person_row.associated_company_ids)

    existing_leads = _existing_leads(db)

    result = deduplicate_lead(
        icp_id=icp_record.id,
        icp_version=icp_record.version,
        company_id=payload.company_id,
        person_id=payload.person_id,
        existing_leads=existing_leads,
        person_associated_company_ids=person_associated_company_ids,
        source=payload.source,
    )

    if result.decision == LeadDeduplicationDecision.NEW_LEAD:
        db.add(CanonicalLeadModel(id=result.lead_id, company_id=result.company_id, person_id=result.person_id))
        try:
            db.flush()
        except IntegrityError:
            # A genuine race: another request inserted the identical
            # (company_id, person_id) pair between our lookup and this
            # insert. The unique constraint is the actual source of truth
            # here — fall back to whatever it now finds rather than ever
            # allowing two canonical leads for the same pair to exist.
            db.rollback()
            winner = (
                db.query(CanonicalLeadModel)
                .filter(CanonicalLeadModel.company_id == result.company_id, CanonicalLeadModel.person_id == result.person_id)
                .one()
            )
            result = result.model_copy(update={"lead_id": winner.id, "decision": LeadDeduplicationDecision.MATCHED_EXISTING_LEAD})

    if result.lead_id is not None:
        _record_membership(db, result.lead_id, result.icp_id, result.icp_version)

    row = LeadDeduplicationModel(
        id=str(uuid4()),
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        person_id=result.person_id,
        decision=result.decision.value,
        lead_id=result.lead_id,
        confidence=result.confidence,
        matched_signals=list(result.matched_signals),
        reason_code=result.reason_code.value,
        explanation=result.explanation,
        company_candidate_id=result.source.company_candidate_id,
        company_resolution_id=result.source.company_resolution_id,
        person_candidate_id=result.source.person_candidate_id,
        person_resolution_id=result.source.person_resolution_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{deduplication_id}", response_model=LeadDeduplicationRead)
def get_lead_deduplication(deduplication_id: str, db: Session = Depends(get_db)) -> LeadDeduplicationModel:
    row = db.get(LeadDeduplicationModel, deduplication_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Deduplication attempt not found")
    return row


@router.get("", response_model=list[LeadDeduplicationRead])
def list_lead_deduplications(
    icp_id: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[LeadDeduplicationModel]:
    if icp_id is None and company_id is None and person_id is None and lead_id is None:
        raise HTTPException(status_code=400, detail="Provide at least one of icp_id, company_id, person_id, lead_id")

    query = db.query(LeadDeduplicationModel)
    if icp_id is not None:
        query = query.filter(LeadDeduplicationModel.icp_id == icp_id)
    if company_id is not None:
        query = query.filter(LeadDeduplicationModel.company_id == company_id)
    if person_id is not None:
        query = query.filter(LeadDeduplicationModel.person_id == person_id)
    if lead_id is not None:
        query = query.filter(LeadDeduplicationModel.lead_id == lead_id)
    return query.order_by(LeadDeduplicationModel.created_at).all()


leads_router = APIRouter(prefix="/api/v1/leads", tags=["lead-deduplication"])


@leads_router.get("/{lead_id}", response_model=CanonicalLeadRead)
def get_canonical_lead(lead_id: str, db: Session = Depends(get_db)) -> CanonicalLeadModel:
    row = db.get(CanonicalLeadModel, lead_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return row


@leads_router.get("/{lead_id}/icp-memberships", response_model=list[LeadIcpMembershipRead])
def list_lead_icp_memberships(lead_id: str, db: Session = Depends(get_db)) -> list[LeadIcpMembershipModel]:
    if db.get(CanonicalLeadModel, lead_id) is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return (
        db.query(LeadIcpMembershipModel)
        .filter(LeadIcpMembershipModel.lead_id == lead_id)
        .order_by(LeadIcpMembershipModel.first_seen_at)
        .all()
    )
