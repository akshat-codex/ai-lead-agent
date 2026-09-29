"""Phase 20 — human review history.

Append-only: every review submission is a new row, preserving the full
audit trail — never an update in place, and never an overwrite of a
previous reviewer's decision about the same lead. Evidence, ICP, score,
qualification, and adversarial-review rows are never modified here; the
`snapshot` column is a frozen copy of what the pipeline showed the
reviewer at submission time, not a live reference.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class HumanReviewModel(Base):
    __tablename__ = "human_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    lead_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    requested_decision: Mapped[str] = mapped_column(String(16))
    effective_decision: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))

    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    reviewer_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    reviewer_id: Mapped[str] = mapped_column(String(255))
    duplicate_of_lead_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    forwarded_feedback_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    explanation: Mapped[str] = mapped_column(String(1000))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
