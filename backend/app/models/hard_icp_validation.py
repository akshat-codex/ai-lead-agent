"""Phase 12 — evidence-backed hard ICP validation audit trail.

Append-only, like every other audit trail in this codebase: re-validating
the same ICP+company+person combination later adds a new row — it never
overwrites a previous result, preserving history by ICP version.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class HardIcpValidationModel(Base):
    __tablename__ = "hard_icp_validations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    overall_result: Mapped[str] = mapped_column(String(16))
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    failed_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    unresolved_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(String(2000))
    evidence_ids: Mapped[dict[str, list[str]]] = mapped_column(JSON, default=dict)

    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
