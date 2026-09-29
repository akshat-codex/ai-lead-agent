"""Phase 16 — LLM Qualification contracts.

The LLM is a reasoning layer over the deterministic pipeline's own output —
never a second source of truth. Every input type here is a *compact* view
built from Phase 11-15 data (evidence, hard-rule validation, business-model
classification, commercial signals, scores); nothing here re-derives any of
that data, and nothing here is sent to the LLM in its raw, full form.

The hard gate is structural, exactly like Phase 15's final_score: a FAIL or
HOLD hard-rule result means the LLM is never even invoked (see
app/services/llm_qualification.py) — QualificationDecision.GOOD_FIT/WEAK_FIT
literally cannot be produced for anything but a PASS-eligible candidate.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.hard_rule_result import OverallResult, ReasonCode


class QualificationDecision(str, Enum):
    GOOD_FIT = "GOOD_FIT"
    WEAK_FIT = "WEAK_FIT"
    NOT_FIT = "NOT_FIT"
    HOLD = "HOLD"
    REJECT = "REJECT"  # only ever assigned by the backend for a hard FAIL — never an LLM output
    # GOOD_FIT is also assigned by the backend (never an LLM output) for
    # STRUCTURED_MATCH_TRUSTED — see that status's own comment. This is
    # the same "backend-assigned decision, not an LLM judgment" pattern
    # REJECT already established for HARD_REJECTED.


class QualificationExecutionStatus(str, Enum):
    """How the qualification attempt actually went — kept separate from
    `decision` so a provider failure is never confused with a real HOLD
    judgment call; both are honest, but only one reflects the LLM actually
    reasoning about the evidence."""

    SUCCESS = "SUCCESS"
    HARD_REJECTED = "HARD_REJECTED"  # hard ICP FAIL - LLM never called
    HARD_HOLD = "HARD_HOLD"  # hard ICP HOLD - LLM never called
    # Phase 12: hard ICP PASS, but the candidate's industry evidence came
    # from a real, live-verified structured Explorium taxonomy match (see
    # app/providers/explorium.py's industry_match_branch tag and
    # app/services/hard_icp_validation.py's Phase 11 bridge) — trusted
    # without the extra semantic-verification LLM call, LLM never called.
    # Never assigned for a keyword-fallback match, which always proceeds
    # to a real provider call exactly as before this phase.
    STRUCTURED_MATCH_TRUSTED = "STRUCTURED_MATCH_TRUSTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    INVALID_EVIDENCE_IDS = "INVALID_EVIDENCE_IDS"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"


# --- compact LLM input -----------------------------------------------------


class EvidenceBrief(BaseModel):
    """One evidence record, reduced to only what the LLM needs to reason
    and cite — never the full EvidenceRecord (source URLs, raw provider
    ids, etc. are omitted to keep the prompt small)."""

    model_config = ConfigDict(frozen=True)

    id: str
    field: str
    value: str
    confidence: str


class RuleResultBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    status: str
    reason_code: str | None = None


class QualificationContext(BaseModel):
    """The complete, compact bundle sent to an LLMProvider. Built by
    app/services/qualification_context.py from real Phase 2/11-15 data —
    this schema itself never touches a database or a provider.

    Deliberately excludes: raw manager-feedback decisions (Phase 4's 566
    leads), full evidence dumps, and anything not already summarized/scored
    by an earlier phase. Including the ICP's own version number is what
    keeps qualification honestly ICP-version-scoped rather than a global
    "is this a good company" judgment.
    """

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    icp_industries: tuple[str, ...]
    icp_geography: tuple[str, ...]
    icp_employee_range: str
    icp_allowed_titles: tuple[str, ...]
    icp_company_types: tuple[str, ...]
    icp_exclusions: tuple[str, ...]
    icp_soft_preferences: tuple[str, ...]

    company_id: str
    person_id: str | None

    hard_rule_result: OverallResult
    rule_results: tuple[RuleResultBrief, ...]
    reason_codes: tuple[ReasonCode, ...]

    # Phase 12: "structured" when the company's industry evidence carries
    # a Phase 11 industry_match_branch provenance tag (a real, live-
    # verified linkedin_category/naics_category exact match — see
    # app/services/qualification_context.py::_discovery_match_type),
    # "keyword_fallback" when industry evidence exists but carries no such
    # tag, "unknown" when there is no industry evidence to judge from at
    # all. Drives app/services/llm_qualification.py's new selective-
    # verification gate AND is surfaced to the LLM itself (per this
    # phase's own requirement to give it "discovery/provenance
    # information") so a keyword_fallback verification call can reason
    # about why it was flagged, not just that it was.
    discovery_match_type: str = "unknown"

    # Phase 13B: the EXACT website_keywords term(s) Explorium's keyword
    # branch was searching with when this candidate was returned — see
    # app/providers/explorium.py's keyword_match_terms tag and
    # app/services/qualification_context.py::_discovery_keyword_terms.
    # Empty for every discovery_match_type other than "keyword_fallback".
    # Never used to gate anything (Phase 12's gate logic is unchanged) —
    # this exists purely so the LLM verification prompt can reason about
    # WHICH specific term(s) this candidate was actually found by, instead
    # of only knowing that it was a keyword-fallback match at all.
    discovery_keyword_terms: tuple[str, ...] = ()

    # Phase 13D: positionally aligned with discovery_keyword_terms — for
    # each term, whether it came from an unmatched ICP industry term or a
    # company_type term (see app/providers/explorium.py's
    # keyword_match_term_sources comment). A company_type match ("D2C")
    # and an industry match ("Entertainment") make genuinely different
    # claims about a candidate, and this lets the LLM verification prompt
    # tell them apart instead of treating every keyword-fallback term as
    # an undifferentiated industry mention. Empty whenever the source
    # breakdown was never supplied (every pre-Phase-13D evidence record).
    discovery_keyword_term_sources: tuple[str, ...] = ()

    # Phase 15 (Phase 14 audit finding): observability ONLY — see
    # app/services/qualification_context.py::_discovery_structured_match_scope's
    # own docstring for exactly why this is NOT a "broad vs strong"
    # classification and does not drive Phase 12's gate condition
    # (app/services/llm_qualification.py, unchanged by this phase).
    # "single_category" when the structured match came from exactly one
    # resolved taxonomy value (Explorium's total_results, in
    # discovery_structured_match_total_results below, is then safely
    # attributable to it); "multi_category" when more than one resolved
    # value fed the same OR'd branch request (the confirmed common case in
    # real ICPs — total_results is NOT attributable to any single category
    # in that case, so it is never surfaced); "unknown" for every
    # keyword-fallback candidate and every pre-Phase-15 context.
    discovery_structured_match_scope: str = "unknown"
    # Only ever populated when discovery_structured_match_scope ==
    # "single_category" — the real Explorium total_results count for that
    # one category's branch request (category + the ICP's own geography/
    # employee-size filters together). None otherwise.
    discovery_structured_match_total_results: int | None = None

    business_model_summary: str | None
    commercial_signal_summary: tuple[str, ...]

    icp_score: float
    commercial_score: float
    evidence_score: float
    freshness_score: float | None
    identity_confidence: float
    final_score: float | None

    evidence: tuple[EvidenceBrief, ...]
    conflicting_fields: tuple[str, ...]
    missing_critical_fields: tuple[str, ...]

    @model_validator(mode="after")
    def _evidence_ids_are_unique(self) -> "QualificationContext":
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("QualificationContext.evidence must not contain duplicate evidence ids")
        return self

    def known_evidence_ids(self) -> frozenset[str]:
        return frozenset(e.id for e in self.evidence)


# --- strict LLM output -------------------------------------------------


class RawQualificationOutput(BaseModel):
    """The exact JSON shape an LLMProvider must return. Strict: unknown
    fields are rejected rather than silently ignored, since a provider
    inventing an extra field is itself a signal something is wrong."""

    model_config = ConfigDict(extra="forbid")

    decision: QualificationDecision
    confidence: float = Field(ge=0, le=100)
    reason_codes: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=2000)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    risk_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    commercial_fit_explanation: str = Field(default="", max_length=2000)
    hard_rule_acknowledgement: str = Field(default="", max_length=1000)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_cannot_be_reject(self) -> "RawQualificationOutput":
        # REJECT is a backend-only outcome for a hard FAIL; an LLM claiming
        # REJECT would be reasoning about a case it should never see.
        if self.decision == QualificationDecision.REJECT:
            raise ValueError("an LLM output must never assign REJECT")
        return self


class LLMQualificationResult(BaseModel):
    """The full, in-memory, audited result of one qualification attempt.
    Always produced, even on failure — status distinguishes a genuine LLM
    judgment from a provider/validation failure, so failures are preserved
    for audit rather than silently discarded (see the Phase 16 task's
    FAILURE HANDLING section)."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None

    hard_rule_result: OverallResult
    status: QualificationExecutionStatus
    decision: QualificationDecision | None
    confidence: float | None
    reason_codes: tuple[str, ...]
    summary: str
    supporting_evidence_ids: tuple[str, ...]
    risk_evidence_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    commercial_fit_explanation: str
    hard_rule_acknowledgement: str
    uncertainties: tuple[str, ...]

    provider_id: str
    model_id: str
    prompt_version: str

    score_snapshot: dict[str, float | None]
    error_message: str | None = None


class LeadQualificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    person_id: str | None = None


class LeadQualificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    hard_rule_result: str
    status: str
    decision: str | None
    confidence: float | None
    reason_codes: list[str]
    summary: str
    supporting_evidence_ids: list[str]
    risk_evidence_ids: list[str]
    missing_evidence: list[str]
    commercial_fit_explanation: str
    hard_rule_acknowledgement: str
    uncertainties: list[str]
    provider_id: str
    model_id: str
    prompt_version: str
    score_snapshot: dict
    error_message: str | None
    created_at: datetime
