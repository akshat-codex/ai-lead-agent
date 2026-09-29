"""Phase 24 — feedback learning snapshot history.

Append-only: every learning analysis pass is a new row, preserving exactly
what the analysis looked like at that moment — never an update in place,
and never a rewrite of the underlying Phase 4 feedback rows it read (those
remain permanently immutable, per Phase 4's own append-only rule). A
snapshot is a frozen record of one analysis, not a live view; the GET
analysis endpoint always recomputes fresh from current feedback, and this
table exists purely for historical/audit comparison across time (did our
learned confidence improve as more feedback accumulated?).
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class FeedbackLearningSnapshotModel(Base):
    __tablename__ = "feedback_learning_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    icp_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_sample_size: Mapped[int] = mapped_column(Integer)

    total_feedback_count: Mapped[int] = mapped_column(Integer)
    overall_confidence: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
