"""Deep discovery mode — LLM relevance pre-screen contracts.

Mirrors app/schemas/discovery_strategy.py's own anti-fabrication discipline
exactly: strict (`extra="forbid"`) raw-output validation, an enum-constrained
verdict field, and a hard vocabulary separation from every OTHER decision
type already in this codebase.

`verdict` uses RELEVANT/NOT_RELEVANT — deliberately NOT PASS/FAIL/HOLD
(app/schemas/hard_rule_result.py) and NOT GOOD_FIT/WEAK_FIT/NOT_FIT/HOLD
(app/schemas/llm_qualification.py). This is a discovery-stage relevance
filter only: it can never be mistaken, by variable name or by vocabulary,
for a hard-rule result or a qualification decision. See
app/services/deep_prescreen.py's own module docstring for the full
pipeline-placement rationale and app/services/batch_orchestration.py::
_advance_company_pipeline for the one call site that consumes this.

No evidence-citation-id mechanism exists here (unlike
RawQualificationOutput) because this schema never makes an evidentiary
claim about a specific fact — it only classifies fit against content
already supplied to it, exactly like RawDiscoveryStrategyOutput.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContentSource = Literal["homepage", "search_snippet", "none"]
PrescreenVerdict = Literal["RELEVANT", "NOT_RELEVANT"]


class DeepPrescreenContext(BaseModel):
    """The compact context sent to the LLM for one candidate. `icp_summary`
    is a deterministic, backend-built restatement of the WHOLE canonical
    ICP (industries, company_types, employee range, geography,
    allowed_titles, exclusions) — never raw free-text — built by
    app/services/deep_prescreen.py::build_icp_summary from the same
    already-AI-merged CanonicalICP fields
    app/services/discovery_strategy.py::build_discovery_strategy_request
    already selects, so the pre-screen judges candidates against the exact
    same expanded ICP terms discovery itself searched for, never a second,
    diverging interpretation."""

    model_config = ConfigDict(frozen=True)

    icp_summary: str
    candidate_name: str
    candidate_homepage_text: str
    candidate_homepage_url: str | None = None
    content_source: ContentSource


class RawDeepPrescreenOutput(BaseModel):
    """The exact JSON shape an LLMProvider must return for a pre-screen
    request. Strict: unknown fields are rejected rather than silently
    ignored, mirroring RawDiscoveryStrategyOutput /
    RawQualificationOutput."""

    model_config = ConfigDict(extra="forbid")

    verdict: PrescreenVerdict
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=100)


class DeepPrescreenResult(BaseModel):
    """The validated, ready-to-use result of one candidate's pre-screen.

    `verdict` is None whenever no LLM judgment was ever made (no content
    to judge, or the provider/parse step failed) — status/error_message
    make that an honest, auditable outcome, mirroring DiscoveryStrategy's
    own failure-is-always-recorded discipline. A None verdict is always
    treated by the caller as "let the candidate through" (see
    app/services/batch_orchestration.py::_deep_prescreen_company) — this
    schema does not encode that policy itself, it only reports what
    happened."""

    model_config = ConfigDict(frozen=True)

    status: str  # "SUCCESS" | "SKIPPED_NO_CONTENT" | "PROVIDER_UNAVAILABLE" | "MALFORMED_OUTPUT" | "SCHEMA_INVALID"
    verdict: PrescreenVerdict | None = None
    reason: str | None = None
    confidence: float | None = None
    content_source: ContentSource = "none"
    homepage_fetched: bool = False
    provider_id: str | None = None
    model_id: str | None = None
    error_message: str | None = None
