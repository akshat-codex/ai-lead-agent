"""Phase 18 — multi-source verification history.

Append-only: every verification attempt (resolved, unresolved, held, or
unexecuted) is a new row, preserving the full audit trail — never an
update in place. Evidence rows themselves are never modified or deleted;
this table only ever records what happened when verification tried to add
more of them for one entity+field.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class FieldVerificationModel(Base):
    __tablename__ = "field_verifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    entity_type: Mapped[str] = mapped_column(String(16), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    field: Mapped[str] = mapped_column(String(255), index=True)

    trigger: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)

    original_status: Mapped[str] = mapped_column(String(16))
    original_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    resulting_status: Mapped[str] = mapped_column(String(16))
    new_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    all_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    providers_consulted: Mapped[list[dict]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(String(2000))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
