"""Phase 20 — human review decision service.

Pure and DB-free, mirroring every other *_engine/service module: given a
ReviewRequest and an already-assembled PipelineSnapshot, returns a
ReviewResult with no side effects. The caller (app/api/human_review.py)
owns persistence and owns loading the snapshot's underlying rows.

The hard gate is enforced here, structurally, exactly like Phase 15's
final_score/Phase 16's decision gating: a reviewer's ACCEPT can never
actually take effect when the snapshot's hard_rule_result is FAIL — the
effective_decision is force-downgraded to REJECT regardless of what the
reviewer submitted, and status records that this happened
(BLOCKED_BY_HARD_FAIL) rather than silently accepting it. This is the
concrete meaning of "hard ICP failures cannot become ACCEPTED merely
because a reviewer likes the lead."

HOLD, REJECT, and DUPLICATE decisions are never blocked by a hard FAIL —
only an attempted ACCEPT is. A reviewer is always free to reject, hold, or
flag a duplicate regardless of pipeline state; only the one escalation a
hard FAIL must never allow (ACCEPT) is intercepted.
"""
from __future__ import annotations

from app.schemas.human_review import (
    PipelineSnapshot,
    ReviewDecision,
    ReviewExecutionStatus,
    ReviewRequest,
    ReviewResult,
)

_HARD_FAIL = "FAIL"
HARD_ICP_FAILURE_REASON_CODE = "HARD_ICP_FAILURE"


def decide_review(
    request: ReviewRequest,
    icp_version: int,
    snapshot: PipelineSnapshot,
) -> ReviewResult:
    effective_decision = request.decision
    status = ReviewExecutionStatus.RECORDED
    explanation = f"Reviewer decision {request.decision.value} recorded."
    reason_codes = tuple(request.reason_codes)

    if request.decision == ReviewDecision.ACCEPT and snapshot.hard_rule_result == _HARD_FAIL:
        effective_decision = ReviewDecision.REJECT
        status = ReviewExecutionStatus.BLOCKED_BY_HARD_FAIL
        explanation = (
            "Reviewer requested ACCEPT, but this lead's hard ICP result is FAIL — a hard failure can "
            "never become ACCEPTED regardless of reviewer preference. Recorded as REJECT instead."
        )
        # A system-attributed reason code is added (never substituted for
        # the reviewer's own) so the resulting REJECT still satisfies
        # Phase 4's own "non-GOOD_FIT requires a reason code" rule, and so
        # the audit trail is explicit about WHY this became a REJECT even
        # if the reviewer supplied no reason of their own.
        if HARD_ICP_FAILURE_REASON_CODE not in reason_codes:
            reason_codes = (*reason_codes, HARD_ICP_FAILURE_REASON_CODE)

    return ReviewResult(
        lead_id=request.lead_id,
        icp_id=request.icp_id,
        icp_version=icp_version,
        company_id=snapshot.company_id,
        person_id=snapshot.person_id,
        requested_decision=request.decision,
        effective_decision=effective_decision,
        status=status,
        reason_codes=reason_codes,
        reviewer_note=request.reviewer_note,
        reviewer_id=request.reviewer_id,
        duplicate_of_lead_id=request.duplicate_of_lead_id,
        snapshot=snapshot,
        forwarded_feedback_id=None,  # populated by the API layer after submitting Phase 4 feedback
        explanation=explanation,
    )
