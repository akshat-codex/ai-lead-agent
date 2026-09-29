"""Phase 22 — lead ranking and prioritization.

Pure and DB-free, mirroring every other *_engine/service module: given a
list of already-assembled RankedLeadSignals-plus-identity tuples, returns
a deterministic RankingResult with no side effects and no database access.
The caller (app/api/ranking.py) owns loading the latest Phase 12/15/16/
17/18/19/20/21 rows for each lead under one ICP/version.

This module never re-scores, re-qualifies, re-validates, re-resolves
identity, or re-deduplicates anything — it only orders leads by signals
those phases already produced. A field the pipeline never populated stays
None all the way through; it is never treated as a positive OR negative
value, only as "insufficient information for that specific signal."

TIER ASSIGNMENT (see RankTier for the full ordinal precedence — this is
the ONLY thing that can make one lead outrank another across tiers; within
a tier, only the numeric tie-break key decides order):

  HARD_FAILED   - hard_rule_result == FAIL. Absolute floor: nothing in any
                  other signal (a great score, a GOOD_FIT qualification, a
                  human ACCEPT that predates the failure) can move a lead
                  out of this tier. This is the concrete meaning of "hard
                  rule failures cannot outrank eligible leads."
  DUPLICATE     - the lead's latest batch/dedup signal marks this specific
                  occurrence a repeat of already-ranked work.
  REJECTED      - a human REJECT, or (absent a human decision) a
                  qualification NOT_FIT or an adversarial DISPROVED.
  HOLD          - hard_rule_result == HOLD, or a PASS whose qualification/
                  adversarial signal is itself unresolved (HOLD or absent
                  entirely after the hard gate passed).
  QUALIFIED_WEAK/ACCEPTED/QUALIFIED_STRONG - see _assign_tier for the
                  exact ladder; a human ACCEPT is the strongest possible
                  signal and always wins over an unreviewed PASS.

TIE-BREAK (deterministic, always total-order — never a coin flip):
  1. final_score, descending (None sorts as -infinity: an unscored lead
     never outranks a scored one within the same tier)
  2. company_quality_score, descending (None sorts as -infinity). Phase 31:
     inserted here, right after final_score and before evidence_score,
     because it is the one signal that actually captures discovery
     provenance (structured vs keyword — see
     app/services/company_quality.py) and rule-by-rule fit strength as a
     single number; final_score (Phase 15) has no notion of discovery
     provenance at all, so without this a structured-match company and a
     weak keyword-match company with an identical final_score would tie
     ahead of this key and fall through to evidence_score/lead_id, which
     say nothing about HOW the company was found. This never changes a
     lead's TIER — only its order within one.
  3. evidence_score, descending (None sorts as -infinity)
  4. qualification_confidence, descending (None sorts as -infinity)
  5. lead_id, ascending (a stable, always-present final key so two leads
     with byte-identical signals still resolve to one deterministic order)
"""
from __future__ import annotations

from app.schemas.ranking import (
    RankedLead,
    RankedLeadSignals,
    RankingReasonCode,
    RankingResult,
    RankTier,
)

# A finite sentinel, not float("-inf") — the tie-break key is exposed on
# RankedLead and must round-trip through JSON (the FastAPI response layer
# and the append-only snapshot model both serialize it), and -inf is not
# valid JSON. Every real score/confidence in this codebase is bounded to
# [0, 100], so any value below 0 is already an impossible real score and
# sorts correctly below every actual measurement.
_MISSING_VALUE_SENTINEL = -1.0

# Ordinal precedence — index 0 is the highest-priority tier. Only this
# ordering may make one lead outrank another across tiers.
_TIER_ORDER: tuple[RankTier, ...] = (
    RankTier.ACCEPTED,
    RankTier.QUALIFIED_STRONG,
    RankTier.QUALIFIED_WEAK,
    RankTier.HOLD,
    RankTier.REJECTED,
    RankTier.DUPLICATE,
    RankTier.HARD_FAILED,
)


def _tier_index(tier: RankTier) -> int:
    return _TIER_ORDER.index(tier)


def _assign_tier_and_reasons(signals: RankedLeadSignals) -> tuple[RankTier, tuple[RankingReasonCode, ...]]:
    reasons: list[RankingReasonCode] = []

    # The hard gate is checked FIRST and is absolute — nothing below this
    # point can move a FAIL lead out of HARD_FAILED.
    if signals.hard_rule_result == "FAIL":
        return RankTier.HARD_FAILED, (RankingReasonCode.HARD_RULE_FAIL,)

    if signals.batch_outcome == "DUPLICATE":
        return RankTier.DUPLICATE, (RankingReasonCode.HUMAN_DUPLICATE,)

    if signals.human_review_decision == "REJECT":
        return RankTier.REJECTED, (RankingReasonCode.HUMAN_REJECTED,)
    if signals.human_review_decision == "ACCEPT":
        reasons.append(RankingReasonCode.HUMAN_ACCEPTED)
        return RankTier.ACCEPTED, tuple(reasons)
    if signals.human_review_decision == "HOLD":
        reasons.append(RankingReasonCode.HUMAN_HELD)
        return RankTier.HOLD, tuple(reasons)
    # DUPLICATE as a human decision (flagged by a reviewer, not the batch
    # dedup signal) is treated the same as the batch-level duplicate tier.
    if signals.human_review_decision == "DUPLICATE":
        return RankTier.DUPLICATE, (RankingReasonCode.HUMAN_DUPLICATE,)

    if signals.hard_rule_result == "HOLD":
        reasons.append(RankingReasonCode.HARD_RULE_HOLD)
        return RankTier.HOLD, tuple(reasons)

    if signals.hard_rule_result is None:
        reasons.append(RankingReasonCode.HARD_RULE_UNKNOWN)
        return RankTier.HOLD, tuple(reasons)

    # From here, hard_rule_result == "PASS" and there is no human decision.
    reasons.append(RankingReasonCode.HARD_RULE_PASS)

    if signals.adversarial_result == "DISPROVED":
        reasons.append(RankingReasonCode.ADVERSARIAL_DISPROVED)
        return RankTier.REJECTED, tuple(reasons)
    if signals.qualification_decision == "NOT_FIT":
        reasons.append(RankingReasonCode.QUALIFICATION_NOT_FIT)
        return RankTier.REJECTED, tuple(reasons)

    if signals.qualification_decision is None:
        reasons.append(RankingReasonCode.QUALIFICATION_MISSING)
        return RankTier.HOLD, tuple(reasons)
    if signals.qualification_decision == "HOLD":
        reasons.append(RankingReasonCode.QUALIFICATION_HOLD)
        return RankTier.HOLD, tuple(reasons)
    if signals.adversarial_result == "HOLD":
        reasons.append(RankingReasonCode.ADVERSARIAL_HOLD)
        return RankTier.HOLD, tuple(reasons)

    if signals.qualification_decision == "GOOD_FIT":
        reasons.append(RankingReasonCode.QUALIFICATION_GOOD_FIT)
        if signals.adversarial_result == "SURVIVES":
            reasons.append(RankingReasonCode.ADVERSARIAL_SURVIVES)
            return RankTier.QUALIFIED_STRONG, tuple(reasons)
        if signals.adversarial_result == "WEAKENED":
            reasons.append(RankingReasonCode.ADVERSARIAL_WEAKENED)
            return RankTier.QUALIFIED_WEAK, tuple(reasons)
        if signals.adversarial_result is None:
            reasons.append(RankingReasonCode.ADVERSARIAL_MISSING)
        return RankTier.QUALIFIED_STRONG, tuple(reasons)

    # WEAK_FIT (or any other successful decision this module doesn't treat
    # as strong) is always QUALIFIED_WEAK regardless of adversarial result.
    reasons.append(RankingReasonCode.QUALIFICATION_WEAK_FIT)
    if signals.adversarial_result == "WEAKENED":
        reasons.append(RankingReasonCode.ADVERSARIAL_WEAKENED)
    elif signals.adversarial_result == "SURVIVES":
        reasons.append(RankingReasonCode.ADVERSARIAL_SURVIVES)
    elif signals.adversarial_result is None:
        reasons.append(RankingReasonCode.ADVERSARIAL_MISSING)
    return RankTier.QUALIFIED_WEAK, tuple(reasons)


def _tie_break_key(signals: RankedLeadSignals, lead_id: str) -> tuple[float, float, float, float, str]:
    return (
        signals.final_score if signals.final_score is not None else _MISSING_VALUE_SENTINEL,
        signals.company_quality_score if signals.company_quality_score is not None else _MISSING_VALUE_SENTINEL,
        signals.evidence_score if signals.evidence_score is not None else _MISSING_VALUE_SENTINEL,
        signals.qualification_confidence if signals.qualification_confidence is not None else _MISSING_VALUE_SENTINEL,
        lead_id,
    )


def _diagnostic_reasons(signals: RankedLeadSignals) -> tuple[RankingReasonCode, ...]:
    extra: list[RankingReasonCode] = []
    if signals.final_score is None and signals.hard_rule_result == "PASS":
        extra.append(RankingReasonCode.SCORE_MISSING)
    if signals.company_quality_score is None and signals.hard_rule_result in {"PASS", "HOLD"}:
        extra.append(RankingReasonCode.COMPANY_QUALITY_MISSING)
    if signals.evidence_has_conflicts:
        extra.append(RankingReasonCode.EVIDENCE_CONFLICTS_PRESENT)
    if signals.verification_unresolved:
        extra.append(RankingReasonCode.VERIFICATION_UNRESOLVED)
    return tuple(extra)


def _explain(tier: RankTier, reasons: tuple[RankingReasonCode, ...]) -> str:
    reason_text = ", ".join(r.value for r in reasons) if reasons else "no pipeline signal available yet"
    return f"Tier {tier.value} — {reason_text}."


def rank_leads(
    icp_id: str,
    icp_version: int,
    batch_id: str | None,
    leads: list[tuple[str, str, str | None, RankedLeadSignals]],
) -> RankingResult:
    """`leads` is a list of (lead_id, company_id, person_id, signals)
    tuples — already assembled by the caller from the latest persisted
    rows for each lead. Sorting is a stable, total order: tier first
    (ascending ordinal, i.e. highest priority first), then the numeric/
    lexicographic tie-break key (descending on the numeric parts, which
    is achieved by negating them since Python's sort is ascending by
    default and this function must remain a pure, allocation-light sort
    with no custom comparator surprises).
    """
    scored: list[tuple[int, tuple[float, float, float, float, str], str, str, str | None, RankTier, tuple[RankingReasonCode, ...], RankedLeadSignals]] = []

    for lead_id, company_id, person_id, signals in leads:
        tier, tier_reasons = _assign_tier_and_reasons(signals)
        reasons = tier_reasons + _diagnostic_reasons(signals)
        tie_key = _tie_break_key(signals, lead_id)
        scored.append((_tier_index(tier), tie_key, lead_id, company_id, person_id, tier, reasons, signals))

    # Ascending sort on (tier_index, -final_score, -company_quality_score,
    # -evidence_score, -qualification_confidence, lead_id): negating the
    # four numeric components turns Python's default ascending sort into
    # "highest score/quality/evidence/confidence first" without a custom
    # comparator, while tier_index and lead_id sort normally ascending
    # (lowest tier_index — i.e. highest priority tier — first; lead_id as
    # the final, always-present tiebreaker for byte-identical signals).
    scored.sort(key=lambda row: (row[0], -row[1][0], -row[1][1], -row[1][2], -row[1][3], row[1][4]))

    ranked: list[RankedLead] = []
    for position, (_, tie_key, lead_id, company_id, person_id, tier, reasons, signals) in enumerate(scored, start=1):
        ranked.append(
            RankedLead(
                rank=position,
                lead_id=lead_id,
                company_id=company_id,
                person_id=person_id,
                icp_id=icp_id,
                icp_version=icp_version,
                tier=tier,
                tie_break_key=tie_key,
                reason_codes=reasons,
                signals=signals,
                explanation=_explain(tier, reasons),
            )
        )

    return RankingResult(icp_id=icp_id, icp_version=icp_version, batch_id=batch_id, ranked_leads=tuple(ranked))
