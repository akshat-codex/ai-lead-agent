import pytest

from app.schemas.candidate import Candidate
from app.schemas.canonical_icp import (
    CanonicalCustomRule,
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.hard_rule_result import OverallResult, ReasonCode, RuleStatus
from app.services.hard_rule_engine import evaluate_hard_rules


def _icp(**hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("Skincare",),
        geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
        employee_range=EmployeeRange(min=10, max=200),
        allowed_titles=("CMO", "Head of Growth"),
        company_types=("D2C",),
        exclusions=("Acme Corp",),
        custom_rules=(CanonicalCustomRule(label="Founded after 2015", description="Company founded after 2015."),),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id="icp-1",
        version=1,
        hard_rules=CanonicalHardRules(**hard_defaults),
        soft_preferences=CanonicalSoftPreferences(
            business_models=("Subscription",),
            commercial_signals=("Recent funding round",),
        ),
    )


def _matching_candidate(**overrides) -> Candidate:
    base = dict(
        company_name="Glow Labs",
        domain="glowlabs.com",
        industry="Skincare",
        geography="United States",
        employee_count=50,
        title="CMO",
        company_type="D2C",
        custom_rule_results={"Founded after 2015": True},
    )
    base.update(overrides)
    return Candidate(**base)


# --- employee count -----------------------------------------------------


def test_employee_minimum_pass():
    result = evaluate_hard_rules(_icp(), _matching_candidate(employee_count=10))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_minimum_fail():
    result = evaluate_hard_rules(_icp(), _matching_candidate(employee_count=5))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_SMALL
    assert result.overall_result == OverallResult.FAIL


def test_employee_maximum_pass():
    result = evaluate_hard_rules(_icp(), _matching_candidate(employee_count=200))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_maximum_fail():
    result = evaluate_hard_rules(_icp(), _matching_candidate(employee_count=500))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_LARGE


def test_employee_count_unknown_holds():
    result = evaluate_hard_rules(_icp(), _matching_candidate(employee_count=None))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.EMPLOYEE_COUNT_UNKNOWN


def test_employee_range_not_applicable_when_icp_has_no_range():
    icp = _icp(employee_range=EmployeeRange(min=None, max=None))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.NOT_APPLICABLE


# --- employee bucket range (Phase 7O) --------------------------------------
# A provider-stated bucket (e.g. Explorium's "51-200") is evaluated by
# OVERLAP with the ICP's [min, max] when no exact employee_count is
# available — never by containment, and never fabricated into a precise
# count. These generalize to any ICP range, not specifically 15-150.


def test_employee_bucket_fully_within_icp_range_passes():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="51-100"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_bucket_overlapping_icp_range_upper_edge_passes():
    """ICP 15-150 + company bucket 51-200: the bucket extends beyond the
    ICP's max, but still genuinely overlaps (51-150 is a real intersection)
    — a company in this bucket COULD qualify, so this must PASS, not FAIL
    or HOLD."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="51-200"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_bucket_overlapping_icp_range_lower_edge_passes():
    """ICP 15-150 + company bucket 11-50: overlaps 15-50 — PASS."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="11-50"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_bucket_entirely_above_icp_range_fails():
    """ICP 15-150 + company bucket 201-500: zero overlap, entirely above
    the ICP's max — every company in this bucket is confirmed too large."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="201-500"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_LARGE


def test_employee_bucket_entirely_below_icp_range_fails():
    icp = _icp(employee_range=EmployeeRange(min=150, max=500))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="1-10"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_SMALL


def test_employee_bucket_exact_boundary_touch_passes():
    """A bucket that touches the ICP's boundary at exactly one point (e.g.
    ICP min=200, bucket 51-200) is a genuine overlap at that single value
    — must PASS, not FAIL."""
    icp = _icp(employee_range=EmployeeRange(min=200, max=500))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="51-200"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_bucket_open_ended_overlapping_passes():
    """An open-ended bucket like "10001+" overlaps any ICP range with no
    stated max (or a max >= 10001)."""
    icp = _icp(employee_range=EmployeeRange(min=5000, max=None))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="10001+"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_employee_bucket_open_ended_non_overlapping_fails():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="10001+"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_LARGE


def test_employee_bucket_missing_holds_never_fails():
    """Neither employee_count nor employee_range is known — HOLD, never a
    guessed FAIL. Missing data must never be treated as a failure."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range=None))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.EMPLOYEE_COUNT_UNKNOWN


def test_employee_bucket_malformed_string_holds_never_fabricates():
    """An unparseable range string (not "<low>-<high>" or "<low>+") must
    HOLD exactly like missing data — never crash, never guess a bound."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    for malformed in ("unknown", "", "51 to 200", "51-", "-200", "abc-def", "200-51"):
        result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range=malformed))
        rule = next(r for r in result.rule_results if r.rule == "employee_range")
        assert rule.status == RuleStatus.HOLD, f"expected HOLD for malformed value {malformed!r}, got {rule.status}"
        assert rule.reason_code == ReasonCode.EMPLOYEE_COUNT_UNKNOWN


def test_employee_count_takes_precedence_over_bucket_when_both_present():
    """An exact integer count, when available, must continue working
    normally and take precedence over a bucket — never overridden by a
    coarser range even if both are somehow present on the same candidate."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    # employee_count=300 is outside the ICP range; employee_range="51-200"
    # would (on its own) PASS by overlap — the exact count must win and
    # correctly FAIL.
    result = evaluate_hard_rules(
        icp, _matching_candidate(employee_count=300, employee_range="51-200")
    )
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_LARGE


def test_employee_count_exact_still_works_normally_range_generic_20_300():
    """A second, different ICP range (20-300, not 15-150) confirms the
    fix generalizes rather than being specific to one example range."""
    icp = _icp(employee_range=EmployeeRange(min=20, max=300))
    result = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="11-50"))
    rule = next(r for r in result.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS  # 20-50 overlap

    result2 = evaluate_hard_rules(icp, _matching_candidate(employee_count=None, employee_range="501-1000"))
    rule2 = next(r for r in result2.rule_results if r.rule == "employee_range")
    assert rule2.status == RuleStatus.FAIL
    assert rule2.reason_code == ReasonCode.EMPLOYEE_TOO_LARGE


# --- geography ------------------------------------------------------------


def test_geography_pass_via_known_country_code():
    result = evaluate_hard_rules(_icp(), _matching_candidate(geography="US"))
    rule = next(r for r in result.rule_results if r.rule == "geography")
    assert rule.status == RuleStatus.PASS


def test_geography_fail_for_confirmed_different_country():
    result = evaluate_hard_rules(_icp(), _matching_candidate(geography="Germany"))
    rule = next(r for r in result.rule_results if r.rule == "geography")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.WRONG_GEOGRAPHY


def test_geography_unknown_holds():
    result = evaluate_hard_rules(_icp(), _matching_candidate(geography=None))
    rule = next(r for r in result.rule_results if r.rule == "geography")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.GEOGRAPHY_UNKNOWN
    assert result.overall_result == OverallResult.HOLD


def test_geography_ambiguous_region_holds_instead_of_guessing():
    icp = _icp(geography=CanonicalGeography(unrecognized=("Nordics",)))
    result = evaluate_hard_rules(icp, _matching_candidate(geography="Sweden"))
    rule = next(r for r in result.rule_results if r.rule == "geography")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.GEOGRAPHY_UNRESOLVED


def test_geography_exact_literal_match_on_unrecognized_term_passes():
    icp = _icp(geography=CanonicalGeography(unrecognized=("Nordics",)))
    result = evaluate_hard_rules(icp, _matching_candidate(geography="Nordics"))
    rule = next(r for r in result.rule_results if r.rule == "geography")
    assert rule.status == RuleStatus.PASS


# --- industry ---------------------------------------------------------


def test_industry_pass():
    result = evaluate_hard_rules(_icp(), _matching_candidate(industry="Skincare"))
    rule = next(r for r in result.rule_results if r.rule == "industry")
    assert rule.status == RuleStatus.PASS


def test_industry_fail():
    result = evaluate_hard_rules(_icp(), _matching_candidate(industry="Fintech"))
    rule = next(r for r in result.rule_results if r.rule == "industry")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.INDUSTRY_MISMATCH


def test_industry_unknown_holds():
    result = evaluate_hard_rules(_icp(), _matching_candidate(industry=None))
    rule = next(r for r in result.rule_results if r.rule == "industry")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.INDUSTRY_UNKNOWN


# --- allowed titles -----------------------------------------------------


def test_title_pass():
    result = evaluate_hard_rules(_icp(), _matching_candidate(title="Head of Growth"))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.PASS


def test_title_fail():
    result = evaluate_hard_rules(_icp(), _matching_candidate(title="Intern"))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.TITLE_NOT_ALLOWED


def test_title_unknown_holds():
    result = evaluate_hard_rules(_icp(), _matching_candidate(title=None))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.TITLE_UNKNOWN


def test_title_pass_on_abbreviation_variant():
    """P3 fix: the ICP's own 'CMO' is satisfied by a candidate discovered
    with the fully-spelled-out equivalent — the exact scenario the audit
    flagged as a missed match under plain exact-string matching."""
    result = evaluate_hard_rules(_icp(), _matching_candidate(title="Chief Marketing Officer"))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.PASS
    assert result.overall_result == OverallResult.PASS


def test_title_pass_on_punctuation_and_word_order_variant():
    icp = _icp(allowed_titles=("Vice President, Marketing",))
    result = evaluate_hard_rules(icp, _matching_candidate(title="VP Marketing"))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.PASS


def test_title_still_fails_for_a_genuinely_different_role():
    """The fix must never turn a real mismatch into a PASS — only the
    ARBITRARY spelling differences are normalized away."""
    icp = _icp(allowed_titles=("VP Marketing",))
    result = evaluate_hard_rules(icp, _matching_candidate(title="VP Sales"))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.TITLE_NOT_ALLOWED


def test_title_still_holds_on_unknown_title_after_fix():
    """Unchanged: missing evidence is never a positive match, regardless
    of the new comparison key."""
    icp = _icp(allowed_titles=("VP Marketing",))
    result = evaluate_hard_rules(icp, _matching_candidate(title=None))
    rule = next(r for r in result.rule_results if r.rule == "allowed_titles")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.TITLE_UNKNOWN


def test_industry_and_company_type_are_unaffected_by_title_normalization():
    """Scoping check: industry/company_type must keep plain exact-match
    semantics — an abbreviation-shaped industry/company_type value must
    NOT benefit from title-style normalization."""
    icp = _icp(industries=("SaaS",), company_types=("D2C",))
    result = evaluate_hard_rules(icp, _matching_candidate(industry="Software as a Service", company_type="Direct to Consumer"))
    industry_rule = next(r for r in result.rule_results if r.rule == "industry")
    company_type_rule = next(r for r in result.rule_results if r.rule == "company_type")
    assert industry_rule.status == RuleStatus.FAIL
    assert company_type_rule.status == RuleStatus.FAIL


# --- company type -------------------------------------------------------


def test_company_type_pass():
    result = evaluate_hard_rules(_icp(), _matching_candidate(company_type="D2C"))
    rule = next(r for r in result.rule_results if r.rule == "company_type")
    assert rule.status == RuleStatus.PASS


def test_company_type_fail():
    result = evaluate_hard_rules(_icp(), _matching_candidate(company_type="Agency"))
    rule = next(r for r in result.rule_results if r.rule == "company_type")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.COMPANY_TYPE_EXCLUDED


def test_company_type_unknown_holds():
    result = evaluate_hard_rules(_icp(), _matching_candidate(company_type=None))
    rule = next(r for r in result.rule_results if r.rule == "company_type")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.COMPANY_TYPE_UNKNOWN


# --- exclusions -----------------------------------------------------------


def test_exclusion_fail_on_literal_company_name_match():
    result = evaluate_hard_rules(_icp(), _matching_candidate(company_name="Acme Corp"))
    rule = next(r for r in result.rule_results if r.rule == "exclusions")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EXPLICIT_EXCLUSION


def test_exclusion_passes_when_no_candidate_field_matches():
    result = evaluate_hard_rules(_icp(), _matching_candidate(company_name="Glow Labs"))
    rule = next(r for r in result.rule_results if r.rule == "exclusions")
    assert rule.status == RuleStatus.PASS


def test_exclusion_holds_with_zero_candidate_evidence():
    icp = _icp()
    empty_candidate = Candidate(employee_count=50, custom_rule_results={"Founded after 2015": True})
    result = evaluate_hard_rules(icp, empty_candidate)
    rule = next(r for r in result.rule_results if r.rule == "exclusions")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE


def test_descriptive_exclusion_with_no_literal_match_passes_by_design():
    """Documents the engine's boundary: free-text exclusions only catch
    literal matches against known candidate fields, not semantic ones."""
    icp = _icp(exclusions=("Wholesale-only brands",))
    result = evaluate_hard_rules(icp, _matching_candidate())
    rule = next(r for r in result.rule_results if r.rule == "exclusions")
    assert rule.status == RuleStatus.PASS


# --- custom mandatory rules ------------------------------------------------


def test_custom_rule_pass():
    result = evaluate_hard_rules(_icp(), _matching_candidate(custom_rule_results={"Founded after 2015": True}))
    rule = next(r for r in result.rule_results if r.rule.startswith("custom_rule:"))
    assert rule.status == RuleStatus.PASS


def test_custom_rule_fail():
    result = evaluate_hard_rules(_icp(), _matching_candidate(custom_rule_results={"Founded after 2015": False}))
    rule = next(r for r in result.rule_results if r.rule.startswith("custom_rule:"))
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.CUSTOM_RULE_FAILED
    assert result.overall_result == OverallResult.FAIL


def test_custom_rule_unresolved_holds():
    result = evaluate_hard_rules(_icp(), _matching_candidate(custom_rule_results={}))
    rule = next(r for r in result.rule_results if r.rule.startswith("custom_rule:"))
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.CUSTOM_RULE_UNRESOLVED


# --- combined scenarios -----------------------------------------------


def test_pass_only_when_every_applicable_rule_passes():
    result = evaluate_hard_rules(_icp(), _matching_candidate())
    assert result.overall_result == OverallResult.PASS
    assert result.failed_rules == ()
    assert result.unresolved_rules == ()
    assert all(r.status in (RuleStatus.PASS, RuleStatus.NOT_APPLICABLE) for r in result.rule_results)


def test_multiple_simultaneous_failures_are_all_reported():
    result = evaluate_hard_rules(
        _icp(), _matching_candidate(employee_count=2, industry="Fintech", title="Intern")
    )
    assert result.overall_result == OverallResult.FAIL
    failed_rules = {r.rule for r in result.failed_rules}
    assert {"employee_range", "industry", "allowed_titles"} <= failed_rules
    assert len(result.failed_rules) >= 3


def test_fail_takes_priority_over_simultaneous_hold():
    result = evaluate_hard_rules(
        _icp(), _matching_candidate(employee_count=2, geography=None)
    )
    assert result.overall_result == OverallResult.FAIL
    assert any(r.rule == "employee_range" and r.status == RuleStatus.FAIL for r in result.rule_results)
    assert any(r.rule == "geography" and r.status == RuleStatus.HOLD for r in result.rule_results)


def test_hold_when_some_rules_unresolved_and_none_failed():
    result = evaluate_hard_rules(_icp(), _matching_candidate(geography=None, title=None))
    assert result.overall_result == OverallResult.HOLD
    assert result.failed_rules == ()
    assert len(result.unresolved_rules) == 2


def test_hard_failure_never_becomes_pass_regardless_of_other_passing_rules():
    """A single confirmed hard-rule failure must dominate, no matter how
    many other rules pass — nothing in this engine can average results."""
    candidate = _matching_candidate(employee_count=1_000_000)  # wildly over max
    result = evaluate_hard_rules(_icp(), candidate)
    assert result.overall_result == OverallResult.FAIL


def test_soft_preferences_do_not_affect_the_result():
    icp_generous_soft = _icp()
    icp_no_soft = CanonicalICP(
        icp_id=icp_generous_soft.icp_id,
        version=icp_generous_soft.version,
        hard_rules=icp_generous_soft.hard_rules,
        soft_preferences=CanonicalSoftPreferences(),
    )

    failing_candidate = _matching_candidate(employee_count=2)
    result_with_soft = evaluate_hard_rules(icp_generous_soft, failing_candidate)
    result_without_soft = evaluate_hard_rules(icp_no_soft, failing_candidate)

    assert result_with_soft.overall_result == OverallResult.FAIL
    assert result_without_soft.overall_result == OverallResult.FAIL
    assert result_with_soft.model_dump(exclude={"icp_id"}) == result_without_soft.model_dump(exclude={"icp_id"})


def test_evaluation_is_deterministic_for_identical_input():
    icp = _icp()
    candidate = _matching_candidate(employee_count=2, geography=None)
    first = evaluate_hard_rules(icp, candidate)
    second = evaluate_hard_rules(icp, candidate)
    assert first == second
    assert first.model_dump() == second.model_dump()


def test_reason_codes_are_aggregated_on_the_evaluation():
    result = evaluate_hard_rules(_icp(), _matching_candidate(employee_count=2, geography=None))
    assert ReasonCode.EMPLOYEE_TOO_SMALL in result.reason_codes
    assert ReasonCode.GEOGRAPHY_UNKNOWN in result.reason_codes


def test_evaluation_preserves_source_icp_id_and_version():
    base = _icp()
    icp = CanonicalICP(
        icp_id="icp-42",
        version=7,
        hard_rules=base.hard_rules,
        soft_preferences=base.soft_preferences,
    )
    result = evaluate_hard_rules(icp, _matching_candidate())
    assert result.icp_id == "icp-42"
    assert result.icp_version == 7
