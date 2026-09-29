"""Canonical ICP representation (Phase 2).

This is the stable, normalized shape all future backend components (starting
with the Phase 3 Hard ICP Rule Engine) consume. It is deliberately a
different schema from the Phase 1 draft (app/schemas/icp.py) — see
app/services/icp_normalization.py for the deterministic transform between
them.
"""
from pydantic import BaseModel, ConfigDict


class EmployeeRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    min: int | None = None
    max: int | None = None


class GeographyEntry(BaseModel):
    """A geography value the normalizer recognized as a specific country."""

    model_config = ConfigDict(frozen=True)

    raw: str
    code: str
    label: str


class CanonicalGeography(BaseModel):
    """Recognized countries and unrecognized raw values, kept separate.

    Unrecognized values (regions like "Europe", typos, or anything not in
    the alias table) are preserved verbatim rather than guessed at — a
    region is never expanded into its member countries, and a country is
    never generalized into a region.
    """

    model_config = ConfigDict(frozen=True)

    countries: tuple[GeographyEntry, ...] = ()
    unrecognized: tuple[str, ...] = ()


class CanonicalCustomRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    description: str


class CanonicalHardRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    industries: tuple[str, ...] = ()
    geography: CanonicalGeography = CanonicalGeography()
    employee_range: EmployeeRange = EmployeeRange()
    allowed_titles: tuple[str, ...] = ()
    company_types: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    custom_rules: tuple[CanonicalCustomRule, ...] = ()
    # Phase 39 — the subset of `industries` (always a subset, never a
    # separate universe of terms) that app/services/discovery_strategy.py's
    # AI term-expansion proposed as a COMBINATION phrase naming a compound
    # ICP's intersection directly (e.g. "Healthcare Software" for a
    # ["Healthcare", "SaaS"] ICP) rather than an independent synonym of one
    # term. Populated ONLY by merge_strategy_into_hard_rules — every other
    # caller leaves this empty, exactly the same "additive, opt-in,
    # invisible to every existing caller" discipline this codebase already
    # uses for term_origin (see discovery_strategy.py's own
    # build_term_origin_map). Read by app/services/hard_icp_validation.py
    # to exclude these terms from the "how many independent industry
    # concepts must this candidate prove" count — see that module's
    # _effective_required_industry_terms docstring for exactly why a
    # literal AI-proposed combination phrase must not be held to a bar
    # raised by the very terms it already names the intersection of.
    industry_combination_terms: tuple[str, ...] = ()


class CanonicalSoftPreferences(BaseModel):
    model_config = ConfigDict(frozen=True)

    business_models: tuple[str, ...] = ()
    commercial_signals: tuple[str, ...] = ()
    growth_signals: tuple[str, ...] = ()
    marketing_signals: tuple[str, ...] = ()
    custom_preferences: tuple[CanonicalCustomRule, ...] = ()


class CanonicalICP(BaseModel):
    """The canonical ICP, tied back to its source Phase 1 draft and version."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    version: int
    hard_rules: CanonicalHardRules
    soft_preferences: CanonicalSoftPreferences
