"""Phase 9 discovery persistence models.

Append-only, like the Phase 6 company discovery records: a people-discovery
run and the candidates it found are a historical record. Re-running
discovery for the same company/ICP later creates new rows — it never
overwrites or deduplicates against a past run.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PeopleDiscoveryRunModel(Base):
    __tablename__ = "people_discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32))
    requested_limit: Mapped[int] = mapped_column(Integer)
    total_returned: Mapped[int] = mapped_column(Integer)
    provider_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PeopleDiscoveryCandidateModel(Base):
    __tablename__ = "people_discovery_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    # Denormalized from the run (same pattern as DiscoveryCandidateModel)
    # so a candidate row is self-describing without a join.
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)

    provider_id: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
