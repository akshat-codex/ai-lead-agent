"""Phase 2 — ICP Normalization Engine.

Deterministically transforms a saved Phase 1 ICP draft (app/schemas/icp.py's
HardRules/SoftPreferences, as stored on ICPModel) into the canonical ICP
representation (app/schemas/canonical_icp.py) that Phase 3 onward will
consume.

Deliberately not LLM-based: every transform here is a pure function of its
input, so the same draft always normalizes to the same canonical ICP, and a
review of this file is a complete review of what normalization can do.

What normalization is allowed to do:
  - clean up whitespace and remove exact (case-insensitive) duplicates
  - map well-known geography aliases ("US", "USA", "United States") to a
    single ISO country code + canonical label
What it must never do:
  - expand a value into a broader category (a country must never become a
    region, e.g. "US" + "UK" must never become "US" + "Europe")
  - narrow, drop, or reinterpret a hard rule
  - "fix" ambiguous or contradictory input — that is rejected instead, via
    IcpNormalizationError, reusing the same cross-field rules Phase 1
    enforces at save time (app/services/icp_rules.py).
"""
from __future__ import annotations

import re

from pydantic import ValidationError

from app.schemas.canonical_icp import (
    CanonicalCustomRule,
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.icp import CustomRule, HardRules, SoftPreferences
from app.services.icp_rules import (
    validate_employee_range,
    validate_exclusion_contradictions,
    validate_geography_duplicates,
    validate_not_empty,
)

_WHITESPACE_RE = re.compile(r"\s+")


class IcpNormalizationError(Exception):
    """Raised when a saved ICP cannot be normalized as-is.

    Carries one or more human-readable messages — normalization reports
    problems rather than silently working around them.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# Common aliases for the countries most likely to appear in an ICP, mapped to
# (ISO 3166-1 alpha-2 code, canonical display label). Deliberately not
# exhaustive: anything not listed here is preserved verbatim as
# "unrecognized" rather than guessed at. Region names (e.g. "Europe", "APAC",
# "EMEA", "North America") are intentionally absent — normalization must
# never expand a region into countries or fold a country into a region.
_GEOGRAPHY_ALIASES: dict[str, tuple[str, str]] = {}


def _register_country(code: str, label: str, *aliases: str) -> None:
    for alias in (label, code, *aliases):
        _GEOGRAPHY_ALIASES[alias.strip().lower()] = (code, label)


_register_country("US", "United States", "USA", "U.S.", "U.S.A.", "United States of America")
_register_country("GB", "United Kingdom", "UK", "U.K.", "Great Britain")
_register_country("CA", "Canada")
_register_country("AU", "Australia")
_register_country("NZ", "New Zealand")
_register_country("DE", "Germany", "Deutschland")
_register_country("FR", "France")
_register_country("NL", "Netherlands", "The Netherlands", "Holland")
_register_country("IE", "Ireland")
_register_country("ES", "Spain")
_register_country("IT", "Italy")
_register_country("SE", "Sweden")
_register_country("NO", "Norway")
_register_country("DK", "Denmark")
_register_country("FI", "Finland")
_register_country("CH", "Switzerland")
_register_country("AT", "Austria")
_register_country("BE", "Belgium")
_register_country("PT", "Portugal")
_register_country("PL", "Poland")
_register_country("IN", "India")
_register_country("SG", "Singapore")
_register_country("JP", "Japan")
_register_country("CN", "China")
_register_country("BR", "Brazil")
_register_country("MX", "Mexico")
_register_country("ZA", "South Africa")
_register_country("AE", "United Arab Emirates", "UAE")
_register_country("IL", "Israel")


def clean_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip())


def resolve_geography_alias(value: str) -> tuple[str, str] | None:
    """Looks up a raw geography string against the shared alias table.

    Returns (iso_code, canonical_label) if recognized, else None. Exposed so
    the Phase 3 Hard ICP Rule Engine resolves candidate geography with this
    exact table — never a second, potentially diverging, copy of it.
    """
    cleaned = clean_text(value)
    if not cleaned:
        return None
    return _GEOGRAPHY_ALIASES.get(cleaned.lower())


# P3 fix — a small, hand-reviewed table of unambiguous title-abbreviation
# EXPANSIONS (never a synonym/equivalence guess): each token maps to the
# single, universally-recognized long form it stands for. Deliberately not
# an "equivalent roles" table — "Head of Growth" is NOT folded into
# "Growth Director" here, since that is an organizational judgment call,
# not a pure abbreviation. Every entry below expands to exactly the phrase
# a person holding that exact abbreviated title would also honestly write
# out in full; nothing here ever merges two DIFFERENT real seniority
# levels (e.g. "VP" and "SVP" expand to different phrases and stay
# distinct after expansion).
_TITLE_TOKEN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "ceo": ("chief", "executive", "officer"),
    "cfo": ("chief", "financial", "officer"),
    "cmo": ("chief", "marketing", "officer"),
    "cto": ("chief", "technology", "officer"),
    "coo": ("chief", "operating", "officer"),
    "cpo": ("chief", "product", "officer"),
    "cro": ("chief", "revenue", "officer"),
    "chro": ("chief", "human", "resources", "officer"),
    "ciso": ("chief", "information", "security", "officer"),
    "cio": ("chief", "information", "officer"),
    "evp": ("executive", "vice", "president"),
    "svp": ("senior", "vice", "president"),
    "avp": ("assistant", "vice", "president"),
    "vp": ("vice", "president"),
    "dir": ("director",),
    "mgr": ("manager",),
    "sr": ("senior",),
    "jr": ("junior",),
}

# Connective words stripped when comparing titles — "VP of Marketing" and
# "VP Marketing" name the exact same role; the connective carries no
# seniority or functional information a hard-rule comparison should ever
# treat as distinguishing. Deliberately NOT stripped from
# _evaluate_exclusions/_text_matches or any other comparison in
# app/services/hard_rule_engine.py — scoped to title comparison only.
_TITLE_STOPWORDS = frozenset({"of", "and", "the", "&"})

_TITLE_TOKEN_RE = re.compile(r"[a-z0-9.]+")


def normalize_title_for_comparison(value: str) -> tuple[str, ...]:
    """Reduces a job-title string to a canonical, order-independent token
    set for hard-rule comparison — e.g. "VP Marketing", "Vice President,
    Marketing", and "VP of Marketing" all reduce to the identical frozen
    tuple ("marketing", "president", "vice").

    Deliberately conservative and fully deterministic:
      - Only the fixed _TITLE_TOKEN_EXPANSIONS table ever expands a token,
        and only when it is an EXACT, whole-token match (never a prefix or
        substring) — "vp" expands, "vps" or "vip" do not.
      - Punctuation is treated as a token/word separator (so "Vice
        President, Marketing" splits into the same tokens as "Vice
        President Marketing"), never stripped from inside an alphabetic
        token in a way that could merge two different words.
      - Token ORDER is deliberately discarded (returned as a sorted tuple)
        so "VP Marketing" and "Vice President, Marketing" compare equal
        regardless of phrasing order — titles name a single role, not a
        sentence where word order carries meaning.
      - This is still exact-token matching underneath, never fuzzy or
        semantic: a title with an extra distinguishing word (e.g. "VP
        Marketing Operations" vs "VP Marketing") normalizes to a
        DIFFERENT token set and does not match — this function only
        removes the ARBITRARY differences (abbreviation, punctuation,
        connective words, order), never a genuine content difference.

    Exposed so app/services/hard_rule_engine.py's allowed_titles
    comparison uses this exact function — never a second, potentially
    diverging title-normalization implementation."""
    cleaned = clean_text(value).lower()
    if not cleaned:
        return ()
    raw_tokens = _TITLE_TOKEN_RE.findall(cleaned)
    expanded: list[str] = []
    for raw_token in raw_tokens:
        # A trailing period ("vp." "sr.") is a common abbreviation marker,
        # never a meaningful character of the word itself — stripped only
        # for the TABLE LOOKUP, so "vp." and "vp" resolve identically;
        # the raw token (with its period, if any) is what's appended when
        # no expansion applies, so an unrecognized token is never silently
        # mutated beyond period-stripping.
        lookup_token = raw_token.rstrip(".")
        expansion = _TITLE_TOKEN_EXPANSIONS.get(lookup_token)
        if expansion is not None:
            expanded.extend(expansion)
        elif lookup_token not in _TITLE_STOPWORDS:
            expanded.append(lookup_token)
    return tuple(sorted(expanded))


def _clean_string_list(values: list[str]) -> tuple[str, ...]:
    """Trims/collapses whitespace and drops case-insensitive duplicates.

    Preserves input order and the casing of the first occurrence — this is
    pure cleanup, not reinterpretation: the set of distinct values is
    unchanged.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def _normalize_geography(values: list[str]) -> CanonicalGeography:
    countries: list[GeographyEntry] = []
    unrecognized: list[str] = []
    seen_codes: set[str] = set()
    seen_unrecognized: set[str] = set()

    for value in values:
        cleaned = clean_text(value)
        if not cleaned:
            continue

        match = _GEOGRAPHY_ALIASES.get(cleaned.lower())
        if match is None:
            key = cleaned.lower()
            if key in seen_unrecognized:
                continue
            seen_unrecognized.add(key)
            unrecognized.append(cleaned)
            continue

        code, label = match
        if code in seen_codes:
            continue
        seen_codes.add(code)
        countries.append(GeographyEntry(raw=cleaned, code=code, label=label))

    return CanonicalGeography(countries=tuple(countries), unrecognized=tuple(unrecognized))


def _normalize_custom_rules(items: list[CustomRule]) -> tuple[CanonicalCustomRule, ...]:
    result: list[CanonicalCustomRule] = []
    for item in items:
        result.append(
            CanonicalCustomRule(label=clean_text(item.label), description=clean_text(item.description))
        )
    return tuple(result)


def _parse_hard_rules(raw: dict) -> HardRules:
    try:
        return HardRules.model_validate(raw)
    except ValidationError as exc:
        raise IcpNormalizationError([f"hard_rules: {err['msg']}" for err in exc.errors()]) from exc


def _parse_soft_preferences(raw: dict) -> SoftPreferences:
    try:
        return SoftPreferences.model_validate(raw)
    except ValidationError as exc:
        raise IcpNormalizationError([f"soft_preferences: {err['msg']}" for err in exc.errors()]) from exc


def normalize_icp(icp_id: str, version: int, hard_rules_raw: dict, soft_preferences_raw: dict) -> CanonicalICP:
    """Normalizes a saved ICP draft into a canonical ICP.

    Raises IcpNormalizationError if the input is malformed or internally
    contradictory (min > max, duplicate/contradictory geography, exclusions
    that contradict allowed values, an empty ICP, or malformed rule/title
    structures) — normalization reports these rather than silently
    resolving them.
    """
    hard = _parse_hard_rules(hard_rules_raw)
    soft = _parse_soft_preferences(soft_preferences_raw)

    errors: list[str] = []
    for validator, args in (
        (validate_employee_range, (hard.min_employees, hard.max_employees)),
        (validate_not_empty, (hard.is_empty(), soft.is_empty())),
        (validate_geography_duplicates, (hard.geography,)),
        (validate_exclusion_contradictions, (hard,)),
    ):
        try:
            validator(*args)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise IcpNormalizationError(errors)

    canonical_hard_rules = CanonicalHardRules(
        industries=_clean_string_list(hard.industry),
        geography=_normalize_geography(hard.geography),
        employee_range=EmployeeRange(min=hard.min_employees, max=hard.max_employees),
        allowed_titles=_clean_string_list(hard.allowed_titles),
        company_types=_clean_string_list(hard.company_type),
        exclusions=_clean_string_list(hard.exclusions),
        custom_rules=_normalize_custom_rules(hard.custom_rules),
    )

    canonical_soft_preferences = CanonicalSoftPreferences(
        business_models=_clean_string_list(soft.business_model_preferences),
        commercial_signals=_clean_string_list(soft.commercial_signals),
        growth_signals=_clean_string_list(soft.growth_signals),
        marketing_signals=_clean_string_list(soft.marketing_signals),
        custom_preferences=_normalize_custom_rules(soft.other_preferences),
    )

    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=canonical_hard_rules,
        soft_preferences=canonical_soft_preferences,
    )
