"""Phase 6 discovery persistence models.

Append-only, like Feedback (Phase 4): a discovery run and the candidates it
found are a historical record. Re-running discovery for the same ICP later
creates new rows — it never overwrites or updates these.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DiscoveryRunModel(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    requested_limit: Mapped[int] = mapped_column(Integer)
    total_returned: Mapped[int] = mapped_column(Integer)
    provider_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscoveryCandidateModel(Base):
    __tablename__ = "discovery_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    # Denormalized from the run (same pattern as FeedbackModel.icp_version)
    # so a candidate row is self-describing without a join.
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)

    provider_id: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
