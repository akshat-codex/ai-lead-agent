"""Phase 17 — Adversarial Second Pass contracts.

The adversarial reviewer asks "why might this lead NOT fit?" using the same
underlying evidence Phase 16 used, plus the first-pass qualification result
itself (so it can be specifically challenged, not re-derived from scratch).
It is a second, independent LLM call — never a re-ask of "was your answer
right?" to the same model/prompt.

The hard gate is exactly as absolute as Phase 16's: a FAIL/HOLD hard-rule
result is never even sent to an adversarial LLM call — see
app/services/adversarial_review.py. AdversarialResult.DISPROVED/WEAKENED/
SURVIVES can only ever be produced for a PASS-eligible, already-qualified
candidate.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import EvidenceBrief, QualificationContext, QualificationDecision


class AdversarialResult(str, Enum):
    SURVIVES = "SURVIVES"
    WEAKENED = "WEAKENED"
    DISPROVED = "DISPROVED"
    HOLD = "HOLD"
    NOT_EXECUTED = "NOT_EXECUTED"  # backend-only, for a hard FAIL/HOLD — never an LLM output


class AdversarialExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    HARD_BLOCKED = "HARD_BLOCKED"  # first-pass hard-rule result was FAIL/HOLD - adversarial LLM never called
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    INVALID_EVIDENCE_IDS = "INVALID_EVIDENCE_IDS"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"


# --- compact adversarial input ---------------------------------------------


class FirstPassBrief(BaseModel):
    """A compact view of the first-pass Phase 16 qualification — enough
    for the adversarial reviewer to challenge specific claims without
    resending the entire qualification row."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    decision: QualificationDecision | None
    confidence: float | None
    summary: str
    supporting_evidence_ids: tuple[str, ...]
    risk_evidence_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    commercial_fit_explanation: str
    uncertainties: tuple[str, ...]


class AdversarialContext(BaseModel):
    """The complete, compact bundle sent to an adversarial LLM call.
    Deliberately reuses QualificationContext (Phase 16) rather than
    re-deriving ICP/hard-rule/business-model/commercial-signal/score/
    evidence fields a second time — this schema only adds what is new to
    Phase 17: the first-pass result being challenged.
    """

    model_config = ConfigDict(frozen=True)

    base: QualificationContext
    first_pass: FirstPassBrief

    @property
    def icp_id(self) -> str:
        return self.base.icp_id

    @property
    def icp_version(self) -> int:
        return self.base.icp_version

    @property
    def company_id(self) -> str:
        return self.base.company_id

    @property
    def person_id(self) -> str | None:
        return self.base.person_id

    @property
    def hard_rule_result(self) -> OverallResult:
        return self.base.hard_rule_result

    def known_evidence_ids(self) -> frozenset[str]:
        return self.base.known_evidence_ids()


# --- strict adversarial output ----------------------------------------


class RawAdversarialOutput(BaseModel):
    """The exact JSON shape an adversarial LLMProvider must return."""

    model_config = ConfigDict(extra="forbid")

    adversarial_result: AdversarialResult
    confidence: float = Field(ge=0, le=100)
    contradictions: list[str] = Field(default_factory=list)
    risk_codes: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field(min_length=1, max_length=2000)
    recommendation: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _result_cannot_be_not_executed(self) -> "RawAdversarialOutput":
        # NOT_EXECUTED is a backend-only outcome for a hard FAIL/HOLD; an
        # LLM claiming it would be reasoning about a case it should never see.
        if self.adversarial_result == AdversarialResult.NOT_EXECUTED:
            raise ValueError("an adversarial LLM output must never assign NOT_EXECUTED")
        return self

    @model_validator(mode="after")
    def _disproved_requires_a_contradiction(self) -> "RawAdversarialOutput":
        # DISPROVED must reflect a genuine, cited contradiction — never
        # merely "evidence was missing" (that belongs to HOLD/WEAKENED).
        if self.adversarial_result == AdversarialResult.DISPROVED and not (self.contradictions or self.contradicting_evidence_ids):
            raise ValueError("DISPROVED requires at least one contradiction or contradicting evidence id")
        return self


class AdversarialReviewResult(BaseModel):
    """The full, in-memory, audited result of one adversarial attempt.
    Always produced, even on failure — status distinguishes a genuine
    adversarial judgment from a provider/validation failure."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    qualification_id: str

    hard_rule_result: OverallResult
    status: AdversarialExecutionStatus
    adversarial_result: AdversarialResult | None
    confidence: float | None
    contradictions: tuple[str, ...]
    risk_codes: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    reasoning_summary: str
    recommendation: str

    provider_id: str
    model_id: str
    prompt_version: str

    error_message: str | None = None


class AdversarialReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qualification_id: str = Field(min_length=1)


class AdversarialReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    qualification_id: str
    hard_rule_result: str
    status: str
    adversarial_result: str | None
    confidence: float | None
    contradictions: list[str]
    risk_codes: list[str]
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    unsupported_claims: list[str]
    missing_evidence: list[str]
    reasoning_summary: str
    recommendation: str
    provider_id: str
    model_id: str
    prompt_version: str
    error_message: str | None
    created_at: datetime
