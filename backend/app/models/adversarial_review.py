"""Phase 17 — adversarial review history.

Append-only: every adversarial attempt (successful or failed) is a new
row, preserving the full audit trail — never an update in place, and never
an overwrite of the first-pass LLMQualificationModel row it references by
id. Evidence, classification, signal, and score rows are never modified.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AdversarialReviewModel(Base):
    __tablename__ = "adversarial_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    qualification_id: Mapped[str] = mapped_column(String(36), index=True)

    hard_rule_result: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    adversarial_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    contradictions: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    contradicting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    unsupported_claims: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    reasoning_summary: Mapped[str] = mapped_column(String(2000))
    recommendation: Mapped[str] = mapped_column(String(1000))

    provider_id: Mapped[str] = mapped_column(String(128))
    model_id: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))

    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
