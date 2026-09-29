"""Phase 3 — Hard ICP Rule Engine.

Deterministically evaluates a Candidate against a CanonicalICP's hard rules
and returns PASS / FAIL / HOLD with an auditable, rule-by-rule breakdown.

This is the authoritative hard-ICP gate for every later pipeline stage.
Nothing here is negotiable by score, LLM opinion, or soft preferences:

  * A confirmed hard-rule violation always yields FAIL, and FAIL always wins
    — it can never be overridden into PASS by anything else in this module
    or by a caller re-interpreting the result.
  * A rule that cannot be evaluated because the candidate is missing the
    relevant information yields HOLD, never PASS. Missing evidence is never
    a positive match.
  * Soft preferences are not read anywhere in this module — they cannot
    affect the result even by accident.
  * There is no LLM call and no randomness anywhere in this file: the same
    (icp, candidate) pair always produces the exact same evaluation.

Deliberately independent of discovery/enrichment/LLM/scoring/batch/review —
this module only imports canonical ICP/candidate schemas and the shared
geography/title comparison helpers from the normalization engine (so
geography and title comparison are never resolved by two diverging
implementations). It takes plain in-memory objects in and returns a plain
in-memory object out; no database access.

P3 fix: allowed_titles is compared via
app/services/icp_normalization.py::normalize_title_for_comparison instead
of plain exact-match — a small, fixed table of unambiguous abbreviation
expansions (VP/Vice President, CMO/Chief Marketing Officer, etc.) plus
punctuation/word-order normalization, applied identically to both sides
of the comparison. Still deterministic, still no fuzzy/semantic matching,
still gated the same PASS/HOLD/FAIL way: a genuinely unmatched title still
FAILs, and an unknown title still HOLDs — only which STRINGS are
considered the "same" key changed, never the PASS/HOLD/FAIL logic itself.
Every other rule (industry, company_type, exclusions) is completely
unaffected and keeps the original plain exact-match key.
"""
from __future__ import annotations

from app.schemas.candidate import Candidate
from app.schemas.canonical_icp import CanonicalICP, CanonicalHardRules
from app.schemas.hard_rule_result import HardRuleEvaluation, OverallResult, ReasonCode, RuleResult, RuleStatus
from app.services.icp_normalization import clean_text, normalize_title_for_comparison, resolve_geography_alias

# Candidate fields checked against free-text exclusion terms. Exclusions in
# the canonical ICP are user-authored free text (e.g. "Wholesale-only
# brands", "Acme Corp", "acme.com") — this engine can only confidently catch
# exclusions that literally match a known candidate attribute. A descriptive
# exclusion with no literal match in these fields is not a false PASS; it is
# the documented boundary of a deterministic, evidence-only engine (richer
# matching belongs to the future Evidence Engine / LLM qualification phases).
_EXCLUSION_CHECK_FIELDS = (
    "company_name",
    "domain",
    "industry",
    "geography",
    "company_type",
    "title",
)


def _text_matches(term: str, value: str) -> bool:
    term_clean = clean_text(term).lower()
    value_clean = clean_text(value).lower()
    if not term_clean or not value_clean:
        return False
    return term_clean == value_clean or term_clean in value_clean or value_clean in term_clean


def _parse_employee_range_bucket(value: str) -> tuple[int, int | None] | None:
    """Parses a provider-stated employee bucket string into (low, high),
    high=None meaning open-ended (e.g. "10001+"). Generic across any
    provider's bucket vocabulary — not hardcoded to Explorium's specific 8
    buckets, just the two shapes any such bucket string can honestly take:
    "<low>-<high>" or "<low>+". Returns None for anything that doesn't
    match either shape — an unparseable string must HOLD, never crash or
    guess a bound."""
    text = value.strip()
    if text.endswith("+"):
        low_text = text[:-1].strip()
        if not low_text.isdigit():
            return None
        return int(low_text), None
    parts = text.split("-", 1)
    if len(parts) != 2:
        return None
    low_text, high_text = (p.strip() for p in parts)
    if not low_text.isdigit() or not high_text.isdigit():
        return None
    low, high = int(low_text), int(high_text)
    if low > high:
        return None
    return low, high


def _evaluate_employee_range(icp: CanonicalHardRules, candidate: Candidate) -> RuleResult:
    employee_range = icp.employee_range
    if employee_range.min is None and employee_range.max is None:
        return RuleResult(rule="employee_range", status=RuleStatus.NOT_APPLICABLE, explanation="No employee range configured.")

    if candidate.employee_count is not None:
        count = candidate.employee_count
        if employee_range.min is not None and count < employee_range.min:
            return RuleResult(
                rule="employee_range",
                status=RuleStatus.FAIL,
                reason_code=ReasonCode.EMPLOYEE_TOO_SMALL,
                explanation=f"Candidate has {count} employees, below the required minimum of {employee_range.min}.",
            )
        if employee_range.max is not None and count > employee_range.max:
            return RuleResult(
                rule="employee_range",
                status=RuleStatus.FAIL,
                reason_code=ReasonCode.EMPLOYEE_TOO_LARGE,
                explanation=f"Candidate has {count} employees, above the allowed maximum of {employee_range.max}.",
            )
        return RuleResult(
            rule="employee_range",
            status=RuleStatus.PASS,
            explanation=f"Candidate employee count {count} is within {employee_range.min}-{employee_range.max}.",
        )

    # No exact count — fall back to a provider-stated bucket, if any.
    # Phase 7O: a bucket is inherently a RANGE, not a single number, so the
    # only honest comparison is OVERLAP with the ICP's [min, max], not
    # containment — the same discipline already applied on the discovery
    # side (see explorium.py's _build_employee_size_filter). A bucket that
    # overlaps the ICP's range means a real company in that bucket COULD
    # qualify, so this must not FAIL a company merely because its bucket's
    # edges extend beyond the ICP's exact bounds; conversely a bucket with
    # zero overlap means every company in that bucket is confirmed outside
    # the ICP's range, which is a genuine, evidence-based FAIL. This never
    # produces a fabricated exact count — PASS/FAIL are based purely on
    # whether the ranges overlap.
    if candidate.employee_range is not None:
        parsed = _parse_employee_range_bucket(candidate.employee_range)
        if parsed is not None:
            bucket_low, bucket_high = parsed
            icp_low = employee_range.min if employee_range.min is not None else 0
            icp_high = employee_range.max
            overlaps = (bucket_high is None or bucket_high >= icp_low) and (
                icp_high is None or bucket_low <= icp_high
            )
            if overlaps:
                return RuleResult(
                    rule="employee_range",
                    status=RuleStatus.PASS,
                    explanation=(
                        f"Candidate employee range '{candidate.employee_range}' overlaps the required "
                        f"{employee_range.min}-{employee_range.max}."
                    ),
                )
            reason_code = (
                ReasonCode.EMPLOYEE_TOO_SMALL
                if bucket_high is not None and icp_low is not None and bucket_high < icp_low
                else ReasonCode.EMPLOYEE_TOO_LARGE
            )
            return RuleResult(
                rule="employee_range",
                status=RuleStatus.FAIL,
                reason_code=reason_code,
                explanation=(
                    f"Candidate employee range '{candidate.employee_range}' does not overlap the required "
                    f"{employee_range.min}-{employee_range.max}."
                ),
            )
        # An employee_range string that doesn't parse into a recognizable
        # bucket shape is treated exactly like no range at all — HOLD,
        # never a guess.

    return RuleResult(
        rule="employee_range",
        status=RuleStatus.HOLD,
        reason_code=ReasonCode.EMPLOYEE_COUNT_UNKNOWN,
        explanation="Candidate employee count/range is unknown; cannot evaluate the required range.",
    )


def _evaluate_geography(icp: CanonicalHardRules, candidate: Candidate) -> RuleResult:
    geography = icp.geography
    if not geography.countries and not geography.unrecognized:
        return RuleResult(rule="geography", status=RuleStatus.NOT_APPLICABLE, explanation="No geography constraint configured.")

    if candidate.geography is None:
        return RuleResult(
            rule="geography",
            status=RuleStatus.HOLD,
            reason_code=ReasonCode.GEOGRAPHY_UNKNOWN,
            explanation="Candidate geography is unknown; cannot evaluate the required geography.",
        )

    candidate_clean = clean_text(candidate.geography)
    candidate_resolved = resolve_geography_alias(candidate.geography)
    candidate_code = candidate_resolved[0] if candidate_resolved else None

    allowed_codes = {entry.code for entry in geography.countries}
    allowed_raw = {value.lower() for value in geography.unrecognized}

    if candidate_code is not None and candidate_code in allowed_codes:
        return RuleResult(
            rule="geography",
            status=RuleStatus.PASS,
            explanation=f"Candidate geography '{candidate.geography}' resolves to {candidate_code}, an allowed country.",
        )
    if candidate_clean.lower() in allowed_raw:
        return RuleResult(
            rule="geography",
            status=RuleStatus.PASS,
            explanation=f"Candidate geography '{candidate.geography}' matches an allowed geography term.",
        )
    if candidate_code is not None and not allowed_raw:
        # Candidate resolves to a known country, and the ICP's constraint is
        # fully expressed in recognized countries with nothing ambiguous
        # left over — a confident, evidence-based failure.
        return RuleResult(
            rule="geography",
            status=RuleStatus.FAIL,
            reason_code=ReasonCode.WRONG_GEOGRAPHY,
            explanation=f"Candidate geography '{candidate.geography}' ({candidate_code}) is not in the allowed countries.",
        )
    # Either the candidate's location or the ICP's requirement (or both)
    # includes a term this engine cannot confidently resolve (e.g. a region
    # like "Nordics"). Guessing which countries that covers would risk
    # exactly the kind of broadening/narrowing this engine must never do, so
    # the honest answer is HOLD, not a guessed FAIL or PASS.
    return RuleResult(
        rule="geography",
        status=RuleStatus.HOLD,
        reason_code=ReasonCode.GEOGRAPHY_UNRESOLVED,
        explanation=f"Cannot confidently compare candidate geography '{candidate.geography}' against the ICP's geography requirement.",
    )


def _plain_key(value: str) -> object:
    return clean_text(value).lower()


def _evaluate_choice_field(
    rule: str,
    allowed_values: tuple[str, ...],
    candidate_value: str | None,
    unknown_reason: ReasonCode,
    mismatch_reason: ReasonCode,
    label: str,
    comparison_key=_plain_key,
) -> RuleResult:
    """comparison_key reduces a raw string to whatever key equality is
    actually checked against — defaults to plain clean+lowercase (exact
    match, unchanged for industry/company_type). P3 fix: allowed_titles
    passes normalize_title_for_comparison instead, so "VP Marketing" and
    "Vice President, Marketing" compare equal — still exact-key matching
    underneath (see that function's own docstring for exactly what it
    does and does not do), never fuzzy/semantic matching, and this
    parameter changes NOTHING about industry/company_type, which keep
    using the original plain-lowercase key."""
    if not allowed_values:
        return RuleResult(rule=rule, status=RuleStatus.NOT_APPLICABLE, explanation=f"No {label} constraint configured.")

    if candidate_value is None:
        return RuleResult(
            rule=rule,
            status=RuleStatus.HOLD,
            reason_code=unknown_reason,
            explanation=f"Candidate {label} is unknown; cannot evaluate the required {label}.",
        )

    candidate_key = comparison_key(candidate_value)
    if any(comparison_key(v) == candidate_key for v in allowed_values):
        return RuleResult(rule=rule, status=RuleStatus.PASS, explanation=f"Candidate {label} '{candidate_value}' is allowed.")

    return RuleResult(
        rule=rule,
        status=RuleStatus.FAIL,
        reason_code=mismatch_reason,
        explanation=f"Candidate {label} '{candidate_value}' is not among the allowed values: {', '.join(allowed_values)}.",
    )


def _evaluate_exclusions(icp: CanonicalHardRules, candidate: Candidate) -> RuleResult:
    if not icp.exclusions:
        return RuleResult(rule="exclusions", status=RuleStatus.NOT_APPLICABLE, explanation="No exclusions configured.")

    candidate_values = {
        field: getattr(candidate, field)
        for field in _EXCLUSION_CHECK_FIELDS
        if getattr(candidate, field) is not None
    }
    if not candidate_values:
        return RuleResult(
            rule="exclusions",
            status=RuleStatus.HOLD,
            reason_code=ReasonCode.INSUFFICIENT_EVIDENCE,
            explanation="No candidate attributes are known yet; cannot confirm the exclusion list does not apply.",
        )

    for exclusion in icp.exclusions:
        for field, value in candidate_values.items():
            if _text_matches(exclusion, value):
                return RuleResult(
                    rule="exclusions",
                    status=RuleStatus.FAIL,
                    reason_code=ReasonCode.EXPLICIT_EXCLUSION,
                    explanation=f"Candidate {field} '{value}' matches explicit exclusion '{exclusion}'.",
                )

    return RuleResult(
        rule="exclusions",
        status=RuleStatus.PASS,
        explanation="No known candidate attribute matches an explicit exclusion.",
    )


def _evaluate_custom_rules(icp: CanonicalHardRules, candidate: Candidate) -> list[RuleResult]:
    results: list[RuleResult] = []
    for custom_rule in icp.custom_rules:
        rule_key = f"custom_rule:{custom_rule.label}"
        outcome = candidate.custom_rule_results.get(custom_rule.label)
        if outcome is None:
            results.append(
                RuleResult(
                    rule=rule_key,
                    status=RuleStatus.HOLD,
                    reason_code=ReasonCode.CUSTOM_RULE_UNRESOLVED,
                    explanation=f"No evidence yet on whether the candidate satisfies custom rule '{custom_rule.label}' ({custom_rule.description}).",
                )
            )
        elif outcome is False:
            results.append(
                RuleResult(
                    rule=rule_key,
                    status=RuleStatus.FAIL,
                    reason_code=ReasonCode.CUSTOM_RULE_FAILED,
                    explanation=f"Candidate does not satisfy custom rule '{custom_rule.label}' ({custom_rule.description}).",
                )
            )
        else:
            results.append(
                RuleResult(
                    rule=rule_key,
                    status=RuleStatus.PASS,
                    explanation=f"Candidate satisfies custom rule '{custom_rule.label}'.",
                )
            )
    return results


def evaluate_hard_rules(icp: CanonicalICP, candidate: Candidate) -> HardRuleEvaluation:
    """Evaluates one candidate against one canonical ICP's hard rules.

    Pure and deterministic: calling this twice with equal inputs returns
    equal results. Only app.schemas.canonical_icp.CanonicalICP's hard_rules
    are read — soft_preferences are never inspected, by construction.
    """
    hard = icp.hard_rules

    rule_results: list[RuleResult] = [
        _evaluate_employee_range(hard, candidate),
        _evaluate_geography(hard, candidate),
        _evaluate_choice_field(
            rule="industry",
            allowed_values=hard.industries,
            candidate_value=candidate.industry,
            unknown_reason=ReasonCode.INDUSTRY_UNKNOWN,
            mismatch_reason=ReasonCode.INDUSTRY_MISMATCH,
            label="industry",
        ),
        _evaluate_choice_field(
            rule="allowed_titles",
            allowed_values=hard.allowed_titles,
            candidate_value=candidate.title,
            unknown_reason=ReasonCode.TITLE_UNKNOWN,
            mismatch_reason=ReasonCode.TITLE_NOT_ALLOWED,
            label="title",
            # P3 fix: "VP Marketing" must match an ICP's "Vice President,
            # Marketing" — see normalize_title_for_comparison's own
            # docstring for exactly what this does and does not do
            # (unambiguous abbreviation expansion + order/punctuation
            # normalization, never fuzzy/semantic matching). Scoped to
            # this ONE field — industry/company_type keep the original
            # plain exact-match key, unaffected.
            comparison_key=normalize_title_for_comparison,
        ),
        _evaluate_choice_field(
            rule="company_type",
            allowed_values=hard.company_types,
            candidate_value=candidate.company_type,
            unknown_reason=ReasonCode.COMPANY_TYPE_UNKNOWN,
            mismatch_reason=ReasonCode.COMPANY_TYPE_EXCLUDED,
            label="company type",
        ),
        _evaluate_exclusions(hard, candidate),
    ]
    rule_results.extend(_evaluate_custom_rules(hard, candidate))

    failed = tuple(r for r in rule_results if r.status == RuleStatus.FAIL)
    unresolved = tuple(r for r in rule_results if r.status == RuleStatus.HOLD)

    if failed:
        overall = OverallResult.FAIL
    elif unresolved:
        overall = OverallResult.HOLD
    else:
        overall = OverallResult.PASS

    reason_codes = tuple(
        r.reason_code for r in (*failed, *unresolved) if r.reason_code is not None
    )

    if overall == OverallResult.PASS:
        explanation = "All applicable hard rules passed."
    else:
        parts = [f"{r.rule}: {r.status.value} ({r.explanation})" for r in (*failed, *unresolved)]
        explanation = "; ".join(parts)

    return HardRuleEvaluation(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        overall_result=overall,
        rule_results=tuple(rule_results),
        failed_rules=failed,
        unresolved_rules=unresolved,
        reason_codes=reason_codes,
        explanation=explanation,
    )
