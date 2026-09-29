"""Manager feedback persistence model (Phase 4).

Feedback rows are append-only: there is no update/delete path anywhere in
this codebase (see app/api/feedback.py) — a new decision about the same lead
is a new row, never an edit of a previous one. icp_version is copied from
the referenced ICP at submission time so a feedback record stays meaningful
even if that ICP is later superseded by a new version.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class FeedbackModel(Base):
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # No leads/companies table exists yet (that's Phase 6+), so this is an
    # opaque caller-supplied reference with no existence check — unlike
    # icp_id below, which references a table that already exists.
    lead_ref: Mapped[str] = mapped_column(String(255), index=True)

    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)

    decision: Mapped[str] = mapped_column(String(32))
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    reviewer_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    reviewer_id: Mapped[str] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
