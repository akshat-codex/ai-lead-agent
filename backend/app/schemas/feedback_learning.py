"""Phase 24 — Feedback Learning contracts.

Reuses Phase 4's FeedbackModel/FeedbackDecision UNCHANGED as the only
source of manager feedback — this module defines no second feedback
schema and reads existing rows exactly as Phase 4 persisted them. Every
signal correlated against feedback here is likewise read from an existing
Phase 13/14/15 row, never invented.

This is a LEARNING SIGNAL, not a decision engine: nothing in this module
(or app/services/feedback_learning.py) outputs ACCEPT/REJECT, touches a
hard ICP rule, or produces a second score/qualification. The output is
descriptive statistics — counts, rates, and an honest confidence label
tied to sample size — that a LATER phase (ranking weighting, discovery
strategy, etc.) may choose to read. This phase does not wire that
consumption up; it only produces the analysis.

CONFIDENCE IS SAMPLE-SIZE HONEST: ConfidenceLevel is derived purely from
how much feedback exists, never from how strong or convenient the pattern
looks. A single decision is never reported as a reliable pattern — see
INSUFFICIENT_DATA and the min_sample_size threshold in
app/services/feedback_learning.py.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.feedback import FeedbackDecision


class ConfidenceLevel(str, Enum):
    """How much weight a pattern deserves, based ONLY on sample size —
    never on how clean-looking the correlation is. A pattern from 2
    observations is INSUFFICIENT_DATA even if both agree perfectly."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # below min_sample_size - an observed pattern, not a reliable one
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class ReasonCodeFrequency(BaseModel):
    """How often one reason code appears, broken down by which decision
    it accompanied. Counts only — this module never ranks reason codes
    against each other beyond what the raw counts already show."""

    model_config = ConfigDict(frozen=True)

    reason_code: str
    good_fit_count: int
    weak_fit_count: int
    not_fit_count: int
    hold_count: int
    total_count: int
    confidence: ConfidenceLevel


class SignalCorrelation(BaseModel):
    """How often a specific, already-observed signal (a business-model
    classification, a commercial signal type, a hard-rule pass/hold/fail)
    co-occurred with a positive (GOOD_FIT) vs negative (NOT_FIT) feedback
    decision. `signal_name` is always a "namespace:value" string pointing
    back to a real Phase 13/14/15 field — e.g. "business_model:DTC" or
    "commercial_signal:META_ADVERTISING" — never a fabricated feature."""

    model_config = ConfigDict(frozen=True)

    signal_name: str
    positive_count: int
    negative_count: int
    total_count: int
    positive_rate: float | None  # None when total_count is 0 - never a fabricated 0.0 or 0.5
    confidence: ConfidenceLevel


class FeedbackLearningScope(BaseModel):
    """What this analysis pass covers — an ICP/version, or explicitly
    global/cross-ICP (icp_id=None) only when the caller deliberately asks
    for shared patterns, per the task's "optionally shared/global patterns
    only when evidence supports it" rule."""

    model_config = ConfigDict(frozen=True)

    icp_id: str | None
    icp_version: int | None


class DecisionCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    good_fit: int
    weak_fit: int
    not_fit: int
    hold: int
    total: int


class FeedbackLearningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str | None = None
    min_sample_size: int = Field(default=5, ge=1)


class FeedbackLearningResult(BaseModel):
    """The full, in-memory result of one learning pass. Always produced,
    even with zero feedback (cold start) — is_cold_start and
    overall_confidence make that state explicit rather than silently
    returning empty-looking statistics."""

    model_config = ConfigDict(frozen=True)

    scope: FeedbackLearningScope
    min_sample_size: int
    decision_counts: DecisionCounts
    is_cold_start: bool
    overall_confidence: ConfidenceLevel
    reason_code_frequencies: tuple[ReasonCodeFrequency, ...]
    signal_correlations: tuple[SignalCorrelation, ...]
    explanation: str


class FeedbackLearningSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str | None
    icp_version: int | None
    min_sample_size: int
    total_feedback_count: int
    overall_confidence: str
    result: dict
    created_at: datetime
