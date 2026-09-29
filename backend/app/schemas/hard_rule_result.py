"""Result contract for the Phase 3 Hard ICP Rule Engine."""
from enum import Enum

from pydantic import BaseModel, ConfigDict


class RuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    HOLD = "HOLD"
    # The ICP does not constrain this rule at all (e.g. no allowed_titles
    # configured) — it neither passes nor fails anything, and never appears
    # in failed_rules/unresolved_rules.
    NOT_APPLICABLE = "NOT_APPLICABLE"


class OverallResult(str, Enum):
    """The engine's final verdict. Deliberately excludes NOT_APPLICABLE —
    an evaluation always resolves to one of these three, never "no result"."""

    PASS = "PASS"
    FAIL = "FAIL"
    HOLD = "HOLD"


class ReasonCode(str, Enum):
    EMPLOYEE_TOO_SMALL = "EMPLOYEE_TOO_SMALL"
    EMPLOYEE_TOO_LARGE = "EMPLOYEE_TOO_LARGE"
    EMPLOYEE_COUNT_UNKNOWN = "EMPLOYEE_COUNT_UNKNOWN"
    WRONG_GEOGRAPHY = "WRONG_GEOGRAPHY"
    GEOGRAPHY_UNKNOWN = "GEOGRAPHY_UNKNOWN"
    GEOGRAPHY_UNRESOLVED = "GEOGRAPHY_UNRESOLVED"
    INDUSTRY_MISMATCH = "INDUSTRY_MISMATCH"
    INDUSTRY_UNKNOWN = "INDUSTRY_UNKNOWN"
    TITLE_NOT_ALLOWED = "TITLE_NOT_ALLOWED"
    TITLE_UNKNOWN = "TITLE_UNKNOWN"
    COMPANY_TYPE_EXCLUDED = "COMPANY_TYPE_EXCLUDED"
    COMPANY_TYPE_UNKNOWN = "COMPANY_TYPE_UNKNOWN"
    EXPLICIT_EXCLUSION = "EXPLICIT_EXCLUSION"
    CUSTOM_RULE_FAILED = "CUSTOM_RULE_FAILED"
    CUSTOM_RULE_UNRESOLVED = "CUSTOM_RULE_UNRESOLVED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RuleResult(BaseModel):
    """The outcome of evaluating one hard rule against one candidate."""

    model_config = ConfigDict(frozen=True)

    rule: str
    status: RuleStatus
    reason_code: ReasonCode | None = None
    explanation: str


class HardRuleEvaluation(BaseModel):
    """The complete, auditable result of evaluating a candidate against an ICP."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    overall_result: OverallResult
    rule_results: tuple[RuleResult, ...]
    failed_rules: tuple[RuleResult, ...]
    unresolved_rules: tuple[RuleResult, ...]
    reason_codes: tuple[ReasonCode, ...]
    explanation: str
