"""Phase 16 — LLM qualification history.

Append-only: every qualification attempt (successful or failed) is a new
row, preserving the full audit trail — never an update in place. Evidence,
classification, signal, and score rows themselves are never modified here.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class LLMQualificationModel(Base):
    __tablename__ = "llm_qualifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    hard_rule_result: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(String(2000))
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    commercial_fit_explanation: Mapped[str] = mapped_column(String(2000))
    hard_rule_acknowledgement: Mapped[str] = mapped_column(String(1000))
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list)

    provider_id: Mapped[str] = mapped_column(String(128))
    model_id: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))

    score_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
