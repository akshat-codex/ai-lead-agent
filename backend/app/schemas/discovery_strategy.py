"""Phase 10 — OpenAI ICP Interpreter contracts.

An LLM (via the existing Phase 9 LLMProvider/OpenAIProvider) is a
*term-expansion* layer over the user's own ICP — never a second source of
truth for what an Explorium taxonomy value is. Nothing here ever produces
a `linkedin_category`/`naics_category` value directly: DiscoveryStrategy
only ever carries candidate STRINGS in the exact same shape
CompanyDiscoveryQuery.industries/company_types already accept today
(app/schemas/candidate_company.py). The live Explorium autocomplete call
(app/providers/explorium.py::_lookup_category) remains the sole authority
on whether any given term — user-typed or AI-proposed — actually resolves
to a real taxonomy label; this module cannot bypass or shortcut that.

This mirrors Phase 16's own discipline (see app/schemas/llm_qualification.py):
strict, `extra="forbid"` raw-output validation, and a hard structural
distinction between "the LLM proposed this" (candidate, unverified) and
"the system accepted this as a fact" (only ever decided downstream, by
Explorium's own live lookup or the existing hard-rule engine — neither of
which this module touches).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DiscoveryStrategyRequest(BaseModel):
    """The compact context sent to the LLM for one ICP submission. Built
    once per ICP (never per candidate/company) by
    app/services/discovery_strategy.py from the ICP's own already-typed
    hard-rule fields — there is no separate "raw text" ICP ingestion point
    anywhere in this codebase today (app/schemas/icp.py's ICPCreate is
    already structured), so "raw ICP" here means the ICP's own hard-rule
    strings exactly as submitted, before any AI expansion — not a new
    freeform-text feature."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    industries: tuple[str, ...] = ()
    geography: tuple[str, ...] = ()
    employee_range: str = "not constrained"
    company_types: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    soft_preferences: tuple[str, ...] = ()


class RawDiscoveryStrategyOutput(BaseModel):
    """The exact JSON shape an LLMProvider must return for a discovery
    strategy request. Strict: unknown fields are rejected rather than
    silently ignored, mirroring RawQualificationOutput
    (app/schemas/llm_qualification.py)."""

    model_config = ConfigDict(extra="forbid")

    industry_terms: list[str] = Field(default_factory=list)
    # Phase 39: industry terms that name a COMPOUND ICP's intersection
    # directly as one phrase (e.g. "Healthcare Software" for
    # ["Healthcare", "SaaS"]) — kept structurally separate from
    # `industry_terms` (independent synonyms of ONE term) so a downstream
    # consumer can tell "this phrase already covers the combination" apart
    # from "this is just another word for one side of it" without any
    # text-shape guessing. See discovery_strategy.py's own
    # _SYSTEM_INSTRUCTIONS for the exact same compound-vs-alternative
    # distinction the prompt already asks the model to reason about.
    combination_industry_terms: list[str] = Field(default_factory=list)
    company_type_terms: list[str] = Field(default_factory=list)
    exclusion_terms: list[str] = Field(default_factory=list)
    geography_notes: list[str] = Field(default_factory=list)
    unsupported_intent: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=100)
    reasoning: str = Field(min_length=1, max_length=1000)


class DiscoveryStrategy(BaseModel):
    """The validated, capped, ready-to-merge result of one ICP's discovery
    strategy interpretation.

    Every term here is a CANDIDATE — worth trying through Explorium's
    existing linkedin_category -> naics_category -> keyword cascade,
    exactly like a user-typed term. None of these fields is ever treated
    as a verified taxonomy value, a hard-rule fact, or evidence: this
    schema carries proposals only. `status`/`error_message` make provider
    failure and degraded-service (fall back to the ICP's own terms,
    unexpanded) an honest, auditable outcome rather than a silent gap —
    mirroring LLMQualificationResult's own failure-is-always-recorded
    discipline (app/schemas/llm_qualification.py).
    """

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    status: str  # "SUCCESS" | "PROVIDER_UNAVAILABLE" | "MALFORMED_OUTPUT" | "SCHEMA_INVALID"
    industry_terms: tuple[str, ...] = ()
    # Phase 39 — see RawDiscoveryStrategyOutput's own field comment. Always
    # a subset conceptually merged alongside industry_terms into
    # CanonicalHardRules.industries (Explorium still searches these
    # exactly like any other industry term); this field exists purely so
    # merge_strategy_into_hard_rules can ALSO record which merged terms
    # were combination phrases, via CanonicalHardRules.industry_combination_terms.
    combination_industry_terms: tuple[str, ...] = ()
    company_type_terms: tuple[str, ...] = ()
    exclusion_terms: tuple[str, ...] = ()
    geography_notes: tuple[str, ...] = ()
    unsupported_intent: tuple[str, ...] = ()
    confidence: float | None = None
    reasoning: str = ""
    provider_id: str | None = None
    model_id: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _terms_present_only_on_success(self) -> "DiscoveryStrategy":
        if self.status != "SUCCESS" and (
            self.industry_terms or self.combination_industry_terms or self.company_type_terms or self.exclusion_terms
        ):
            raise ValueError("a non-SUCCESS DiscoveryStrategy must not carry any proposed terms")
        return self
