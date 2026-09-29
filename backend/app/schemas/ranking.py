"""Phase 22 — Lead Ranking & Prioritization contracts.

Ranking reads what Phases 12/15/16/17/18/19/20/21 already produced and
orders leads by it — it never scores, qualifies, resolves identity,
deduplicates, or reviews anything itself. Every input here is the LATEST
already-persisted row for a given (icp_id, company_id, person_id); nothing
is re-derived, re-evaluated, or guessed when a stage hasn't run yet.

The hard gate is exactly as absolute here as everywhere else in this
pipeline: a hard-rule FAIL can never rank above (or even alongside, on
equal footing with) an eligible lead — see
app/services/lead_ranking.py's RankTier, which places FAIL leads in their
own, always-lowest tier regardless of any other signal.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class RankTier(str, Enum):
    """The coarse bucket a lead falls into before any fine-grained
    ordering happens — ties within a tier are broken by score/evidence/id,
    but a lead in a lower tier never outranks one in a higher tier,
    regardless of any individual signal. Ordered highest-priority first;
    the ordinal position of each member IS the tier's rank precedence."""

    ACCEPTED = "ACCEPTED"  # human-reviewed ACCEPT — the strongest possible signal
    QUALIFIED_STRONG = "QUALIFIED_STRONG"  # hard PASS + GOOD_FIT (or WEAK_FIT), not disproved
    QUALIFIED_WEAK = "QUALIFIED_WEAK"  # hard PASS but qualification/adversarial signal is weaker or absent
    HOLD = "HOLD"  # hard HOLD, or PASS with an unresolved/held qualification-adjacent signal
    REJECTED = "REJECTED"  # human REJECT, qualification NOT_FIT, or adversarial DISPROVED
    DUPLICATE = "DUPLICATE"  # a batch/dedup outcome marking this occurrence a repeat
    HARD_FAILED = "HARD_FAILED"  # hard-rule FAIL — always last, never rescued by any other signal


class RankingReasonCode(str, Enum):
    HARD_RULE_FAIL = "HARD_RULE_FAIL"
    HARD_RULE_HOLD = "HARD_RULE_HOLD"
    HARD_RULE_PASS = "HARD_RULE_PASS"
    HARD_RULE_UNKNOWN = "HARD_RULE_UNKNOWN"
    HUMAN_ACCEPTED = "HUMAN_ACCEPTED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    HUMAN_HELD = "HUMAN_HELD"
    HUMAN_DUPLICATE = "HUMAN_DUPLICATE"
    QUALIFICATION_GOOD_FIT = "QUALIFICATION_GOOD_FIT"
    QUALIFICATION_WEAK_FIT = "QUALIFICATION_WEAK_FIT"
    QUALIFICATION_NOT_FIT = "QUALIFICATION_NOT_FIT"
    QUALIFICATION_HOLD = "QUALIFICATION_HOLD"
    QUALIFICATION_MISSING = "QUALIFICATION_MISSING"
    ADVERSARIAL_SURVIVES = "ADVERSARIAL_SURVIVES"
    ADVERSARIAL_WEAKENED = "ADVERSARIAL_WEAKENED"
    ADVERSARIAL_DISPROVED = "ADVERSARIAL_DISPROVED"
    ADVERSARIAL_HOLD = "ADVERSARIAL_HOLD"
    ADVERSARIAL_MISSING = "ADVERSARIAL_MISSING"
    SCORE_MISSING = "SCORE_MISSING"
    EVIDENCE_CONFLICTS_PRESENT = "EVIDENCE_CONFLICTS_PRESENT"
    VERIFICATION_UNRESOLVED = "VERIFICATION_UNRESOLVED"
    TIE_BROKEN_BY_EVIDENCE_SCORE = "TIE_BROKEN_BY_EVIDENCE_SCORE"
    TIE_BROKEN_BY_LEAD_ID = "TIE_BROKEN_BY_LEAD_ID"
    # Phase 31: company_quality_score exists (Phase 30 ran) but is None —
    # e.g. a lead ranked before the batch orchestrator reached the
    # COMPANY_QUALITY_SCORED stage, or a lead sourced outside the batch
    # pipeline entirely. Diagnostic only, exactly like SCORE_MISSING —
    # never changes the tier, only explains why the company-quality
    # tie-break key sorted this lead as if it had none.
    COMPANY_QUALITY_MISSING = "COMPANY_QUALITY_MISSING"


class RankedLeadSignals(BaseModel):
    """The compact, read-only bundle of latest signals a ranked lead was
    ordered from — every field is None when that pipeline stage has not
    produced a row yet, never fabricated to fill the gap."""

    model_config = ConfigDict(frozen=True)

    hard_rule_result: str | None
    final_score: float | None
    icp_score: float | None
    commercial_score: float | None
    evidence_score: float | None
    freshness_score: float | None
    identity_confidence: float | None
    qualification_decision: str | None
    qualification_confidence: float | None
    adversarial_result: str | None
    adversarial_confidence: float | None
    evidence_has_conflicts: bool
    verification_unresolved: bool
    human_review_decision: str | None
    batch_outcome: str | None
    # Phase 31 (Phase 30's own CompanyQualityResult, read back verbatim —
    # never re-derived here): company_quality_score is None whenever
    # hard_rule_result == FAIL (Phase 30's own "no score for a REJECT"
    # contract) or whenever the Phase 30 stage simply hasn't run yet for
    # this lead. company_quality_label is the raw STRONG/REVIEW/REJECT
    # string, exposed for display/audit — ranking itself keys off the
    # score, not the label (the label is coarser and already implied by
    # hard_rule_result for REJECT; for REVIEW vs STRONG the numeric score
    # is what actually orders leads within a tier).
    company_quality_score: float | None = None
    company_quality_label: str | None = None


class RankedLead(BaseModel):
    """One lead's position in one ranking pass. `rank` is 1-based and
    strictly increasing in the returned order; `tier` explains WHY it sits
    where it does at a coarse level, `reason_codes` explains the specific
    signals that placed it there, and `signals` is the full evidence for
    an auditor to check the ranking's own work."""

    model_config = ConfigDict(frozen=True)

    rank: int
    lead_id: str
    company_id: str
    person_id: str | None
    icp_id: str
    icp_version: int
    tier: RankTier
    # Phase 31: widened from (final_score, evidence_score,
    # qualification_confidence, lead_id) to insert company_quality_score
    # right after final_score — see lead_ranking.py::_tie_break_key's own
    # comment for why it sits there. lead_id stays the final element so
    # every existing reader that only cares about the deterministic
    # tail-tiebreak (e.g. tests indexing [-1]) is unaffected.
    tie_break_key: tuple[float, float, float, float, str]
    reason_codes: tuple[RankingReasonCode, ...]
    signals: RankedLeadSignals
    explanation: str


class RankingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str
    batch_id: str | None = None


class RankingResult(BaseModel):
    """The full, in-memory result of one ranking pass over every lead seen
    under one ICP/version (optionally narrowed to one batch)."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    batch_id: str | None
    ranked_leads: tuple[RankedLead, ...]


class RankingSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    batch_id: str | None
    lead_count: int
    ranked_leads: list[dict]
    created_at: datetime
