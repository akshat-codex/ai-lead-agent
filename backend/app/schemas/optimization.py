"""Phase 25 — Feedback-Driven Optimization contracts.

This module turns a Phase 24 FeedbackLearningResult into RECOMMENDATIONS
only — never an automatic mutation of ranking, scoring, or ICP state.
Every recommendation is a plain, inspectable object; applying one is a
separate, explicit, future action this phase does not perform.

STRUCTURAL SAFETY (why this can never become a second decision engine):
  - This module has no code path that writes to CanonicalICP, ScoringWeights,
    HardRuleEvaluation, or any evidence/identity table. It only reads a
    Phase 24 result and a Phase 22 ranking result and derives a proposed,
    additive tie-break nudge plus advisory text.
  - A RankingAdjustment's `tier` is always copied verbatim from the
    Phase 22 RankedLead it references — this module cannot assign or
    change a tier, and therefore cannot move a HARD_FAILED lead anywhere
    else. See app/services/feedback_optimization.py's
    apply_adjustments_preview() for the one place a "what-if" reordering
    is computed, which re-sorts only WITHIN each existing tier — the tier
    boundaries themselves (and therefore the hard-FAIL floor) are
    untouched, exactly like Phase 22's own tie-break never crosses tiers.
  - Every recommendation carries its own confidence (copied from Phase 24,
    itself sample-size-honest) and sample_count — a caller can and should
    refuse to act on anything below a threshold it chooses; this module
    additionally refuses to even PRODUCE certain recommendation types
    (see MIN_SAMPLES_FOR_RECOMMENDATION) when the evidence is too thin,
    directly implementing "never learn from a single decision."
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.feedback_learning import ConfidenceLevel


class OptimizationRecommendationType(str, Enum):
    RANKING_TIE_BREAK_NUDGE = "RANKING_TIE_BREAK_NUDGE"
    SOFT_PREFERENCE_WEIGHT_HINT = "SOFT_PREFERENCE_WEIGHT_HINT"
    PROVIDER_PRIORITY_HINT = "PROVIDER_PRIORITY_HINT"
    DEEPER_VERIFICATION_HINT = "DEEPER_VERIFICATION_HINT"


class ExpectedEffect(str, Enum):
    """A qualitative, honest label for what a recommendation is expected
    to do — never a fabricated numeric guarantee. Phase 24's own
    confidence already carries the reliability signal; this only
    describes direction."""

    INCREASE_PRIORITY = "INCREASE_PRIORITY"
    DECREASE_PRIORITY = "DECREASE_PRIORITY"
    INCREASE_PROVIDER_USAGE = "INCREASE_PROVIDER_USAGE"
    DECREASE_PROVIDER_USAGE = "DECREASE_PROVIDER_USAGE"
    PRIORITIZE_VERIFICATION = "PRIORITIZE_VERIFICATION"
    NO_EFFECT_COLD_START = "NO_EFFECT_COLD_START"


class OptimizationSourceReference(BaseModel):
    """Provenance back to the exact Phase 24 analysis (and, transitively,
    the Phase 4 feedback rows it summarized) a recommendation came from."""

    model_config = ConfigDict(frozen=True)

    learning_snapshot_id: str | None
    signal_name: str | None
    reason_code: str | None


class OptimizationRecommendation(BaseModel):
    """One recommendation. `sample_count` and `confidence` are copied
    verbatim from the Phase 24 signal/reason-code they derive from — never
    recomputed or inflated. A recommendation is advisory only: nothing in
    this schema or the service that produces it has an "apply" side
    effect."""

    model_config = ConfigDict(frozen=True)

    recommendation_type: OptimizationRecommendationType
    icp_id: str
    icp_version: int
    is_global: bool  # true only for a cross-ICP pattern strong enough to justify sharing (see the service)

    signal_name: str | None
    reason_code: str | None
    sample_count: int
    confidence: ConfidenceLevel
    expected_effect: ExpectedEffect
    magnitude_hint: float  # a small, bounded [-1.0, 1.0] nudge strength — advisory only, never applied automatically

    explanation: str
    source: OptimizationSourceReference


class RankingAdjustmentPreview(BaseModel):
    """A read-only, "what-if" preview of how one lead's position WITHIN
    its existing Phase 22 tier would shift if the recommendations below
    were applied. `tier` is copied verbatim from Phase 22 and can never
    change here — only `preview_rank` (still scoped to the same tier
    ordering) may differ from `original_rank`."""

    model_config = ConfigDict(frozen=True)

    lead_id: str
    tier: str
    original_rank: int
    preview_rank: int
    adjustment_score: float
    applied_recommendations: tuple[str, ...]  # recommendation_type values that contributed


class OptimizationResult(BaseModel):
    """The full, in-memory result of one optimization pass. Always
    produced, even with zero recommendations (cold start) — is_cold_start
    makes that state explicit."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    is_cold_start: bool
    min_sample_size: int
    recommendations: tuple[OptimizationRecommendation, ...]
    ranking_preview: tuple[RankingAdjustmentPreview, ...]
    explanation: str
    generated_at: datetime


class OptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    batch_id: str | None = None
    min_sample_size: int = Field(default=5, ge=1)
    include_global_patterns: bool = False


class OptimizationSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    min_sample_size: int
    is_cold_start: bool
    recommendation_count: int
    result: dict
    created_at: datetime
