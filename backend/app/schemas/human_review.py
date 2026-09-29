"""Phase 20 — Human Review contracts.

A human reviewer makes exactly one operational decision about one
(lead, ICP) pairing: ACCEPT, REJECT, HOLD, or DUPLICATE. This is
deliberately a different, narrower vocabulary than Phase 4's
FeedbackDecision (GOOD_FIT/WEAK_FIT/NOT_FIT/HOLD) — ACCEPT/REJECT/DUPLICATE
are pipeline-state actions ("what happens to this lead next"), not a
qualitative fit judgment. Where a reviewer's decision DOES correspond to a
fit judgment (see _FEEDBACK_MAPPING below), this module submits it through
the existing, unchanged Phase 4 FeedbackCreate/FeedbackModel — it never
redefines or duplicates that contract.

The hard gate is exactly as absolute here as everywhere else in this
pipeline (Phase 3/12/15/16/17): a reviewer's ACCEPT can never actually
persist as ACCEPT when the lead's current hard-rule result is FAIL. See
app/services/human_review.py for where this is enforced structurally, not
just documented.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.feedback import FeedbackDecision


class ReviewDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    HOLD = "HOLD"
    DUPLICATE = "DUPLICATE"


class ReviewExecutionStatus(str, Enum):
    """Distinguishes a reviewer's decision actually taking effect from the
    hard gate overriding it — mirrors Phase 16/17's status/decision split."""

    RECORDED = "RECORDED"
    BLOCKED_BY_HARD_FAIL = "BLOCKED_BY_HARD_FAIL"


# A reviewer's ACCEPT/REJECT maps naturally onto Phase 4's existing
# GOOD_FIT/NOT_FIT vocabulary when the reviewer also supplies one; HOLD
# maps onto Phase 4's own HOLD. DUPLICATE has no Phase 4 equivalent (it is
# a pipeline/dedup outcome, not a fit judgment) and is therefore never
# forwarded to Phase 4 feedback.
_FEEDBACK_MAPPING: dict[ReviewDecision, FeedbackDecision | None] = {
    ReviewDecision.ACCEPT: FeedbackDecision.GOOD_FIT,
    ReviewDecision.REJECT: FeedbackDecision.NOT_FIT,
    ReviewDecision.HOLD: FeedbackDecision.HOLD,
    ReviewDecision.DUPLICATE: None,
}


def feedback_decision_for(decision: ReviewDecision) -> FeedbackDecision | None:
    return _FEEDBACK_MAPPING[decision]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: str = Field(min_length=1)
    icp_id: str = Field(min_length=1)
    decision: ReviewDecision
    reason_codes: list[str] = Field(default_factory=list)
    reviewer_note: str | None = Field(default=None, max_length=2000)
    reviewer_id: str = Field(min_length=1)
    duplicate_of_lead_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> "ReviewRequest":
        self.lead_id = self.lead_id.strip()
        self.reviewer_id = self.reviewer_id.strip()
        if not self.lead_id:
            raise ValueError("lead_id must not be blank")
        if not self.reviewer_id:
            raise ValueError("reviewer_id must not be blank")
        if self.decision == ReviewDecision.DUPLICATE and not self.duplicate_of_lead_id:
            raise ValueError("DUPLICATE decisions must supply duplicate_of_lead_id")
        if self.decision != ReviewDecision.DUPLICATE and self.duplicate_of_lead_id:
            raise ValueError("duplicate_of_lead_id is only valid for a DUPLICATE decision")

        # Every non-ACCEPT decision that forwards into Phase 4 feedback
        # (REJECT -> NOT_FIT, HOLD -> HOLD) must satisfy Phase 4's own
        # "non-GOOD_FIT requires a reason code" rule up front — enforced
        # here too, not just at the point of forwarding, so a request is
        # rejected immediately rather than failing deep inside submission
        # after other side effects may already be queued.
        if self.decision in (ReviewDecision.REJECT, ReviewDecision.HOLD) and not self.reason_codes:
            raise ValueError(f"{self.decision.value} reviews must include at least one reason code")
        return self


class PipelineSnapshot(BaseModel):
    """A compact, read-only view of everything the pipeline currently
    knows about this (company, person, ICP) — assembled from already-
    persisted rows, never re-derived or re-evaluated. Fields are None when
    that pipeline stage has not run yet; this module never fabricates a
    value to fill a gap."""

    model_config = ConfigDict(frozen=True)

    company_id: str
    person_id: str | None

    hard_rule_result: str | None
    hard_rule_reason_codes: tuple[str, ...]
    # The engine's own per-rule explanation text for every FAIL/HOLD rule
    # (e.g. "custom_rule:Sells to providers, not payers: No evidence yet
    # on whether the candidate satisfies custom rule '...' (...)."),
    # verbatim from app/services/hard_rule_engine.py's own RuleResult.explanation
    # — never re-derived or reworded here. Exists so a reviewer judging a
    # HOLD/FAIL lead (most concretely: an unresolved custom rule, which
    # has no other surfaced description anywhere on this snapshot) does
    # not have to separately look up the ICP to see what they're
    # evaluating. Empty when every rule is PASS/NOT_APPLICABLE, or when no
    # hard validation has run yet.
    hard_rule_unresolved_explanations: tuple[str, ...] = ()

    final_score: float | None
    icp_score: float | None
    commercial_score: float | None
    evidence_score: float | None

    qualification_decision: str | None
    qualification_confidence: float | None
    qualification_summary: str | None

    adversarial_result: str | None
    adversarial_confidence: float | None

    evidence_conflicts: tuple[str, ...]
    evidence_missing_critical_fields: tuple[str, ...]

    latest_verification_outcome: str | None


class ReviewQueueItem(BaseModel):
    """One row of the review queue — a lead under one ICP, plus its
    current pipeline snapshot and whether it has already been reviewed
    under that ICP."""

    model_config = ConfigDict(frozen=True)

    lead_id: str
    icp_id: str
    icp_version: int
    snapshot: PipelineSnapshot
    already_reviewed: bool
    latest_review_decision: str | None


class ReviewResult(BaseModel):
    """The full, in-memory result of one review submission. Always
    produced — status distinguishes the reviewer's decision actually
    taking effect from the hard gate overriding it."""

    model_config = ConfigDict(frozen=True)

    lead_id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None

    requested_decision: ReviewDecision
    effective_decision: ReviewDecision
    status: ReviewExecutionStatus

    reason_codes: tuple[str, ...]
    reviewer_note: str | None
    reviewer_id: str
    duplicate_of_lead_id: str | None

    snapshot: PipelineSnapshot
    forwarded_feedback_id: str | None
    explanation: str


class ReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    requested_decision: str
    effective_decision: str
    status: str
    reason_codes: list[str]
    reviewer_note: str | None
    reviewer_id: str
    duplicate_of_lead_id: str | None
    snapshot: dict
    forwarded_feedback_id: str | None
    explanation: str
    created_at: datetime
