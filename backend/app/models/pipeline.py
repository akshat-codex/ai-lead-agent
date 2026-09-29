"""Phase 29 — end-to-end pipeline run coordination record.

PipelineRunModel is a thin, mutable coordination row exactly like Phase
21's BatchModel (which it wraps 1:1 via batch_id) — it is re-computed and
overwritten in place on every run/resume call, matching BatchModel's own
convention of overwriting summary counters rather than growing an audit
log. The actual audit trail for every stage this run touches already
lives in that stage's own append-only tables (Phase 12 validations, Phase
15 scores, Phase 20 human reviews, Phase 22 ranking snapshots, Phase 28
confidence snapshots, ...); this row only remembers which batch a
pipeline run wraps and the last computed downstream summary so it does
not need to be recomputed to answer a status check.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PipelineRunModel(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)

    status: Mapped[str] = mapped_column(String(32), default="PENDING")

    lead_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    held_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    human_review_pending_count: Mapped[int] = mapped_column(Integer, default=0)

    stage_statuses: Mapped[list] = mapped_column(JSON, default=list)
    leads: Mapped[list] = mapped_column(JSON, default=list)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
