"""Manager feedback API contract (Phase 4).

This phase only captures reliable feedback data — it does not rank, score,
or learn from it. See docs/quality-contract.md §3 for the governing rules:
feedback may later influence ranking/discovery/scoring, but must never
modify or weaken a hard ICP rule.
"""
import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")

# Illustrative reason codes a caller might use — NOT an exhaustive or
# enforced list. Validation only checks that a code is well-formed
# (SCREAMING_SNAKE_CASE), so new reason codes can be introduced without a
# code change here. Kept for documentation/tooling (e.g. a future UI
# autocomplete), never used to reject a code.
KNOWN_REASON_CODES = frozenset(
    {
        "B2B_HEALTHCARE",
        "WHOLESALE_HEAVY",
        "AGENCY",
        "CONSULTANCY",
        "ENTERPRISE",
        "UNCLEAR_BUSINESS_MODEL",
        "FOUNDER_LED",
        "DTC",
        "ECOMMERCE",
        "PAID_ACQUISITION",
        "GROWTH_SIGNAL",
        "RETAIL_EXPANSION",
    }
)


class FeedbackDecision(str, Enum):
    GOOD_FIT = "GOOD_FIT"
    WEAK_FIT = "WEAK_FIT"
    NOT_FIT = "NOT_FIT"
    HOLD = "HOLD"


def _validate_reason_codes(codes: list[str]) -> list[str]:
    cleaned = [c.strip() for c in codes]
    for code in cleaned:
        if not _REASON_CODE_RE.match(code):
            raise ValueError(
                f"invalid reason code '{code}': must be SCREAMING_SNAKE_CASE, e.g. 'FOUNDER_LED'"
            )
    lowered = [c.lower() for c in cleaned]
    if len(lowered) != len(set(lowered)):
        raise ValueError("reason_codes must not contain duplicates")
    return cleaned


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_ref: str = Field(min_length=1)
    icp_id: str = Field(min_length=1)
    decision: FeedbackDecision
    reason_codes: list[str] = Field(default_factory=list)
    reviewer_note: str | None = Field(default=None, max_length=2000)
    reviewer_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> "FeedbackCreate":
        self.lead_ref = self.lead_ref.strip()
        self.reviewer_id = self.reviewer_id.strip()
        if not self.lead_ref:
            raise ValueError("lead_ref must not be blank")
        if not self.reviewer_id:
            raise ValueError("reviewer_id must not be blank")

        self.reason_codes = _validate_reason_codes(self.reason_codes)

        # Per the Phase 0 quality contract: every non-GOOD_FIT decision must
        # carry a structured reason — this is enforced here, not left to the
        # caller's discretion.
        if self.decision != FeedbackDecision.GOOD_FIT and not self.reason_codes:
            raise ValueError(f"{self.decision.value} feedback must include at least one reason code")

        return self


class FeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_ref: str
    icp_id: str
    icp_version: int
    decision: FeedbackDecision
    reason_codes: list[str]
    reviewer_note: str | None
    reviewer_id: str
    created_at: datetime
