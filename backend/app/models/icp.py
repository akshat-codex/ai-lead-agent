"""ICP draft persistence model.

This is the Phase 1 storage shape only: a saved ICP draft, versioned by name.
It is intentionally not the canonical normalized ICP that Phase 2 will
produce — see docs/architecture.md and the ICP schemas for the distinction.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ICPModel(Base):
    __tablename__ = "icps"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_icp_name_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[int] = mapped_column(Integer)
    hard_rules: Mapped[dict] = mapped_column(JSON)
    soft_preferences: Mapped[dict] = mapped_column(JSON)
    # The generic filters[] the ICP was authored from, when submitted that
    # way (see app/schemas/filter_criterion.py). Null/empty for ICPs saved
    # via the original direct hard_rules/soft_preferences path.
    filters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
