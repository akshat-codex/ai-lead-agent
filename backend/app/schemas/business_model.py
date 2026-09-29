"""Phase 13 — Business Model Classification contracts.

Describes what a canonical company appears to be commercially (B2B, DTC,
Marketplace, ...) — never whether it fits an ICP, and never a score. See
app/services/business_model_classifier.py for the full boundary.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.schemas.evidence import ConfidenceLevel


class BusinessModel(str, Enum):
    B2C = "B2C"
    DTC = "DTC"
    B2B = "B2B"
    B2B2C = "B2B2C"
    MARKETPLACE = "MARKETPLACE"
    AGENCY = "AGENCY"
    CONSULTANCY = "CONSULTANCY"
    WHOLESALE = "WHOLESALE"
    RETAIL = "RETAIL"
    SERVICE = "SERVICE"
    HYBRID = "HYBRID"
    UNKNOWN = "UNKNOWN"


class ClassificationStatus(str, Enum):
    CLASSIFIED = "CLASSIFIED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class BusinessModelClassificationResult(BaseModel):
    """One classification pass over a company's current evidence.

    primary_model is BusinessModel.HYBRID (not one of the concrete models)
    whenever evidence independently supports two or more distinct models —
    secondary_models then lists exactly which ones. HYBRID is never used as
    a stand-in for "sources disagree"; see conflicting_evidence_ids for
    that case, which can be populated even when a confident primary_model
    was still reached from other, unrelated evidence.
    """

    model_config = ConfigDict(frozen=True)

    company_id: str
    primary_model: BusinessModel
    secondary_models: tuple[BusinessModel, ...] = ()
    status: ClassificationStatus
    confidence: ConfidenceLevel
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    explanation: str


class BusinessModelClassificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    primary_model: str
    secondary_models: list[str]
    status: str
    confidence: str
    supporting_evidence_ids: list[str]
    conflicting_evidence_ids: list[str]
    explanation: str
    classified_at: datetime
