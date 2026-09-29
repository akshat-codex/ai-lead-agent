"""Phase 30 — company-quality scoring contracts.

Sits AFTER hard-rule validation + Phase 15 scoring + selective Phase 12
verification, and BEFORE Unipile/person discovery (see
app/services/company_quality.py for the scoring logic and
app/services/batch_orchestration.py for where this now gates person
discovery). This is a company-only judgment — it never reads or requires
a person_id, and nothing here re-derives a hard rule, a score component,
or a verification outcome; every signal is a direct, explainable read of
data Phase 11-15/18 already computed.

Deliberately NOT a second qualification system: CompanyQualityLabel is
coarser and purely deterministic (no LLM call of its own), used only to
decide whether a company is worth spending a person-discovery call on. The
existing LLM qualification (Phase 16) and adversarial review (Phase 17)
are completely unchanged and still run afterward, on whichever companies
pass this gate.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompanyQualityLabel(str, Enum):
    """The three-way outcome of the quality gate.

    STRONG / REVIEW are both eligible for person discovery — the gate is
    "not hard-rejected AND not so evidence-poor that a person search would
    likely be wasted," not a narrow pass/fail on top of the hard rules.
    REJECT is assigned ONLY for a hard-rule FAIL, mirroring
    QualificationDecision.REJECT's own "backend-assigned, never invented"
    contract in app/schemas/llm_qualification.py — nothing here can
    downgrade a HOLD or PASS company to REJECT on soft signals alone.
    """

    STRONG = "STRONG"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class CompanyQualitySignal(BaseModel):
    """One named, independently-inspectable contribution to the overall
    quality score. `value` is always in [0, 100] and `weight` is the share
    of the final weighted average this signal contributed (weights actually
    used are re-normalized over whichever signals are available for THIS
    company — see app/services/company_quality.py::score_company_quality
    — so weight here reflects what was actually applied, not a fixed
    static configuration value). `explanation` cites the exact underlying
    fact (a rule status, an evidence field, a discovery-provenance tag)
    the value was read from — never a vague restatement of the label."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    explanation: str


class CompanyQualityResult(BaseModel):
    """The full, in-memory result of one company-quality scoring pass.
    Always produced for a hard PASS or HOLD company (REJECT candidates
    still get a result, just with score=None and label=REJECT — see
    score_company_quality's own docstring for why a FAIL is never scored
    a number, exactly mirroring Phase 15's final_score contract)."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    company_id: str

    hard_rule_result: str  # OverallResult.value — PASS/HOLD/FAIL, read verbatim, never re-evaluated
    label: CompanyQualityLabel
    score: float | None = Field(default=None, ge=0, le=100)  # None only when hard_rule_result == FAIL

    signals: tuple[CompanyQualitySignal, ...]
    explanation: str

    @model_validator(mode="after")
    def _reject_only_for_hard_fail_and_has_no_score(self) -> "CompanyQualityResult":
        if self.label == CompanyQualityLabel.REJECT and self.hard_rule_result != "FAIL":
            raise ValueError("CompanyQualityLabel.REJECT may only be assigned for a hard-rule FAIL")
        if self.hard_rule_result == "FAIL" and self.score is not None:
            raise ValueError("a hard-rule FAIL company must never receive a numeric quality score")
        if self.hard_rule_result != "FAIL" and self.score is None:
            raise ValueError("a non-FAIL company must always receive a numeric quality score")
        return self


class CompanyQualityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)


class CompanyQualityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str
    hard_rule_result: str
    label: str
    score: float | None
    signals: list[dict]
    explanation: str
    created_at: datetime
