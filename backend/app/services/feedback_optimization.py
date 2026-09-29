"""Phase 25 — feedback-driven optimization.

Pure and DB-free, mirroring every other *_engine/service module: given an
already-computed Phase 24 FeedbackLearningResult and an already-computed
Phase 22 RankingResult, produces OptimizationRecommendations and a
within-tier ranking preview. No database access, no provider call, no
re-scoring, no re-qualification, no re-validation of hard rules.

WHY A HARD FAIL CAN NEVER BE PROMOTED HERE: build_ranking_preview() groups
ranked leads by their EXISTING Phase 22 tier and only ever reorders leads
inside the same group — a lead's tier field is copied through unchanged,
and the preview's own rank numbers are assigned by walking the tiers in
their already-fixed Phase 22 order (see _TIER_ORDER reuse) and, within
each tier, applying the recommended nudge as a pure additive tie-break on
top of the existing tie-break score. A HARD_FAILED lead can only ever be
reordered relative to OTHER HARD_FAILED leads — it is structurally
impossible for it to end up ranked ahead of any non-HARD_FAILED lead,
because tiers are processed in their fixed order and never interleaved.

MIN_SAMPLES_FOR_RECOMMENDATION is the concrete "never learn from a single
decision" guard: a signal/reason-code whose Phase 24 confidence is
INSUFFICIENT_DATA (below Phase 24's own min_sample_size) produces NO
recommendation at all — not a weak one, none. Cold start (zero or
near-zero feedback) always resolves to an empty recommendation list and an
unchanged ranking preview (original_rank == preview_rank for every lead).
"""
from __future__ import annotations

from app.schemas.feedback_learning import ConfidenceLevel, FeedbackLearningResult, SignalCorrelation
from app.schemas.optimization import (
    ExpectedEffect,
    OptimizationRecommendation,
    OptimizationRecommendationType,
    OptimizationResult,
    OptimizationSourceReference,
    RankingAdjustmentPreview,
)
from app.schemas.ranking import RankedLead, RankingResult

# Confidence levels that clear the "never learn from a single decision"
# bar. INSUFFICIENT_DATA never produces a recommendation, no exceptions.
_ELIGIBLE_CONFIDENCE = frozenset({ConfidenceLevel.LOW, ConfidenceLevel.MODERATE, ConfidenceLevel.HIGH})

# A positive/negative rate must clear this margin from 0.5 before it is
# worth recommending anything at all — a 52%/48% split is statistical
# noise at any sample size this system is likely to see, and surfacing it
# as a "hint" would itself be a subtle form of overfitting to chance.
_MIN_RATE_DEVIATION_FROM_NEUTRAL = 0.15

# Bounds the advisory nudge magnitude — deliberately small and capped, so
# even a HIGH-confidence pattern can only ever nudge a within-tier
# tie-break, never overpower the primary score-based ordering Phase 22
# already establishes.
_MAGNITUDE_BY_CONFIDENCE = {
    ConfidenceLevel.LOW: 0.2,
    ConfidenceLevel.MODERATE: 0.5,
    ConfidenceLevel.HIGH: 1.0,
}

_BUSINESS_MODEL_PREFIX = "business_model:"
_COMMERCIAL_SIGNAL_PREFIX = "commercial_signal:"
_HARD_RULE_PREFIX = "hard_rule_result:"


def _expected_effect_for(rate: float) -> ExpectedEffect:
    return ExpectedEffect.INCREASE_PRIORITY if rate > 0.5 else ExpectedEffect.DECREASE_PRIORITY


def _recommendation_for_signal(
    correlation: SignalCorrelation,
    icp_id: str,
    icp_version: int,
    is_global: bool,
    learning_snapshot_id: str | None,
) -> OptimizationRecommendation | None:
    if correlation.confidence not in _ELIGIBLE_CONFIDENCE:
        return None
    if correlation.positive_rate is None:
        return None
    if abs(correlation.positive_rate - 0.5) < _MIN_RATE_DEVIATION_FROM_NEUTRAL:
        return None
    # A hard_rule_result correlation is exposed as observation, never as a
    # rank/preference nudge — nudging priority based on hard-rule outcome
    # would blur into "learning overriding the hard gate," which this
    # phase must never do. It is deliberately excluded from producing any
    # recommendation type at all.
    if correlation.signal_name.startswith(_HARD_RULE_PREFIX):
        return None

    magnitude = _MAGNITUDE_BY_CONFIDENCE[correlation.confidence]
    if correlation.positive_rate < 0.5:
        magnitude = -magnitude

    if correlation.signal_name.startswith(_BUSINESS_MODEL_PREFIX) or correlation.signal_name.startswith(_COMMERCIAL_SIGNAL_PREFIX):
        rec_type = OptimizationRecommendationType.SOFT_PREFERENCE_WEIGHT_HINT
    else:
        rec_type = OptimizationRecommendationType.RANKING_TIE_BREAK_NUDGE

    explanation = (
        f"Signal '{correlation.signal_name}' co-occurred with a "
        f"{'higher' if correlation.positive_rate > 0.5 else 'lower'} GOOD_FIT rate "
        f"({correlation.positive_rate:.0%} across {correlation.total_count} observations, "
        f"confidence={correlation.confidence.value}). Recommendation only — no automatic change applied."
    )

    return OptimizationRecommendation(
        recommendation_type=rec_type,
        icp_id=icp_id,
        icp_version=icp_version,
        is_global=is_global,
        signal_name=correlation.signal_name,
        reason_code=None,
        sample_count=correlation.total_count,
        confidence=correlation.confidence,
        expected_effect=_expected_effect_for(correlation.positive_rate),
        magnitude_hint=round(magnitude, 4),
        explanation=explanation,
        source=OptimizationSourceReference(
            learning_snapshot_id=learning_snapshot_id, signal_name=correlation.signal_name, reason_code=None
        ),
    )


def build_recommendations(
    learning: FeedbackLearningResult,
    icp_id: str,
    icp_version: int,
    learning_snapshot_id: str | None = None,
    include_global_patterns: bool = False,
) -> tuple[OptimizationRecommendation, ...]:
    """Cold start (learning.is_cold_start) always returns an empty tuple —
    the concrete meaning of "if feedback is insufficient, use existing/
    default behavior unchanged." Scope isolation: recommendations are
    only built from `learning`'s own scope; a caller wanting global
    patterns must have already computed `learning` with icp_id=None
    (Phase 24's own global-scope support) and pass include_global_patterns
    to label them correctly here — this module never silently mixes scopes.
    """
    if learning.is_cold_start:
        return ()

    is_global = include_global_patterns and learning.scope.icp_id is None
    recommendations = []
    for correlation in learning.signal_correlations:
        rec = _recommendation_for_signal(correlation, icp_id, icp_version, is_global, learning_snapshot_id)
        if rec is not None:
            recommendations.append(rec)

    # Deterministic order: strongest sample count first, then signal name.
    recommendations.sort(key=lambda r: (-r.sample_count, r.signal_name or ""))
    return tuple(recommendations)


def build_provider_priority_hints(
    learning: FeedbackLearningResult,
    icp_id: str,
    icp_version: int,
    learning_snapshot_id: str | None = None,
) -> tuple[OptimizationRecommendation, ...]:
    """Provider/query prioritization is derived ONLY from commercial-signal
    and business-model correlations that are themselves strong enough to
    recommend (same eligibility bar as build_recommendations) — a signal a
    provider is known to supply (Phase 14's own extraction) that
    correlates with positive feedback suggests that provider's output is
    worth prioritizing in future discovery/enrichment queries. This is
    advisory text only; no query, provider registry entry, or discovery
    strategy is modified."""
    if learning.is_cold_start:
        return ()

    hints = []
    for correlation in learning.signal_correlations:
        if not correlation.signal_name.startswith(_COMMERCIAL_SIGNAL_PREFIX):
            continue
        if correlation.confidence not in _ELIGIBLE_CONFIDENCE:
            continue
        if correlation.positive_rate is None or abs(correlation.positive_rate - 0.5) < _MIN_RATE_DEVIATION_FROM_NEUTRAL:
            continue

        magnitude = _MAGNITUDE_BY_CONFIDENCE[correlation.confidence]
        effect = ExpectedEffect.INCREASE_PROVIDER_USAGE
        if correlation.positive_rate < 0.5:
            magnitude = -magnitude
            effect = ExpectedEffect.DECREASE_PROVIDER_USAGE

        hints.append(
            OptimizationRecommendation(
                recommendation_type=OptimizationRecommendationType.PROVIDER_PRIORITY_HINT,
                icp_id=icp_id,
                icp_version=icp_version,
                is_global=False,
                signal_name=correlation.signal_name,
                reason_code=None,
                sample_count=correlation.total_count,
                confidence=correlation.confidence,
                expected_effect=effect,
                magnitude_hint=round(magnitude, 4),
                explanation=(
                    f"Commercial signal '{correlation.signal_name}' correlates with "
                    f"{'higher' if correlation.positive_rate > 0.5 else 'lower'} GOOD_FIT feedback "
                    f"({correlation.total_count} observations). Providers/queries that surface this signal "
                    f"may deserve {'more' if correlation.positive_rate > 0.5 else 'less'} priority in future discovery — "
                    "advisory only, no discovery configuration changed."
                ),
                source=OptimizationSourceReference(
                    learning_snapshot_id=learning_snapshot_id, signal_name=correlation.signal_name, reason_code=None
                ),
            )
        )
    hints.sort(key=lambda r: (-r.sample_count, r.signal_name or ""))
    return tuple(hints)


_TIER_ORDER: tuple[str, ...] = ("ACCEPTED", "QUALIFIED_STRONG", "QUALIFIED_WEAK", "HOLD", "REJECTED", "DUPLICATE", "HARD_FAILED")


def _lead_signal_names(ranked_lead: RankedLead) -> frozenset[str]:
    signals = ranked_lead.signals
    names = set()
    if signals.qualification_decision is not None:
        names.add(f"qualification_decision:{signals.qualification_decision}")
    if signals.adversarial_result is not None:
        names.add(f"adversarial_result:{signals.adversarial_result}")
    # Business-model/commercial-signal correlations are keyed by signal
    # values this module does not have direct per-lead access to without
    # a DB lookup (that is the API layer's job) — so the pure preview here
    # nudges leads only via recommendation types that reference signals
    # already present on RankedLeadSignals; the API layer is responsible
    # for supplying any additional per-lead signal membership when it
    # wants business-model/commercial-signal nudges reflected in the
    # preview (see app/api/optimization.py's _lead_extra_signals).
    return frozenset(names)


def apply_adjustments_preview(
    ranking: RankingResult,
    recommendations: tuple[OptimizationRecommendation, ...],
    lead_extra_signals: dict[str, frozenset[str]] | None = None,
) -> tuple[RankingAdjustmentPreview, ...]:
    """Builds a read-only, within-tier-only preview. `lead_extra_signals`
    optionally maps lead_id -> a frozenset of "namespace:value" strings
    (e.g. business-model/commercial-signal membership) the API layer
    looked up — never required, since a cold-start or signal-less ranking
    must still preview cleanly as a no-op (every preview_rank == original_rank).
    """
    lead_extra_signals = lead_extra_signals or {}
    nudge_by_signal: dict[str, float] = {}
    applied_by_signal: dict[str, str] = {}
    for rec in recommendations:
        if rec.signal_name is not None:
            nudge_by_signal[rec.signal_name] = nudge_by_signal.get(rec.signal_name, 0.0) + rec.magnitude_hint
            applied_by_signal[rec.signal_name] = rec.recommendation_type.value

    by_tier: dict[str, list[RankedLead]] = {tier: [] for tier in _TIER_ORDER}
    for ranked_lead in ranking.ranked_leads:
        by_tier.setdefault(ranked_lead.tier.value, []).append(ranked_lead)

    previews: list[RankingAdjustmentPreview] = []
    global_rank = 0

    for tier in _TIER_ORDER:
        tier_leads = by_tier.get(tier, [])
        if not tier_leads:
            continue

        scored_leads = []
        for ranked_lead in tier_leads:
            signal_names = _lead_signal_names(ranked_lead) | lead_extra_signals.get(ranked_lead.lead_id, frozenset())
            adjustment = sum(nudge_by_signal.get(name, 0.0) for name in signal_names)
            applied = tuple(sorted({applied_by_signal[name] for name in signal_names if name in applied_by_signal}))
            scored_leads.append((ranked_lead, adjustment, applied))

        # Stable, deterministic within-tier order: higher adjustment first,
        # ties broken by the ORIGINAL Phase 22 rank (never lead_id alone,
        # so a zero-adjustment tier reproduces Phase 22's own order exactly).
        scored_leads.sort(key=lambda item: (-item[1], item[0].rank))

        for ranked_lead, adjustment, applied in scored_leads:
            global_rank += 1
            previews.append(
                RankingAdjustmentPreview(
                    lead_id=ranked_lead.lead_id,
                    tier=tier,
                    original_rank=ranked_lead.rank,
                    preview_rank=global_rank,
                    adjustment_score=round(adjustment, 4),
                    applied_recommendations=applied,
                )
            )

    return tuple(previews)


def build_optimization_result(
    learning: FeedbackLearningResult,
    ranking: RankingResult,
    icp_id: str,
    icp_version: int,
    min_sample_size: int,
    generated_at,
    learning_snapshot_id: str | None = None,
    include_global_patterns: bool = False,
    lead_extra_signals: dict[str, frozenset[str]] | None = None,
) -> OptimizationResult:
    recommendations = build_recommendations(learning, icp_id, icp_version, learning_snapshot_id, include_global_patterns)
    provider_hints = build_provider_priority_hints(learning, icp_id, icp_version, learning_snapshot_id)
    all_recommendations = recommendations + provider_hints

    preview = apply_adjustments_preview(ranking, recommendations, lead_extra_signals)

    if learning.is_cold_start:
        explanation = (
            f"Cold start: only {learning.decision_counts.total} feedback observation(s) available "
            f"(minimum {min_sample_size} required). No recommendations produced; ranking preview is "
            "identical to the existing Phase 22 ranking."
        )
    elif not all_recommendations:
        explanation = (
            f"{learning.decision_counts.total} feedback observations analyzed, but no correlation cleared "
            "the minimum confidence/deviation bar for a recommendation. Ranking preview is unchanged."
        )
    else:
        explanation = (
            f"{len(all_recommendations)} recommendation(s) derived from {learning.decision_counts.total} feedback "
            f"observations. These are advisory only — no ranking, scoring, or ICP configuration was changed."
        )

    return OptimizationResult(
        icp_id=icp_id,
        icp_version=icp_version,
        is_cold_start=learning.is_cold_start,
        min_sample_size=min_sample_size,
        recommendations=all_recommendations,
        ranking_preview=preview,
        explanation=explanation,
        generated_at=generated_at,
    )
