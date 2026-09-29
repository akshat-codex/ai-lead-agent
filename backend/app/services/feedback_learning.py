"""Phase 24 — feedback learning analysis.

Pure and DB-free, mirroring every other *_engine/service module: given
already-loaded feedback rows (Phase 4, unchanged) and, for each, an
already-loaded bundle of correlated signals (Phase 13/14/15 fields the
caller looked up), returns a deterministic FeedbackLearningResult with no
side effects and no database access. The caller (app/api/feedback_learning.py)
owns all persistence and all signal lookups.

WHY THIS CAN NEVER BECOME A SECOND DECISION ENGINE: this module has no
notion of ACCEPT/REJECT, no notion of a hard rule, and no code path that
touches evidence, identity, or scores — it only counts how often an
already-recorded FeedbackDecision co-occurred with an already-recorded
signal value. There is nothing here to "override" a hard gate with,
because this module never produces a gate-like output at all.

OVERFITTING GUARDRAILS (see _confidence_for_sample_size):
  - A pattern build from fewer than `min_sample_size` observations is
    always ConfidenceLevel.INSUFFICIENT_DATA, regardless of how uniform
    those few observations look. One or two matching decisions is an
    anecdote, not a pattern — this module labels it as such rather than
    reporting a deceptively confident rate.
  - Sample counts are always exposed alongside every rate/frequency, so a
    caller can independently judge reliability rather than trust a single
    confidence label blindly.
  - Every result is reproducible: identical feedback + identical signal
    lookups always produce an identical FeedbackLearningResult (plain
    counting and grouping, no randomness, no time-dependent behavior).
"""
from __future__ import annotations

from collections import defaultdict

from app.schemas.feedback import FeedbackDecision
from app.schemas.feedback_learning import (
    ConfidenceLevel,
    DecisionCounts,
    FeedbackLearningResult,
    FeedbackLearningScope,
    ReasonCodeFrequency,
    SignalCorrelation,
)

# Below this many total observations, a rate is reported as an observed
# but unreliable pattern (INSUFFICIENT_DATA) — never hidden, just labeled
# honestly. Between this and the next threshold, LOW; then MODERATE; then
# HIGH. These are the only three real tiers above the floor — deliberately
# coarse, since manufacturing finer distinctions than the sample size
# actually supports would itself be a form of overfitting.
_LOW_CONFIDENCE_THRESHOLD_MULTIPLIER = 2
_MODERATE_CONFIDENCE_THRESHOLD_MULTIPLIER = 4


def _confidence_for_sample_size(count: int, min_sample_size: int) -> ConfidenceLevel:
    if count < min_sample_size:
        return ConfidenceLevel.INSUFFICIENT_DATA
    if count < min_sample_size * _LOW_CONFIDENCE_THRESHOLD_MULTIPLIER:
        return ConfidenceLevel.LOW
    if count < min_sample_size * _MODERATE_CONFIDENCE_THRESHOLD_MULTIPLIER:
        return ConfidenceLevel.MODERATE
    return ConfidenceLevel.HIGH


class FeedbackObservation:
    """One feedback row plus the signals observed for the same lead at
    the time of analysis — a plain, DB-agnostic bundle the caller builds
    from real Phase 4/13/14/15 rows. `signals` is a set of "namespace:value"
    strings (e.g. {"business_model:DTC", "commercial_signal:META_ADVERTISING",
    "hard_rule_result:PASS"}) — never a numeric feature invented for this
    module; each entry must trace back to an existing pipeline field."""

    __slots__ = ("decision", "reason_codes", "signals")

    def __init__(self, decision: FeedbackDecision, reason_codes: tuple[str, ...], signals: frozenset[str]):
        self.decision = decision
        self.reason_codes = reason_codes
        self.signals = signals


def _decision_counts(observations: list[FeedbackObservation]) -> DecisionCounts:
    counts = {d: 0 for d in FeedbackDecision}
    for obs in observations:
        counts[obs.decision] += 1
    total = len(observations)
    return DecisionCounts(
        good_fit=counts[FeedbackDecision.GOOD_FIT],
        weak_fit=counts[FeedbackDecision.WEAK_FIT],
        not_fit=counts[FeedbackDecision.NOT_FIT],
        hold=counts[FeedbackDecision.HOLD],
        total=total,
    )


def _reason_code_frequencies(observations: list[FeedbackObservation], min_sample_size: int) -> tuple[ReasonCodeFrequency, ...]:
    per_code: dict[str, dict[FeedbackDecision, int]] = defaultdict(lambda: {d: 0 for d in FeedbackDecision})
    for obs in observations:
        for code in obs.reason_codes:
            per_code[code][obs.decision] += 1

    frequencies = []
    for code, counts in per_code.items():
        total = sum(counts.values())
        frequencies.append(
            ReasonCodeFrequency(
                reason_code=code,
                good_fit_count=counts[FeedbackDecision.GOOD_FIT],
                weak_fit_count=counts[FeedbackDecision.WEAK_FIT],
                not_fit_count=counts[FeedbackDecision.NOT_FIT],
                hold_count=counts[FeedbackDecision.HOLD],
                total_count=total,
                confidence=_confidence_for_sample_size(total, min_sample_size),
            )
        )
    # Deterministic order: by descending total count, then alphabetically
    # by reason code — never insertion order, which would depend on
    # whatever order the caller happened to load rows in.
    frequencies.sort(key=lambda f: (-f.total_count, f.reason_code))
    return tuple(frequencies)


_POSITIVE_DECISIONS = frozenset({FeedbackDecision.GOOD_FIT})
_NEGATIVE_DECISIONS = frozenset({FeedbackDecision.NOT_FIT})


def _signal_correlations(observations: list[FeedbackObservation], min_sample_size: int) -> tuple[SignalCorrelation, ...]:
    per_signal: dict[str, dict[str, int]] = defaultdict(lambda: {"positive": 0, "negative": 0, "total": 0})
    for obs in observations:
        for signal in obs.signals:
            bucket = per_signal[signal]
            bucket["total"] += 1
            if obs.decision in _POSITIVE_DECISIONS:
                bucket["positive"] += 1
            elif obs.decision in _NEGATIVE_DECISIONS:
                bucket["negative"] += 1
            # WEAK_FIT/HOLD observations count toward total (the signal was
            # genuinely present) but are neither positive nor negative —
            # never forced into one bucket to inflate a rate.

    correlations = []
    for signal, bucket in per_signal.items():
        classified = bucket["positive"] + bucket["negative"]
        positive_rate = bucket["positive"] / classified if classified > 0 else None
        correlations.append(
            SignalCorrelation(
                signal_name=signal,
                positive_count=bucket["positive"],
                negative_count=bucket["negative"],
                total_count=bucket["total"],
                positive_rate=positive_rate,
                confidence=_confidence_for_sample_size(bucket["total"], min_sample_size),
            )
        )
    correlations.sort(key=lambda c: (-c.total_count, c.signal_name))
    return tuple(correlations)


def analyze_feedback(
    icp_id: str | None,
    icp_version: int | None,
    observations: list[FeedbackObservation],
    min_sample_size: int = 5,
) -> FeedbackLearningResult:
    """Pure orchestration: no database access, no randomness, no wall-clock
    read. Identical (icp_id, icp_version, observations, min_sample_size)
    always produces an identical result.
    """
    decision_counts = _decision_counts(observations)
    is_cold_start = decision_counts.total < min_sample_size
    overall_confidence = _confidence_for_sample_size(decision_counts.total, min_sample_size)

    reason_frequencies = _reason_code_frequencies(observations, min_sample_size)
    correlations = _signal_correlations(observations, min_sample_size)

    scope = FeedbackLearningScope(icp_id=icp_id, icp_version=icp_version)

    if is_cold_start:
        explanation = (
            f"Only {decision_counts.total} feedback observation(s) available "
            f"(minimum {min_sample_size} required) — patterns below are observed, not reliable. "
            "Treat this as a cold-start state; avoid acting on any single pattern here."
        )
    else:
        explanation = (
            f"{decision_counts.total} feedback observations analyzed "
            f"({decision_counts.good_fit} GOOD_FIT, {decision_counts.weak_fit} WEAK_FIT, "
            f"{decision_counts.not_fit} NOT_FIT, {decision_counts.hold} HOLD). "
            f"Overall confidence: {overall_confidence.value}."
        )

    return FeedbackLearningResult(
        scope=scope,
        min_sample_size=min_sample_size,
        decision_counts=decision_counts,
        is_cold_start=is_cold_start,
        overall_confidence=overall_confidence,
        reason_code_frequencies=reason_frequencies,
        signal_correlations=correlations,
        explanation=explanation,
    )
