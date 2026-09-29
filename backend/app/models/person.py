"""Phase 10 — canonical person entity + resolution audit trail.

CanonicalPersonModel represents a real-world person, independent of any
ICP and not permanently tied to one company — see the Phase 10 task's
Multi-ICP Requirement and Company Relationship sections.
associated_company_ids is an append-only history of every company this
person has been seen at (a person may change jobs); canonical_company_id is
simply the first one recorded and is never silently overwritten.
PersonResolutionModel is the permanent, append-only audit record of how
each people-discovery candidate was decided — MATCH, NEW, or UNRESOLVED,
and why; a candidate is resolved at most once (see
uq_person_resolution_candidate below).
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CanonicalPersonModel(Base):
    __tablename__ = "canonical_people"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(500))
    canonical_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    associated_company_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    linkedin_id: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    provider_identities: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PersonResolutionModel(Base):
    __tablename__ = "person_resolutions"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_person_resolution_candidate"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(36), index=True)
    people_discovery_run_id: Mapped[str] = mapped_column(String(36), index=True)

    status: Mapped[str] = mapped_column(String(16))
    canonical_person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    matched_person_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    matched_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflicting_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(String(1000))

    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
