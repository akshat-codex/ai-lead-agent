"""Phase 8 discovery-adjacent persistence: enrichment runs and facts.

Append-only, like every other audit trail in this codebase: an enrichment
run and the facts it gathered are historical records. Re-enriching the same
company later adds new rows — it never overwrites or deduplicates against
past facts, since even a repeated identical value from a later run is a
useful corroboration signal for a future evidence process, not noise to be
collapsed away.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class EnrichmentRunModel(Base):
    __tablename__ = "enrichment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32))
    provider_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EnrichmentFactModel(Base):
    __tablename__ = "enrichment_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True)

    field: Mapped[str] = mapped_column(String(255), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    provider_id: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
