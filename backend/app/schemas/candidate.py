"""Candidate input for the Phase 3 Hard ICP Rule Engine.

This is deliberately minimal — only the fields needed to evaluate the hard
rules currently in the canonical ICP (industry, geography, employee count,
title, company type, exclusions, custom rules). Company/person discovery,
enrichment, and evidence infrastructure (Phases 6-11) will eventually
populate a much richer record; this schema is not that record; it is the
narrow slice the rule engine needs, and should not be pre-expanded with
fields no current rule uses.

Any field left as None means "unknown" — the engine treats that as missing
evidence, never as a match.
"""
from pydantic import BaseModel, ConfigDict, Field


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_name: str | None = None
    domain: str | None = None
    industry: str | None = None
    geography: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    # Phase 7O: a provider-stated employee BUCKET (e.g. "51-200", "10001+"),
    # distinct from employee_count — never fabricated into or from a precise
    # count. Only consulted by the employee_range hard rule when
    # employee_count is unavailable; see hard_rule_engine.py.
    employee_range: str | None = None
    title: str | None = None
    company_type: str | None = None

    # Whether each canonical custom rule (keyed by its label) is satisfied,
    # as determined by whatever evidence process supplied this candidate.
    # A missing key means "not yet evaluated" — the engine cannot infer an
    # answer to a free-text custom rule on its own (that would mean guessing
    # or requiring an LLM, both out of scope for this deterministic engine).
    custom_rule_results: dict[str, bool] = Field(default_factory=dict)
