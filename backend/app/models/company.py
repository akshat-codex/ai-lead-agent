"""Phase 7 — canonical company entity + resolution audit trail.

CanonicalCompanyModel represents a real-world company entity, independent
of any ICP — the same row is reused across every ICP that discovers it (see
the Phase 7 task's Multi-ICP Requirement and docs/architecture.md).
CompanyResolutionModel is the permanent, append-only audit record of how
each discovery candidate was decided — MATCH, NEW, or UNRESOLVED — and why;
a candidate is resolved at most once (see uq_resolution_candidate below).
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CanonicalCompanyModel(Base):
    __tablename__ = "canonical_companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(500))
    canonical_domain: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider_identities: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanyResolutionModel(Base):
    __tablename__ = "company_resolutions"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_resolution_candidate"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(36), index=True)
    discovery_run_id: Mapped[str] = mapped_column(String(36), index=True)

    status: Mapped[str] = mapped_column(String(16))
    canonical_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    matched_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    matched_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflicting_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(String(1000))

    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
