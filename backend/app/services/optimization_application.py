"""Phase 26 — controlled optimization application.

Pure and DB-free, mirroring every other *_engine/service module: given
already-loaded Phase 25 recommendations and already-loaded approval/
application history rows (plain dicts/tuples the caller assembled from
real database rows), this module computes fingerprints, eligibility, and
the effective-configuration read model with no side effects and no
database access. The caller (app/api/optimization_application.py) owns
all persistence.

WHY A HARD RULE CAN NEVER BE TOUCHED HERE: this module never reads or
writes CanonicalICP, HardRuleEvaluation, or any evidence/identity/scoring
table — it only processes OptimizationRecommendation objects (which
Phase 25 already guarantees never represent a hard-rule change) and
records human approve/apply/rollback decisions about them. There is no
code path in this file that could produce a hard-rule mutation even in
principle, because no hard-rule type ever enters it.

DETERMINISM: recommendation_fingerprint() is a pure function of a
recommendation's own content — the same underlying pattern (ICP/version,
recommendation type, signal, global flag) always fingerprints identically
across repeated Phase 25 computations, which is what lets "approve this
recommendation" remain meaningful even though Phase 25 recomputes fresh
every call.
"""
from __future__ import annotations

import hashlib

from app.schemas.feedback_learning import ConfidenceLevel
from app.schemas.optimization import OptimizationRecommendation
from app.schemas.optimization_application import (
    _ALLOWED_RECOMMENDATION_TYPES,
    _BLOCKED_CONFIDENCE,
    EffectiveAdjustment,
    EffectiveConfiguration,
    PendingRecommendation,
    RecommendationStatus,
)


def recommendation_fingerprint(
    icp_id: str,
    icp_version: int,
    recommendation_type: str,
    signal_name: str | None,
    reason_code: str | None,
    is_global: bool,
) -> str:
    """A stable, deterministic identifier for "this specific recommendation
    pattern" — never based on confidence/sample_count/magnitude, which
    change as more feedback arrives; those live on the approval/
    application record as a point-in-time snapshot instead (see
    ApprovalRequest's own fields)."""
    scope = "GLOBAL" if is_global else f"{icp_id}:{icp_version}"
    parts = "|".join([scope, recommendation_type, signal_name or "", reason_code or ""])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


def fingerprint_for_recommendation(rec: OptimizationRecommendation) -> str:
    return recommendation_fingerprint(
        rec.icp_id, rec.icp_version, rec.recommendation_type.value, rec.signal_name, rec.reason_code, rec.is_global
    )


def eligibility_for(recommendation_type_value: str, confidence: ConfidenceLevel) -> tuple[bool, str | None]:
    """Returns (is_eligible, reason_if_not). A recommendation is eligible
    for approval only when both its confidence clears Phase 24's own
    sample-size bar AND its type is one of the known, non-hard-rule
    recommendation types — the second check is a defensive belt-and-
    braces, since Phase 25 itself never emits anything else."""
    if confidence in _BLOCKED_CONFIDENCE:
        return False, f"confidence {confidence.value} is below the minimum required for approval"
    if recommendation_type_value not in {t.value for t in _ALLOWED_RECOMMENDATION_TYPES}:
        return False, f"recommendation type {recommendation_type_value} is not eligible for application"
    return True, None


def build_pending_recommendations(
    recommendations: tuple[OptimizationRecommendation, ...],
    status_by_fingerprint: dict[str, RecommendationStatus],
) -> tuple[PendingRecommendation, ...]:
    """`status_by_fingerprint` is supplied by the caller, derived from its
    own append-only approval/application history — this function never
    infers status from anything other than what it's explicitly given, so
    a recommendation with no history at all is always reported PENDING,
    never guessed into some other state."""
    pending: list[PendingRecommendation] = []
    for rec in recommendations:
        fingerprint = fingerprint_for_recommendation(rec)
        is_eligible, reason = eligibility_for(rec.recommendation_type.value, rec.confidence)
        status = status_by_fingerprint.get(fingerprint, RecommendationStatus.PENDING)
        pending.append(
            PendingRecommendation(
                fingerprint=fingerprint,
                icp_id=rec.icp_id,
                icp_version=rec.icp_version,
                is_global=rec.is_global,
                recommendation_type=rec.recommendation_type,
                signal_name=rec.signal_name,
                reason_code=rec.reason_code,
                sample_count=rec.sample_count,
                confidence=rec.confidence,
                expected_effect=rec.expected_effect,
                magnitude_hint=rec.magnitude_hint,
                explanation=rec.explanation,
                status=status,
                is_eligible_for_approval=is_eligible,
                ineligibility_reason=reason,
            )
        )
    pending.sort(key=lambda p: (-p.sample_count, p.fingerprint))
    return tuple(pending)


def build_effective_configuration(
    icp_id: str,
    icp_version: int,
    active_applications: list[dict],
    generated_at,
) -> EffectiveConfiguration:
    """`active_applications` is a list of plain dicts (one per currently-
    applied, not-rolled-back application row the caller loaded) — never a
    live query performed by this module. Empty input always yields
    is_default=True and zero adjustments in every bucket: the honest
    "nothing has been applied, original defaults stand" state, which is
    also exactly the cold-start behavior."""
    ranking_adjustments: list[EffectiveAdjustment] = []
    soft_preference_hints: list[EffectiveAdjustment] = []
    provider_priority_hints: list[EffectiveAdjustment] = []
    verification_hints: list[EffectiveAdjustment] = []

    for app_row in active_applications:
        adjustment = EffectiveAdjustment(
            recommendation_type=app_row["recommendation_type"],
            signal_name=app_row["signal_name"],
            reason_code=app_row["reason_code"],
            magnitude_hint=app_row["magnitude_hint"],
            expected_effect=app_row["expected_effect"],
            application_id=app_row["id"],
            applied_at=app_row["applied_at"],
        )
        rec_type = app_row["recommendation_type"]
        if rec_type == "RANKING_TIE_BREAK_NUDGE":
            ranking_adjustments.append(adjustment)
        elif rec_type == "SOFT_PREFERENCE_WEIGHT_HINT":
            soft_preference_hints.append(adjustment)
        elif rec_type == "PROVIDER_PRIORITY_HINT":
            provider_priority_hints.append(adjustment)
        elif rec_type == "DEEPER_VERIFICATION_HINT":
            verification_hints.append(adjustment)

    is_default = not (ranking_adjustments or soft_preference_hints or provider_priority_hints or verification_hints)

    return EffectiveConfiguration(
        icp_id=icp_id,
        icp_version=icp_version,
        is_default=is_default,
        ranking_adjustments=tuple(ranking_adjustments),
        soft_preference_hints=tuple(soft_preference_hints),
        provider_priority_hints=tuple(provider_priority_hints),
        verification_hints=tuple(verification_hints),
        generated_at=generated_at,
    )
