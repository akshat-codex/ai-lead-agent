"""Phase 28 — lead confidence/readiness snapshot history.

Append-only: every confidence assessment is a new row, preserving exactly
what the readiness classification looked like at that moment — never an
update in place, and never a rewrite of any Phase 11/12/15/16/17/18/19/20/
22 row it read. A snapshot is a frozen record of one assessment; the GET
confidence endpoint always recomputes fresh from current data, and this
table exists purely for historical/audit comparison across time (did this
lead's readiness improve as more evidence/review activity accumulated?).
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class LeadConfidenceSnapshotModel(Base):
    __tablename__ = "lead_confidence_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    readiness: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
