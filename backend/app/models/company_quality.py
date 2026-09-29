"""Phase 30 — company-quality scoring history.

Append-only, mirroring every other scoring/qualification/review table in
this codebase (Phase 15 scores, Phase 16 qualifications, Phase 17
adversarial reviews): a new scoring pass always inserts a fresh row, never
updates or overwrites a previous observation.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CompanyQualityModel(Base):
    __tablename__ = "company_quality_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[str] = mapped_column(String(36), index=True)

    hard_rule_result: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(16))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    signals: Mapped[list[dict]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(String(4000))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
