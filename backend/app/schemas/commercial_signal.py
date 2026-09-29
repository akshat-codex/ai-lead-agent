"""Phase 14 — Commercial Signal Extraction contracts.

A commercial signal is an *observed behavior* ("this company runs Meta ad
campaigns"), never a company classification (Phase 13's business model)
and never a fitness judgment. Status/confidence are deliberately the exact
same vocabulary as Phase 11's evidence engine (SUPPORTED/CONFLICT/
INSUFFICIENT/UNKNOWN, HIGH/MEDIUM/LOW/UNKNOWN) — this module is a lens over
the same evidence, not a parallel scale.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.evidence import ConfidenceLevel, EvidenceStatus


class CommercialSignalResult(BaseModel):
    """One extraction pass's verdict for one signal type on one company.

    Only ever produced when at least one piece of evidence mentions the
    signal — a signal never mentioned at all is simply absent from the
    result set (see app/services/commercial_signal_extractor.py), the same
    "absence means unknown, not negative" convention Phase 11 established.
    """

    model_config = ConfigDict(frozen=True)

    company_id: str
    signal_type: str
    status: EvidenceStatus
    confidence: ConfidenceLevel
    value: str | None = None
    provider_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    explanation: str


class CommercialSignalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    signal_type: str
    status: str
    confidence: str
    value: str | None
    provider_ids: list[str]
    evidence_ids: list[str]
    first_seen: datetime | None
    last_seen: datetime | None
    explanation: str
    created_at: datetime
