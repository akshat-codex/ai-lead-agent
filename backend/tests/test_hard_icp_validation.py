from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    CanonicalCustomRule,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.hard_rule_result import OverallResult, ReasonCode, RuleStatus
from app.services.hard_icp_validation import validate_against_icp


def _icp(icp_id="icp-1", version=1, soft=None, **hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("Skincare",),
        geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
        employee_range=EmployeeRange(min=10, max=200),
        allowed_titles=("CMO", "Head of Growth"),
        company_types=("D2C",),
        exclusions=("Acme Corp",),
        custom_rules=(),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=CanonicalHardRules(**hard_defaults),
        soft_preferences=soft or CanonicalSoftPreferences(business_models=("Subscription",)),
    )


def _evidence(entity_type: EntityType, entity_id: str, field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        value=value,
        source_provider_id="provider-a",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _corroborated(entity_type: EntityType, entity_id: str, field: str, value) -> list[EvidenceRecord]:
    """Two independent records agreeing on a value — the minimum needed to
    reach Phase 11's SUPPORTED status without a stated confidence."""
    return [
        _evidence(entity_type, entity_id, field, value, source_provider_id="provider-a", external_id="ext-1"),
        _evidence(entity_type, entity_id, field, value, source_provider_id="provider-b", external_id="ext-2"),
    ]


def _trusted_structured(entity_type: EntityType, entity_id: str, field: str, value) -> list[EvidenceRecord]:
    """A single record from Explorium's real discovery provider_id — the
    one case that reaches SUPPORTED_STRUCTURED off a lone, honestly
    UNKNOWN-confidence sighting, per evidence_engine.py's explicit
    trusted-provider allowlist. Never corroborated, never given a stated
    confidence — the allowlist alone is what makes this eligible."""
    return [_evidence(entity_type, entity_id, field, value, source_provider_id="explorium-company-discovery-v1")]


_FULL_COMPANY_EVIDENCE = lambda company_id, **field_overrides: (  # noqa: E731
    _corroborated(EntityType.COMPANY, company_id, "industry", field_overrides.get("industry", "Skincare"))
    + _corroborated(EntityType.COMPANY, company_id, "country", field_overrides.get("country", "US"))
    + _corroborated(EntityType.COMPANY, company_id, "employee_count", field_overrides.get("employee_count", 50))
    + _corroborated(EntityType.COMPANY, company_id, "company_type", field_overrides.get("company_type", "D2C"))
)


# --- PASS with supported evidence -----------------------------------


def test_pass_with_fully_supported_evidence():
    # No person is supplied here, so a configured allowed_titles rule would
    # correctly HOLD (title unknown) — cleared to isolate the company-only
    # fields this test is actually about.
    icp = _icp(allowed_titles=())
    result = validate_against_icp(icp, "company-1", _FULL_COMPANY_EVIDENCE("company-1"), None, [])
    assert result.evaluation.overall_result == OverallResult.PASS


# --- FAIL with confirmed violation ------------------------------------


def test_fail_with_confirmed_employee_violation():
    icp = _icp(employee_range=EmployeeRange(min=10, max=200))
    evidence = _FULL_COMPANY_EVIDENCE("company-1", employee_count=500)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    assert result.evaluation.overall_result == OverallResult.FAIL
    assert ReasonCode.EMPLOYEE_TOO_LARGE in result.evaluation.reason_codes


def test_fail_remains_fail_even_with_other_excellent_fields():
    icp = _icp()
    evidence = _FULL_COMPANY_EVIDENCE("company-1", employee_count=500)  # everything else supported/allowed
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    assert result.evaluation.overall_result == OverallResult.FAIL


# --- HOLD with missing evidence -----------------------------------------


def test_hold_with_no_employee_evidence_at_all():
    icp = _icp()
    evidence = [e for e in _FULL_COMPANY_EVIDENCE("company-1") if e.field != "employee_count"]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    assert result.evaluation.overall_result == OverallResult.HOLD
    assert ReasonCode.EMPLOYEE_COUNT_UNKNOWN in result.evaluation.reason_codes


def test_hold_never_becomes_pass_from_uncertainty():
    icp = _icp()
    result = validate_against_icp(icp, "company-1", [], None, [])  # zero evidence anywhere
    assert result.evaluation.overall_result != OverallResult.PASS
    assert result.evaluation.overall_result == OverallResult.HOLD


# --- HOLD with conflicting evidence ---------------------------------------


def test_hold_with_conflicting_employee_evidence():
    icp = _icp()
    conflicting = [
        _evidence(EntityType.COMPANY, "company-1", "employee_count", 30, source_provider_id="provider-a"),
        _evidence(EntityType.COMPANY, "company-1", "employee_count", 300, source_provider_id="provider-b"),
    ]
    evidence = [e for e in _FULL_COMPANY_EVIDENCE("company-1") if e.field != "employee_count"] + conflicting
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    assert result.evaluation.overall_result == OverallResult.HOLD
    assert ReasonCode.EMPLOYEE_COUNT_UNKNOWN in result.evaluation.reason_codes


# --- FAIL precedence over HOLD -------------------------------------------


def test_fail_takes_precedence_over_simultaneous_hold():
    icp = _icp()
    evidence = _FULL_COMPANY_EVIDENCE("company-1", employee_count=500)  # FAIL
    evidence = [e for e in evidence if e.field != "country"]  # geography missing -> would HOLD
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    assert result.evaluation.overall_result == OverallResult.FAIL
    statuses = {r.rule: r.status.value for r in result.evaluation.rule_results}
    assert statuses["employee_range"] == "FAIL"
    assert statuses["geography"] == "HOLD"


# --- employee_range bucket evidence (Phase 7O) -----------------------------
# employee_range is NOT in evidence_engine.py's _TRUSTED_STRUCTURED_FIELDS
# (only industry/country are), so a single Explorium sighting alone still
# stays INSUFFICIENT — real corroboration (2+ agreeing sources) is required
# here exactly as it already is for employee_count/company_type/domain.
# This intentionally does not change evidence trust policy; it only wires
# an already-corroborated employee_range value into the rule engine.


def test_employee_range_bucket_resolves_the_rule_when_no_exact_count_exists():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = [e for e in _FULL_COMPANY_EVIDENCE("company-1") if e.field != "employee_count"]
    evidence += _corroborated(EntityType.COMPANY, "company-1", "employee_range", "51-200")
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert rule.status.value == "PASS"  # 51-200 overlaps 15-150
    assert result.evidence_ids["employee_range"]  # cites the range evidence, not empty


def test_employee_range_bucket_fail_when_no_overlap():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = [e for e in _FULL_COMPANY_EVIDENCE("company-1") if e.field != "employee_count"]
    evidence += _corroborated(EntityType.COMPANY, "company-1", "employee_range", "201-500")
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    assert result.evaluation.overall_result == OverallResult.FAIL
    assert ReasonCode.EMPLOYEE_TOO_LARGE in result.evaluation.reason_codes


def test_employee_count_still_takes_precedence_over_range_evidence():
    """When BOTH an exact count and a range are corroborated, the exact
    count must continue resolving the rule exactly as before this phase —
    never overridden by the coarser bucket."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = _FULL_COMPANY_EVIDENCE("company-1", employee_count=500)  # exact FAIL
    evidence += _corroborated(EntityType.COMPANY, "company-1", "employee_range", "51-200")  # would PASS alone
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    assert result.evaluation.overall_result == OverallResult.FAIL
    assert ReasonCode.EMPLOYEE_TOO_LARGE in result.evaluation.reason_codes


def test_single_uncorroborated_employee_range_sighting_now_resolves_via_the_bridge():
    """Phase 11 fix: employee_range WAS excluded from
    evidence_engine.py's _TRUSTED_STRUCTURED_FIELDS, so a single,
    uncorroborated Explorium sighting could never reach
    SUPPORTED_STRUCTURED and the rule always HOLD'd on
    EMPLOYEE_COUNT_UNKNOWN regardless of what Explorium actually returned
    — confirmed as one of two proven company-discovery qualification
    blockers by a controlled discovery-quality evaluation. employee_range
    is now in that allowlist (still requiring the same real, named
    trusted-provider id — this is not a general loosening of evidence
    trust, only this one specific field/provider combination), so this
    single sighting now resolves via the SAME bucket-overlap logic
    app/services/hard_rule_engine.py already had — 51-200 overlaps the
    ICP's 15-150 — producing PASS for employee_range specifically. allowed_titles
    is cleared to isolate that from an unrelated TITLE_UNKNOWN HOLD, the
    same isolation test_pass_with_fully_supported_evidence already uses."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150), allowed_titles=())
    evidence = [e for e in _FULL_COMPANY_EVIDENCE("company-1") if e.field != "employee_count"]
    evidence += _trusted_structured(EntityType.COMPANY, "company-1", "employee_range", "51-200")
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    employee_rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert employee_rule.status == RuleStatus.PASS
    assert result.evaluation.overall_result == OverallResult.PASS


def test_untrusted_provider_employee_range_sighting_still_holds():
    """The trust extension is scoped to the named allowlisted provider,
    not to employee_range in general — a single sighting from a provider
    NOT on _TRUSTED_STRUCTURED_PROVIDERS must still HOLD, exactly as
    before this fix. This is the regression guard for "no weakening of
    unrelated evidence trust"."""
    icp = _icp(employee_range=EmployeeRange(min=15, max=150), allowed_titles=())
    evidence = [e for e in _FULL_COMPANY_EVIDENCE("company-1") if e.field != "employee_count"]
    evidence += [_evidence(EntityType.COMPANY, "company-1", "employee_range", "51-200", source_provider_id="some-other-provider")]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    employee_rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert employee_rule.status == RuleStatus.HOLD
    assert ReasonCode.EMPLOYEE_COUNT_UNKNOWN in result.evaluation.reason_codes


# --- soft preferences have zero effect ------------------------------------


def test_soft_preferences_have_zero_effect():
    evidence = _FULL_COMPANY_EVIDENCE("company-1", employee_count=500)  # FAIL regardless

    icp_generous_soft = _icp(soft=CanonicalSoftPreferences(business_models=("Subscription",), growth_signals=("Hiring",)))
    icp_no_soft = _icp(soft=CanonicalSoftPreferences())

    result_with_soft = validate_against_icp(icp_generous_soft, "company-1", evidence, None, [])
    result_without_soft = validate_against_icp(icp_no_soft, "company-1", evidence, None, [])

    assert result_with_soft.evaluation.overall_result == OverallResult.FAIL
    assert result_without_soft.evaluation.overall_result == OverallResult.FAIL
    assert result_with_soft.evaluation.model_dump() == result_without_soft.evaluation.model_dump()


# --- per hard-rule category -----------------------------------------


def test_geography_pass_fail_hold():
    icp = _icp()

    passing = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "country", "US"), None, [])
    assert next(r for r in passing.evaluation.rule_results if r.rule == "geography").status.value == "PASS"

    failing = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "country", "Germany"), None, [])
    assert next(r for r in failing.evaluation.rule_results if r.rule == "geography").status.value == "FAIL"

    holding = validate_against_icp(icp, "c1", [], None, [])
    assert next(r for r in holding.evaluation.rule_results if r.rule == "geography").status.value == "HOLD"


def test_industry_pass_fail_hold():
    icp = _icp()
    passing = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "industry", "Skincare"), None, [])
    assert next(r for r in passing.evaluation.rule_results if r.rule == "industry").status.value == "PASS"

    failing = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "industry", "Fintech"), None, [])
    assert next(r for r in failing.evaluation.rule_results if r.rule == "industry").status.value == "FAIL"

    holding = validate_against_icp(icp, "c1", [], None, [])
    assert next(r for r in holding.evaluation.rule_results if r.rule == "industry").status.value == "HOLD"


def test_trusted_structured_provider_single_sighting_reaches_real_pass_or_fail_on_industry():
    """The exact scenario this fix exists for: Explorium states industry
    directly, once, with an honest UNKNOWN confidence — no corroboration.
    This must resolve to a real PASS or FAIL, never sit at HOLD forever."""
    icp = _icp(industries=("Healthcare", "FMCG", "D2C Brands", "OTT Platforms", "Microdrama Companies"))

    mismatched = validate_against_icp(
        icp, "c1", _trusted_structured(EntityType.COMPANY, "c1", "industry", "Employment Services"), None, []
    )
    industry_rule = next(r for r in mismatched.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status.value == "FAIL"
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_MISMATCH

    matched = validate_against_icp(
        icp, "c1", _trusted_structured(EntityType.COMPANY, "c1", "industry", "Healthcare"), None, []
    )
    assert next(r for r in matched.evaluation.rule_results if r.rule == "industry").status.value == "PASS"


def test_trusted_structured_provider_single_sighting_reaches_real_pass_on_geography():
    icp = _icp()
    result = validate_against_icp(
        icp, "c1", _trusted_structured(EntityType.COMPANY, "c1", "country", "united states"), None, []
    )
    assert next(r for r in result.evaluation.rule_results if r.rule == "geography").status.value == "PASS"


def test_untrusted_single_sighting_on_industry_still_holds_not_pass_or_fail():
    """The same lone-sighting shape, but from a provider not on the
    allowlist — must still HOLD, exactly as before this fix. Confirms the
    allowlist boundary, not just the trusted-path behavior."""
    icp = _icp()
    result = validate_against_icp(
        icp, "c1", [_evidence(EntityType.COMPANY, "c1", "industry", "Skincare", source_provider_id="some-other-provider")], None, []
    )
    assert next(r for r in result.evaluation.rule_results if r.rule == "industry").status.value == "HOLD"


def test_trusted_structured_evidence_id_is_still_attached_to_the_rule_result():
    """SUPPORTED_STRUCTURED must carry real evidence_ids through to the
    result, same as SUPPORTED — this is real evidence, not a bypass."""
    icp = _icp(industries=("Healthcare",))
    evidence = _trusted_structured(EntityType.COMPANY, "c1", "industry", "Healthcare")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    assert result.evidence_ids["industry"] == (evidence[0].id,)


def test_company_type_pass_fail_hold():
    icp = _icp()
    passing = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "company_type", "D2C"), None, [])
    assert next(r for r in passing.evaluation.rule_results if r.rule == "company_type").status.value == "PASS"

    failing = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "company_type", "Agency"), None, [])
    assert next(r for r in failing.evaluation.rule_results if r.rule == "company_type").status.value == "FAIL"

    holding = validate_against_icp(icp, "c1", [], None, [])
    assert next(r for r in holding.evaluation.rule_results if r.rule == "company_type").status.value == "HOLD"


def test_employee_min_and_max():
    icp = _icp(employee_range=EmployeeRange(min=10, max=200))

    too_small = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "employee_count", 3), None, [])
    assert next(r for r in too_small.evaluation.rule_results if r.rule == "employee_range").reason_code == ReasonCode.EMPLOYEE_TOO_SMALL

    too_large = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "employee_count", 500), None, [])
    assert next(r for r in too_large.evaluation.rule_results if r.rule == "employee_range").reason_code == ReasonCode.EMPLOYEE_TOO_LARGE

    within = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "employee_count", 50), None, [])
    assert next(r for r in within.evaluation.rule_results if r.rule == "employee_range").status.value == "PASS"


# --- exclusions -----------------------------------------------------


def test_exclusion_fail_when_evidence_confirms_the_excluded_name():
    icp = _icp(exclusions=("Acme Corp",))
    evidence = _corroborated(EntityType.COMPANY, "c1", "company_identity", "Acme Corp")
    result = validate_against_icp(icp, "c1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "exclusions")
    assert rule.status.value == "FAIL"
    assert rule.reason_code == ReasonCode.EXPLICIT_EXCLUSION


def test_exclusion_holds_when_nothing_is_known_about_the_company():
    icp = _icp(exclusions=("Acme Corp",))
    result = validate_against_icp(icp, "c1", [], None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "exclusions")
    assert rule.status.value == "HOLD"
    assert rule.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE


def test_exclusion_never_fabricates_a_match():
    icp = _icp(exclusions=("Wholesale-only brands",))
    evidence = _FULL_COMPANY_EVIDENCE("c1")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "exclusions")
    assert rule.status.value == "PASS"  # no literal match found among known fields


# --- custom mandatory rules ----------------------------------------------


def test_custom_mandatory_rule_always_holds_without_an_evidence_source():
    icp = _icp(custom_rules=(CanonicalCustomRule(label="Founded after 2015", description="..."),))
    evidence = _FULL_COMPANY_EVIDENCE("c1")  # every other field passes
    result = validate_against_icp(icp, "c1", evidence, None, [])

    custom_rule_result = next(r for r in result.evaluation.rule_results if r.rule.startswith("custom_rule:"))
    assert custom_rule_result.status.value == "HOLD"
    assert custom_rule_result.reason_code == ReasonCode.CUSTOM_RULE_UNRESOLVED
    assert result.evaluation.overall_result == OverallResult.HOLD


# --- person + company association -----------------------------------


def test_person_title_used_only_when_company_association_confirms_this_company():
    icp = _icp()
    person_evidence = _corroborated(EntityType.PERSON, "p1", "current_title", "CMO") + _corroborated(
        EntityType.PERSON, "p1", "company_association", "company-1"
    )
    result = validate_against_icp(icp, "company-1", [], "p1", person_evidence)
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "PASS"


def test_person_title_ignored_when_association_points_elsewhere():
    icp = _icp()
    person_evidence = _corroborated(EntityType.PERSON, "p1", "current_title", "CMO") + _corroborated(
        EntityType.PERSON, "p1", "company_association", "a-different-company"
    )
    result = validate_against_icp(icp, "company-1", [], "p1", person_evidence)
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "HOLD"
    assert title_rule.reason_code == ReasonCode.TITLE_UNKNOWN


def test_single_discovery_sighting_alone_never_verifies_a_title():
    """A lone Phase 9 sighting always carries ConfidenceLevel.UNKNOWN, so
    it can never alone reach Phase 11's SUPPORTED status — this is the
    concrete mechanism behind "do not treat discovery data alone as
    verified current employment/title."""
    icp = _icp()
    person_evidence = [
        _evidence(EntityType.PERSON, "p1", "current_title", "CMO"),
        _evidence(EntityType.PERSON, "p1", "company_association", "company-1"),
    ]
    result = validate_against_icp(icp, "company-1", [], "p1", person_evidence)
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "HOLD"
    assert title_rule.reason_code == ReasonCode.TITLE_UNKNOWN


def test_allowed_title_fail_when_confirmed_title_is_not_allowed():
    icp = _icp(allowed_titles=("CEO",))
    person_evidence = _corroborated(EntityType.PERSON, "p1", "current_title", "Intern") + _corroborated(
        EntityType.PERSON, "p1", "company_association", "company-1"
    )
    result = validate_against_icp(icp, "company-1", [], "p1", person_evidence)
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "FAIL"
    assert title_rule.reason_code == ReasonCode.TITLE_NOT_ALLOWED


def test_no_person_id_makes_title_rule_not_applicable_for_this_evaluation():
    """P1 fix: company-only validation (person_id=None) must not force
    HOLD on allowed_titles merely because no person has been looked for
    yet — that is "nothing to evaluate," not "evidence is missing." The
    ICP's own allowed_titles requirement is untouched (see the next test)
    and is still enforced in full once a real person is evaluated."""
    icp = _icp()
    result = validate_against_icp(icp, "company-1", _FULL_COMPANY_EVIDENCE("company-1"), None, [])
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "NOT_APPLICABLE"
    # A NOT_APPLICABLE rule must never contribute to an overall HOLD.
    assert title_rule not in result.evaluation.unresolved_rules
    assert result.evaluation.overall_result == OverallResult.PASS


def test_title_rule_still_holds_once_a_real_person_with_unknown_title_is_evaluated():
    """The suppression above is strictly scoped to person_id is None — a
    validation call that actually supplies a person must still HOLD on an
    unresolved title exactly as before this fix (unchanged: see
    test_single_discovery_sighting_alone_never_verifies_a_title above)."""
    icp = _icp()
    result = validate_against_icp(icp, "company-1", _FULL_COMPANY_EVIDENCE("company-1"), "p1", [])
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "HOLD"
    assert title_rule.reason_code == ReasonCode.TITLE_UNKNOWN
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_title_suppression_does_not_widen_a_later_person_scoped_evaluation():
    """The per-call suppression must never leak into the ICP object
    itself — a second validate_against_icp call for the SAME icp
    instance, now with a person whose title is confirmed but not
    allowed, must still FAIL exactly as it would with no prior
    company-only call at all."""
    icp = _icp(allowed_titles=("CEO",))
    validate_against_icp(icp, "company-1", _FULL_COMPANY_EVIDENCE("company-1"), None, [])  # company-only pass first
    person_evidence = _corroborated(EntityType.PERSON, "p1", "current_title", "Intern") + _corroborated(
        EntityType.PERSON, "p1", "company_association", "company-1"
    )
    result = validate_against_icp(icp, "company-1", _FULL_COMPANY_EVIDENCE("company-1"), "p1", person_evidence)
    title_rule = next(r for r in result.evaluation.rule_results if r.rule == "allowed_titles")
    assert title_rule.status.value == "FAIL"
    assert title_rule.reason_code == ReasonCode.TITLE_NOT_ALLOWED
    assert icp.hard_rules.allowed_titles == ("CEO",)  # the ICP itself was never mutated


# --- ICP version isolation / same company under different ICPs ---------


def test_same_company_under_different_icps_can_produce_different_results():
    lenient_icp = _icp(icp_id="icp-lenient", version=1, allowed_titles=(), employee_range=EmployeeRange(min=1, max=1000))
    strict_icp = _icp(icp_id="icp-strict", version=1, allowed_titles=(), employee_range=EmployeeRange(min=1, max=100))
    evidence = _FULL_COMPANY_EVIDENCE("company-1", employee_count=500)

    lenient_result = validate_against_icp(lenient_icp, "company-1", evidence, None, [])
    strict_result = validate_against_icp(strict_icp, "company-1", evidence, None, [])

    assert lenient_result.evaluation.overall_result == OverallResult.PASS
    assert strict_result.evaluation.overall_result == OverallResult.FAIL


def test_icp_version_is_preserved_on_the_result():
    icp = _icp(icp_id="icp-1", version=7)
    result = validate_against_icp(icp, "company-1", [], None, [])
    assert result.evaluation.icp_id == "icp-1"
    assert result.evaluation.icp_version == 7


# --- evidence provenance attached to decisions -----------------------


def test_evidence_ids_attached_to_supporting_rules():
    icp = _icp()
    evidence = _corroborated(EntityType.COMPANY, "c1", "industry", "Skincare")
    result = validate_against_icp(icp, "c1", evidence, None, [])

    evidence_ids_for_industry = result.evidence_ids["industry"]
    assert set(evidence_ids_for_industry) == {e.id for e in evidence}


def test_no_evidence_ids_for_an_unresolved_rule():
    icp = _icp()
    result = validate_against_icp(icp, "c1", [], None, [])
    assert result.evidence_ids["industry"] == ()


# --- determinism / no fabrication -----------------------------------------


def test_validation_is_deterministic_for_identical_input():
    icp = _icp()
    evidence = _FULL_COMPANY_EVIDENCE("company-1")
    first = validate_against_icp(icp, "company-1", evidence, None, [])
    second = validate_against_icp(icp, "company-1", evidence, None, [])
    assert first.evaluation == second.evaluation
    assert first.evidence_ids == second.evidence_ids


def test_no_field_is_ever_populated_without_supporting_evidence():
    icp = _icp()
    # Only industry has evidence; every other field must stay unknown, not guessed.
    result = validate_against_icp(icp, "c1", _corroborated(EntityType.COMPANY, "c1", "industry", "Skincare"), None, [])
    statuses = {r.rule: r.status.value for r in result.evaluation.rule_results}
    assert statuses["industry"] == "PASS"
    assert statuses["geography"] == "HOLD"
    assert statuses["employee_range"] == "HOLD"
    assert statuses["company_type"] == "HOLD"
    # person_id=None here — allowed_titles is NOT_APPLICABLE for this
    # company-only call (P1 fix), never a guessed PASS: see
    # test_title_rule_still_holds_once_a_real_person_with_unknown_title_is_evaluated
    # for the unchanged HOLD behavior once a person is actually supplied.
    assert statuses["allowed_titles"] == "NOT_APPLICABLE"
