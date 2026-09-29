"""Phase 22 — ranking snapshot history.

Append-only: every ranking pass is a new row, preserving exactly what the
ranking looked like at that moment — never an update in place, and never
a rewrite of any Phase 12/15/16/17/18/19/20/21 row it read to produce it.
A snapshot is a frozen record of one ranking computation, not a live view;
GET /api/v1/rankings always recomputes fresh from current data, and this
table exists purely for historical/audit comparison across time.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class RankingSnapshotModel(Base):
    __tablename__ = "ranking_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    lead_count: Mapped[int] = mapped_column(Integer)
    ranked_leads: Mapped[list[dict]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
