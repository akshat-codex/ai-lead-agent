"""Phase 13 — business model classification history.

Append-only: a new classification is always a new row, preserving every
prior result even when new evidence changes the outcome — never an update
in place. Evidence records themselves (app/models/evidence.py) are never
modified by this module.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class BusinessModelClassificationModel(Base):
    __tablename__ = "business_model_classifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    primary_model: Mapped[str] = mapped_column(String(32))
    secondary_models: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[str] = mapped_column(String(16))
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflicting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(String(1000))
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
