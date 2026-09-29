"""Phase 19 — canonical lead entity, ICP membership, and deduplication audit.

CanonicalLeadModel represents one real-world (company, person) pairing —
or a company alone when no person is involved yet — independent of any
ICP, exactly like Phase 7/10's canonical company/person rows. The
`uq_lead_company_person` constraint is the actual safety net: even a bug
in the service layer can never produce two canonical lead rows for the
same (company_id, person_id) pair, because the database itself refuses
the insert.

LeadIcpMembershipModel is the append-only record of which ICPs a lead has
been seen under — this is what makes "a lead can belong to multiple ICPs
while remaining one underlying entity" concrete: the lead row itself never
mentions an ICP, and membership rows accumulate rather than overwrite.
`uq_membership_lead_icp_version` keeps re-processing idempotent: the same
lead surfacing again under the same ICP version does not create a second
membership row.

LeadDeduplicationModel is the permanent, append-only audit record of every
deduplication attempt — MATCHED_EXISTING_LEAD, NEW_LEAD, or UNRESOLVED —
mirroring CompanyResolutionModel/PersonResolutionModel exactly. Unlike
those two tables, an attempt is NOT constrained to run at most once per
candidate: the same underlying candidate pair may legitimately be
reprocessed (e.g. a later discovery run), and every attempt is preserved.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CanonicalLeadModel(Base):
    __tablename__ = "canonical_leads"
    __table_args__ = (UniqueConstraint("company_id", "person_id", name="uq_lead_company_person"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadIcpMembershipModel(Base):
    __tablename__ = "lead_icp_memberships"
    __table_args__ = (UniqueConstraint("lead_id", "icp_id", "icp_version", name="uq_membership_lead_icp_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lead_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadDeduplicationModel(Base):
    __tablename__ = "lead_deduplications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    decision: Mapped[str] = mapped_column(String(32))
    lead_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    matched_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(String(1000))

    company_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    company_resolution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    person_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    person_resolution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
