"""Phase 15 — Adaptive Lead Scoring contracts.

Six independently-inspectable numbers, never one opaque score. The hard
gate is absolute: eligible_for_scoring and final_score both come directly
from Phase 12's unchanged HardRuleEvaluation.overall_result —
final_score is None (never a number) whenever that result is FAIL or
HOLD, so nothing downstream can mistake "scored well" for "cleared to
accept." Component scores are still always computed and exposed even when
ineligible, since they are diagnostic ("why did this hold/fail") rather
than a verdict.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.hard_rule_result import OverallResult, ReasonCode


class ScoringWeights(BaseModel):
    """One clear configuration object — never magic numbers scattered
    through the scoring code. Must sum to 1.0; validated on construction
    so an invalid configuration can never silently produce a skewed score.
    """

    model_config = ConfigDict(frozen=True)

    version: str = "default-v1"
    icp_weight: float = Field(default=0.40, ge=0, le=1)
    commercial_weight: float = Field(default=0.30, ge=0, le=1)
    evidence_weight: float = Field(default=0.15, ge=0, le=1)
    freshness_weight: float = Field(default=0.10, ge=0, le=1)
    identity_weight: float = Field(default=0.05, ge=0, le=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ScoringWeights":
        total = self.icp_weight + self.commercial_weight + self.evidence_weight + self.freshness_weight + self.identity_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"scoring weights must sum to 1.0, got {total}")
        return self


DEFAULT_SCORING_WEIGHTS = ScoringWeights()


class FreshnessConfig(BaseModel):
    """Freshness is a configurable decay curve, not a universal rule. No
    default here claims to be "the" correct staleness threshold — a
    different ICP or a later phase may reasonably use a different one.
    """

    model_config = ConfigDict(frozen=True)

    full_credit_within_days: float = Field(default=30.0, ge=0)
    zero_credit_after_days: float = Field(default=180.0, gt=0)

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> "FreshnessConfig":
        if self.zero_credit_after_days <= self.full_credit_within_days:
            raise ValueError("zero_credit_after_days must be greater than full_credit_within_days")
        return self


DEFAULT_FRESHNESS_CONFIG = FreshnessConfig()


class ResolutionSignal(BaseModel):
    """A minimal, DB-agnostic view of one Phase 7/10 resolution record —
    just enough for identity-confidence scoring, without creating a
    second identity-resolution system or coupling the scoring service to
    SQLAlchemy models."""

    model_config = ConfigDict(frozen=True)

    status: str  # "MATCH" | "NEW" | "UNRESOLVED"
    confidence: str | None = None  # "HIGH" when Phase 7/10 set one, else None
    has_conflict: bool = False


class LeadScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    person_id: str | None = None


class LeadScoreResult(BaseModel):
    """The full, in-memory result of one scoring pass. The API layer is
    responsible for persisting this; this schema itself has no DB
    knowledge, mirroring every other *_engine/service result in this
    codebase."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None

    hard_icp_result: OverallResult
    eligible_for_scoring: bool

    icp_score: float
    commercial_score: float
    evidence_score: float
    freshness_score: float | None
    identity_confidence: float
    final_score: float | None

    weights: ScoringWeights
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[ReasonCode, ...]
    explanation: str


class LeadScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    hard_icp_result: str
    eligible_for_scoring: bool
    icp_score: float
    commercial_score: float
    evidence_score: float
    freshness_score: float | None
    identity_confidence: float
    final_score: float | None
    weights_version: str
    weights: dict
    evidence_ids: list[str]
    reason_codes: list[str]
    explanation: str
    scored_at: datetime
