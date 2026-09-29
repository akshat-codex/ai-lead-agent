"""Phase 15 — lead score history.

Append-only: a new scoring pass is always a new row, preserving every
prior score even when the same (icp_id, company_id, person_id) is scored
again later with different evidence, weights, or ICP version — never an
update in place. Evidence/classification/signal rows themselves are never
modified by this module.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class LeadScoreModel(Base):
    __tablename__ = "lead_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    hard_icp_result: Mapped[str] = mapped_column(String(16))
    eligible_for_scoring: Mapped[bool] = mapped_column(Boolean)

    icp_score: Mapped[float] = mapped_column(Float)
    commercial_score: Mapped[float] = mapped_column(Float)
    evidence_score: Mapped[float] = mapped_column(Float)
    freshness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    identity_confidence: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    weights_version: Mapped[str] = mapped_column(String(64))
    weights: Mapped[dict] = mapped_column(JSON)

    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(String(2000))

    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
