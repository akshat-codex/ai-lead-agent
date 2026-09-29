"""Phase 25 — optimization recommendation snapshot history.

Append-only: every optimization pass is a new row, preserving exactly what
was recommended at that moment — never an update in place, and never a
rewrite of the underlying Phase 4 feedback, Phase 24 learning, or Phase 22
ranking rows it read. A snapshot is a frozen record of one set of
recommendations, never itself applied to anything; there is no "status"
column because this table never represents a pending action, only a
historical observation.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class OptimizationSnapshotModel(Base):
    __tablename__ = "optimization_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    min_sample_size: Mapped[int] = mapped_column(Integer)
    is_cold_start: Mapped[bool] = mapped_column(Boolean)

    recommendation_count: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
