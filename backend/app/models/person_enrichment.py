"""Phase 5 (Contact Enrichment) — person enrichment run persistence.

Deliberately minimal, unlike app/models/enrichment.py's run+fact pair: this
model only records that a run happened and its outcome (status/provider/
error) so the UI has a stable run to poll. Individual facts (email, title,
linkedin_url, ...) are NOT stored here — they are written directly as
EvidenceModel rows (app/models/evidence.py), which is already an
entity-agnostic provenance table with everything an EnrichmentFactModel row
would have (field/value/provider_id/external_id/confidence/retrieved_at)
plus entity_type/entity_id/source_type. Duplicating that as a parallel
PersonEnrichmentFactModel would only re-implement what evidence already does.

Append-only, like every other run-history table in this codebase: enriching
the same person again adds a new row, never overwrites a past one.
"""
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PersonEnrichmentRunModel(Base):
    __tablename__ = "person_enrichment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
