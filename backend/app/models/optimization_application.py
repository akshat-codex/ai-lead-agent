"""Phase 26 — optimization approval/application audit history.

Both tables are append-only: an approval decision or an application/
rollback event is always a new row, never an update in place. "Rolling
back" an application does NOT delete or edit the original APPLIED row —
it appends a new event row with status=ROLLED_BACK referencing the same
fingerprint, so the full history (approved -> applied -> rolled back) is
always reconstructable and no prior state is ever destroyed.

`uq_application_active_fingerprint` is NOT a table-level constraint here
(SQLite/SQLAlchemy would need a partial/filtered unique index to express
"at most one currently-active row per fingerprint", which is awkward
across backends) — the "duplicate application prevented" and "rollback
restores previous state" rules are instead enforced by the service layer
reading the latest event per fingerprint before deciding whether a new
APPLIED row is allowed, exactly like Phase 19's own dedup-then-insert
pattern for canonical leads.
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class OptimizationApprovalModel(Base):
    __tablename__ = "optimization_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False)

    decision: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    recommendation_type: Mapped[str] = mapped_column(String(64))
    signal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String(32))
    expected_effect: Mapped[str] = mapped_column(String(32))
    magnitude_hint: Mapped[float] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OptimizationApplicationModel(Base):
    __tablename__ = "optimization_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    icp_id: Mapped[str] = mapped_column(String(36), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(16))  # APPLIED | ROLLED_BACK
    approval_id: Mapped[str] = mapped_column(String(36), index=True)

    applied_by: Mapped[str] = mapped_column(String(255))
    apply_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    rolled_back_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rollback_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    recommendation_type: Mapped[str] = mapped_column(String(64))
    signal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    magnitude_hint: Mapped[float] = mapped_column(Float)
    expected_effect: Mapped[str] = mapped_column(String(32))

    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
