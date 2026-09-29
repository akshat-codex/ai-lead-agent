"""Phase 26 — Controlled Optimization Application contracts.

Phase 25 produces advisory OptimizationRecommendations only. This module
is the first place a recommendation can become an explicit, human-approved
EFFECTIVE ADJUSTMENT — never automatically, never merely because
confidence is high, and never in a way that touches an ICP's hard_rules,
the original Phase 15 ScoringWeights, the Phase 5 provider registry
definitions, or any historical feedback/score/ranking row.

FINGERPRINTING: a Phase 25 OptimizationRecommendation has no stable id of
its own (Phase 25 recomputes fresh on every call, by design — see its own
determinism guarantee). This module identifies "the same recommendation"
across repeated computations by a deterministic fingerprint of its
content (icp_id, icp_version, recommendation_type, signal_name, is_global)
— see recommendation_fingerprint() in app/services/optimization_application.py.
Approving/applying is therefore scoped to a specific, reproducible pattern,
not to an ephemeral object identity.

THE EFFECTIVE CONFIGURATION IS A SEPARATE, ADDITIVE OVERLAY: it is read by
this phase's own read-model endpoint only; nothing in this phase reaches
into CanonicalICP, ScoringWeights, or the provider registry and mutates
them. A future ranking/discovery consumer would need to explicitly read
this overlay and choose to apply it — exactly the same "recommend, don't
auto-apply" discipline Phase 25 established, just one layer further in.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.feedback_learning import ConfidenceLevel
from app.schemas.optimization import ExpectedEffect, OptimizationRecommendationType


class ApprovalDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApplicationStatus(str, Enum):
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"


class RecommendationStatus(str, Enum):
    """The read-model status of one fingerprinted recommendation, as seen
    by the pending-recommendations endpoint — derived from this phase's
    own append-only approval/application history, never stored as a
    mutable field on the recommendation itself."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"


# Recommendations below this confidence can never be approved, regardless
# of who tries or how many times. This is the concrete meaning of
# "insufficient-confidence recommendations cannot be applied."
_BLOCKED_CONFIDENCE = frozenset({ConfidenceLevel.INSUFFICIENT_DATA})

# Recommendation types that could plausibly be mistaken for touching a
# hard rule are blocked defensively here too, even though Phase 25 itself
# already never produces a hard-rule-based recommendation — a second,
# independent check at the approval boundary, not reliance on Phase 25
# alone never regressing.
_ALLOWED_RECOMMENDATION_TYPES = frozenset(
    {
        OptimizationRecommendationType.RANKING_TIE_BREAK_NUDGE,
        OptimizationRecommendationType.SOFT_PREFERENCE_WEIGHT_HINT,
        OptimizationRecommendationType.PROVIDER_PRIORITY_HINT,
        OptimizationRecommendationType.DEEPER_VERIFICATION_HINT,
    }
)


class PendingRecommendation(BaseModel):
    """One fingerprinted recommendation as currently computed by Phase 25,
    joined with this phase's own approval/application status. Recomputed
    fresh from Phase 25 every time — never cached or stored as the source
    of truth here."""

    model_config = ConfigDict(frozen=True)

    fingerprint: str
    icp_id: str
    icp_version: int
    is_global: bool
    recommendation_type: OptimizationRecommendationType
    signal_name: str | None
    reason_code: str | None
    sample_count: int
    confidence: ConfidenceLevel
    expected_effect: ExpectedEffect
    magnitude_hint: float
    explanation: str
    status: RecommendationStatus
    is_eligible_for_approval: bool
    ineligibility_reason: str | None


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1)
    icp_id: str = Field(min_length=1)
    icp_version: int
    decision: ApprovalDecision
    approved_by: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=2000)

    # The full recommendation payload at approval time is captured so the
    # approval record is self-contained and auditable even if Phase 25's
    # live recommendation set later changes (new feedback arrives, the
    # pattern disappears, etc.) — approving is a decision about a specific,
    # timestamped observation, not a live subscription to a moving target.
    recommendation_type: OptimizationRecommendationType
    signal_name: str | None = None
    reason_code: str | None = None
    sample_count: int = Field(ge=0)
    confidence: ConfidenceLevel
    expected_effect: ExpectedEffect
    magnitude_hint: float
    is_global: bool = False

    @model_validator(mode="after")
    def _validate(self) -> "ApprovalRequest":
        self.fingerprint = self.fingerprint.strip()
        self.approved_by = self.approved_by.strip()
        if not self.fingerprint:
            raise ValueError("fingerprint must not be blank")
        if not self.approved_by:
            raise ValueError("approved_by must not be blank")
        return self


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fingerprint: str
    icp_id: str
    icp_version: int
    is_global: bool
    decision: str
    approved_by: str
    note: str | None
    recommendation_type: str
    signal_name: str | None
    reason_code: str | None
    sample_count: int
    confidence: str
    expected_effect: str
    magnitude_hint: float
    created_at: datetime


class ApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1)
    icp_id: str = Field(min_length=1)
    icp_version: int
    applied_by: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate(self) -> "ApplicationRequest":
        self.fingerprint = self.fingerprint.strip()
        self.applied_by = self.applied_by.strip()
        if not self.fingerprint:
            raise ValueError("fingerprint must not be blank")
        if not self.applied_by:
            raise ValueError("applied_by must not be blank")
        return self


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rolled_back_by: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate(self) -> "RollbackRequest":
        self.rolled_back_by = self.rolled_back_by.strip()
        if not self.rolled_back_by:
            raise ValueError("rolled_back_by must not be blank")
        return self


class ApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fingerprint: str
    icp_id: str
    icp_version: int
    is_global: bool
    status: str
    approval_id: str
    applied_by: str
    apply_note: str | None
    rolled_back_by: str | None
    rollback_note: str | None
    recommendation_type: str
    signal_name: str | None
    reason_code: str | None
    magnitude_hint: float
    applied_at: datetime
    rolled_back_at: datetime | None


class EffectiveAdjustment(BaseModel):
    """One currently-active (applied, not rolled back) adjustment, as it
    contributes to the effective configuration read model."""

    model_config = ConfigDict(frozen=True)

    recommendation_type: OptimizationRecommendationType
    signal_name: str | None
    reason_code: str | None
    magnitude_hint: float
    expected_effect: ExpectedEffect
    application_id: str
    applied_at: datetime


class EffectiveConfiguration(BaseModel):
    """The read model a future ranking/discovery consumer would query.
    When no optimization is active for this ICP/version, `adjustments` is
    empty and `is_default` is True — the original Phase 15/22 default
    behavior, completely unmodified, is what such a consumer would use.
    This schema is deliberately separate from CanonicalICP, ScoringWeights,
    and the provider registry — nothing here IS any of those; it is only
    ever an additive overlay a consumer may choose to read."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    is_default: bool
    ranking_adjustments: tuple[EffectiveAdjustment, ...]
    soft_preference_hints: tuple[EffectiveAdjustment, ...]
    provider_priority_hints: tuple[EffectiveAdjustment, ...]
    verification_hints: tuple[EffectiveAdjustment, ...]
    generated_at: datetime
