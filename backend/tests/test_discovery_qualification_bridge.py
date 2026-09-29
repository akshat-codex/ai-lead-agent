"""Phase 11 — regression tests for the two proven company-discovery
qualification blockers identified by the controlled discovery-quality
evaluation (Phase 10 continuation):

  1. EMPLOYEE RANGE TRUST — a single, genuine Explorium employee_range
     sighting can now participate in hard-rule evaluation (bucket-overlap
     PASS/FAIL), where before it always HOLD'd regardless of what
     Explorium returned. Fix: app/services/evidence_engine.py's
     _TRUSTED_STRUCTURED_FIELDS now includes "employee_range".

  2. INDUSTRY SEARCH -> TAXONOMY BRIDGE — a discovery search term that
     resolves through a real linkedin_category/naics_category exact match
     to a differently-worded taxonomy label no longer FAILs the industry
     hard rule merely for not string-matching the search term that found
     it. Fix: app/providers/explorium.py tags structured/naics records with
     provenance (never keyword-fallback records), app/services/
     evidence_import.py carries that provenance into the industry evidence
     record's existing evidence_text field, and app/services/
     hard_icp_validation.py::_bridged_industry_terms uses it to widen the
     allowed industries set for ONE candidate's evaluation only — never
     touching app/services/hard_rule_engine.py, which remains
     exact-match-only and fully deterministic.

This file exercises the REAL, end-to-end chain wherever practical:
ExploriumCompanyDiscoveryProvider (mocked HTTP via respx) -> the same
evidence field-mapping evidence_import.py uses -> validate_against_icp.
Unit-level tests for evidence_engine.py's own trust-gate change live in
tests/test_evidence_engine.py and tests/test_hard_icp_validation.py; this
file is scoped to the qualification-blocker regression checklist itself.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.hard_rule_result import OverallResult, ReasonCode, RuleStatus
from app.services.evidence_import import INDUSTRY_MATCH_PROVENANCE_KEY, KEYWORD_MATCH_PROVENANCE_KEY, _industry_match_provenance
from app.services.hard_icp_validation import validate_against_icp

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _icp(icp_id="icp-1", **hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("Healthcare",),
        geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
        employee_range=EmployeeRange(min=50, max=1000),
        allowed_titles=(),
        company_types=(),
        exclusions=(),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id=icp_id, version=1, hard_rules=CanonicalHardRules(**hard_defaults), soft_preferences=CanonicalSoftPreferences()
    )


def _evidence(entity_id: str, field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=EntityType.COMPANY,
        entity_id=entity_id,
        field=field,
        value=value,
        source_provider_id="explorium-company-discovery-v1",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _company_evidence_from_attributes(company_id: str, attributes: dict) -> list[EvidenceRecord]:
    """Mirrors evidence_import.py's own field mapping exactly (including
    the Phase 11 evidence_text provenance carry-through), so these tests
    exercise the real production mapping rather than a hand-rolled one."""
    records = [_evidence(company_id, "company_identity", "Some Company")]
    if attributes.get("industry"):
        records.append(_evidence(company_id, "industry", attributes["industry"], evidence_text=_industry_match_provenance(attributes)))
    if attributes.get("country"):
        records.append(_evidence(company_id, "country", attributes["country"]))
    if attributes.get("employee_range"):
        records.append(_evidence(company_id, "employee_range", attributes["employee_range"]))
    if isinstance(attributes.get("employee_count"), int):
        records.append(_evidence(company_id, "employee_count", attributes["employee_count"]))
    return records


def _discover_one(icp: CanonicalICP, autocomplete_mocks: list[dict], search_response: dict, keyword_branch_search_response: dict | None = None):
    """Runs the real ExploriumCompanyDiscoveryProvider against mocked HTTP
    and returns the single resulting NormalizedRecord.

    `keyword_branch_search_response` (Phase 37): OPTIONAL. Without it
    (every pre-Phase-37 caller), every active branch's real, SEPARATE
    POST /v2/businesses call receives the SAME `search_response` — fine
    when a test genuinely doesn't care which branch "found" the single
    company (most callers), but WRONG for a test whose actual intent is
    "this company was found via the structured branch ALONE, the keyword
    branch found nothing/something else" — since Phase 37's own
    cross-branch merge (see app/providers/explorium.py::
    _merge_cross_branch_attributes) now means two branches returning the
    same business_id is treated as genuine corroboration, not silently
    dropped. Pass a distinct response here (e.g. {"data": []}) to keep
    such a test's keyword branch genuinely empty, exactly matching its
    original single-branch intent."""
    with respx.mock:
        for mock in autocomplete_mocks:
            respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": mock["field"], "query": mock["query"]}).mock(
                return_value=httpx.Response(200, json=mock["response"])
            )
        respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
        if keyword_branch_search_response is not None:
            def _search_side_effect(request):
                import json as _json

                body = _json.loads(request.content)
                # "website_keywords" is nested under "filters", never a
                # top-level key — checking the top level (a real bug this
                # helper had) would always be False, silently making the
                # keyword branch reuse `search_response` (the SAME company)
                # regardless of `keyword_branch_search_response`.
                if "website_keywords" in body.get("filters", {}):
                    return httpx.Response(200, json=keyword_branch_search_response)
                return httpx.Response(200, json=search_response)

            respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_search_side_effect)
        else:
            respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json=search_response))

        provider = ExploriumCompanyDiscoveryProvider(api_key="test-key")
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_DISCOVERY,
            query={
                "industries": icp.hard_rules.industries,
                "geography_codes": tuple(e.code for e in icp.hard_rules.geography.countries),
                "min_employees": icp.hard_rules.employee_range.min,
                "max_employees": icp.hard_rules.employee_range.max,
                "limit": 10,
            },
        )
        response = provider.run(request)
    assert response.success is True
    assert len(response.data) == 1
    return response.data[0]


# ============================================================================
# FIX 1 — employee_range trust
# ============================================================================


def test_structured_employee_range_evidence_passes_when_overlapping():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = _company_evidence_from_attributes(
        "company-1", {"industry": "Healthcare", "country": "United States", "employee_range": "51-200"}
    )
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.PASS


def test_structured_employee_range_evidence_fails_when_clearly_outside():
    icp = _icp(employee_range=EmployeeRange(min=15, max=50))
    evidence = _company_evidence_from_attributes(
        "company-1", {"industry": "Healthcare", "country": "United States", "employee_range": "10001+"}
    )
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.FAIL
    assert rule.reason_code == ReasonCode.EMPLOYEE_TOO_LARGE
    assert result.evaluation.overall_result == OverallResult.FAIL


def test_missing_employee_data_still_holds():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = _company_evidence_from_attributes("company-1", {"industry": "Healthcare", "country": "United States"})
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.EMPLOYEE_COUNT_UNKNOWN


def test_unparseable_employee_range_string_still_holds_never_guessed():
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = _company_evidence_from_attributes(
        "company-1", {"industry": "Healthcare", "country": "United States", "employee_range": "not-a-real-bucket"}
    )
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.EMPLOYEE_COUNT_UNKNOWN


def test_exact_employee_count_still_takes_precedence_over_a_conflicting_bucket():
    # employee_count is NOT in _TRUSTED_STRUCTURED_FIELDS (unchanged by
    # this fix — only employee_range was added), so it needs the same
    # two-independent-sources corroboration any other untrusted field
    # does; employee_range gets the single-sighting structured-trust path
    # this fix specifically adds. Both are exercised together here to
    # prove precedence between them is unaffected by either evidence path.
    icp = _icp(employee_range=EmployeeRange(min=15, max=150))
    evidence = _company_evidence_from_attributes(
        "company-1", {"industry": "Healthcare", "country": "United States", "employee_range": "10001+"}
    )
    evidence += [
        _evidence("company-1", "employee_count", 100, source_provider_id="provider-a", external_id="ext-a"),
        _evidence("company-1", "employee_count", 100, source_provider_id="provider-b", external_id="ext-b"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    rule = next(r for r in result.evaluation.rule_results if r.rule == "employee_range")
    # employee_count=100 is within [15,150] -> PASS, even though the
    # (deliberately conflicting) bucket "10001+" does not overlap at all —
    # exact count precedence is unchanged by this fix.
    assert rule.status == RuleStatus.PASS
    assert "100" in rule.explanation


# ============================================================================
# FIX 2 — industry search -> taxonomy bridge
# ============================================================================


def test_structured_linkedin_category_label_differing_from_search_term_holds_not_fails():
    """Corrected (live-test audit finding): ICP term 'Healthcare' resolves
    via linkedin_category to a differently-worded real label
    (naics_description, a DIFFERENT taxonomy with no shared identifier
    space) — the discovered company must not FAIL merely for that textual
    difference (an unverifiable relationship is not proof of mismatch),
    but it must also no longer PASS on it (an unverifiable relationship
    is not proof of match either) — see _bridged_industry_terms's own
    docstring for the full investigation. HOLD is the only honest
    outcome."""
    icp = _icp(industries=("Healthcare",))
    autocomplete_mocks = [
        # Exact label match on the query term itself ("Healthcare") is
        # what actually resolves it into the structured branch — no fuzzy
        # matching, per _lookup_category's own documented, unchanged
        # behavior. The textual mismatch this bridge exists for shows up
        # one step later: Explorium's SEARCH response (naics_description
        # below) states a more specific, differently-worded label for the
        # matched company than the autocomplete term that found it.
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "hospital-health-care"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "hc0000000000000000000000000001",
                "name": "Midwest Regional Health Group",
                "country_name": "United States",
                "number_of_employees_range": "201-500",
                "naics_description": "General Medical and Surgical Hospitals",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry"] == "General Medical and Surgical Hospitals"
    assert record.attributes["industry_match_branch"] == "linkedin_category"
    assert record.attributes["industry_match_terms"] == ["Healthcare"]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_UNKNOWN
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_structured_naics_category_label_differing_from_search_term_also_holds():
    """Generic, not healthcare-specific: the same corrected behavior for
    naics_category matches, not just linkedin_category — the resolved
    NAICS CODE and the returned naics_description TEXT are never directly
    comparable without a code-to-description lookup this codebase does
    not have and must not invent."""
    icp = _icp(industries=("industrial automation",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "industrial automation", "response": []},
        {"field": "naics_category", "query": "industrial automation", "response": [{"query": "industrial automation", "label": "industrial automation", "value": "333999"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "ia0000000000000000000000000001",
                "name": "Precision Robotics Corp",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "All Other Miscellaneous General Purpose Machinery Manufacturing",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry_match_branch"] == "naics_category"

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evaluation.overall_result == OverallResult.HOLD


# ============================================================================
# FIX 3 (Phase 25) — compound ICPs never bridge on a single satisfied term
# ============================================================================
#
# Root cause, traced live from a real "Healthcare SaaS" false positive
# (Optical Goods Stores companies passing on Healthcare-only evidence):
# app/providers/explorium.py's execute() tags a structured-match record's
# `industry_match_terms` with the WHOLE industries tuple the request was
# built from — never scoped to which specific term(s) actually produced
# THIS branch's match. For a single-industry ICP that's harmless (there is
# only one term to begin with). For a compound ICP ("Healthcare", "SaaS"),
# a company discovered via the Healthcare branch alone has ZERO evidence
# about the SaaS half — but _bridged_industry_terms's old overlap check
# ("does the tag mention ANY of my industries?") was trivially satisfied
# by the tag's own full-list shape, so the candidate's own resolved value
# ("Optical Goods Stores") got silently added to the allowed set and the
# industry rule PASSed on Healthcare-only evidence. Fix: the bridge now
# never fires at all when the ICP states more than one industry term —
# see _bridged_industry_terms's own updated docstring (condition 3).


def test_healthcare_saas_optical_goods_stores_no_longer_falsely_passes():
    """The exact reproduced live scenario: a compound 'Healthcare SaaS'
    ICP, a company discovered via the Healthcare linkedin_category branch
    alone (real Explorium industry value 'Optical Goods Stores' — a real,
    live-observed NAICS/LinkedIn sub-category of Healthcare, not a
    fabricated test value), zero independent SaaS evidence. Before the
    Phase 25 fix this PASSed the industry rule; it must now HOLD (unknown,
    since only ONE of the two required industry terms has any evidence at
    all) or FAIL — never PASS."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
        # "SaaS" has no exact structured match anywhere (matches the real,
        # live-observed Phase 24 behavior) — falls to keyword, but this
        # single-candidate response never actually returns a SaaS-branch
        # hit, mirroring the live scenario where the Healthcare branch
        # alone produced this company.
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "og0000000000000000000000000001",
                "name": "Precision Optical Co.",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Optical Goods Stores",
            }
        ]
    }
    # Phase 37: the keyword branch (SaaS) genuinely returns nothing here —
    # without this, _discover_one's shared-mock default would make the
    # SAME company appear to come from the keyword branch too, which is a
    # real, different scenario (genuine cross-branch corroboration) from
    # the one this test is actually about.
    record = _discover_one(icp, autocomplete_mocks, search_response, keyword_branch_search_response={"data": []})
    assert record.attributes["industry"] == "Optical Goods Stores"
    assert record.attributes["industry_match_branch"] == "linkedin_category"
    # Phase 37: the tag now lists ONLY the ICP term(s) that actually,
    # structurally resolved into THIS branch — "Healthcare" alone, never
    # "SaaS" too (which has no exact taxonomy match anywhere and never
    # touched this branch at all). This is a precision improvement over
    # the prior behavior (which listed the full ICP industries tuple
    # regardless of which term actually produced the branch); it does not
    # change this test's own outcome — a candidate with only ONE of two
    # required terms resolved must still never PASS.
    assert record.attributes["industry_match_terms"] == ["Healthcare"]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    # Part 2 of the Phase 25 fix (see _industry_has_unprovable_compound_match's
    # own docstring): a company with genuine but PARTIAL structural
    # evidence (one of the two required terms resolved, one entirely
    # unconfirmed) is neither proven nor disproven — HOLD is the honest
    # outcome, not a confident FAIL (which would claim more certainty than
    # this codebase actually has) and never PASS (which was the original,
    # now-fixed bug).
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert industry_rule.status == RuleStatus.HOLD
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_UNKNOWN
    # And the overall candidate must never reach PASS/ACCEPTED off this
    # alone — confirms it doesn't get fabricated/incorrectly accepted.
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_keyword_matched_candidate_whose_real_industry_literally_equals_an_icp_term_still_honestly_passes_industry_only():
    """Audit finding (pre-Phase-33 investigation, no bug found — this is a
    confirming regression test): a keyword-fallback record's `industry`
    evidence value is ALWAYS Explorium's own real, reported naics_description
    for that company (see app/providers/explorium.py's _BUSINESS_ATTRIBUTE_MAP
    and app/services/evidence_import.py::collect_company_evidence, both of
    which read the provider's actual industry field, never the searched
    keyword term itself) — completely independent of which branch
    (structured or keyword) found the candidate. So a company can
    genuinely, honestly be industry='Healthcare' (an exact ICP term) while
    having been *discovered* via a keyword hit on the ICP's other term
    ('SaaS', which has no exact taxonomy match anywhere for this ICP).

    In that case the industry rule correctly PASSes outright via
    evaluate_hard_rules' own plain, unmodified exact-match check — this is
    not the bridge (_bridged_industry_terms) or the partial-coverage HOLD
    softener (_industry_has_unprovable_compound_match) firing; neither
    function is even reachable here, since the record carries no
    industry_match_* provenance tag at all (see
    test_keyword_fallback_does_not_gain_structured_trust above). This is
    simply the industry rule being honestly true. Crucially, this must
    never be conflated with the "SaaS" half of the ICP being confirmed —
    no rule result or evidence anywhere claims that; the industry rule
    only ever asserts what it always has, that the company's real
    industry matches one of the ICP's stated terms."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": []},
        {"field": "naics_category", "query": "Healthcare", "response": []},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "hs0000000000000000000000000001",
                "name": "HealthTech Software Co",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                # A real, verified Explorium industry value that happens to
                # literally equal one of the ICP's own terms — found via a
                # keyword hit (e.g. "SaaS" appearing in website copy), not a
                # structured taxonomy match on either term.
                "naics_description": "Healthcare",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert "industry_match_branch" not in record.attributes  # genuinely keyword-fallback, not structured
    assert record.attributes["industry"] == "Healthcare"

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.PASS  # honestly true: the real industry IS one of the ICP's own terms


def test_healthcare_only_icp_holds_on_an_unverifiable_structured_match():
    """Live-test audit finding (2026-09-03), corrected after investigation:
    a real live batch run against Explorium with industries=("Healthcare",)
    returned optical/eyewear retailers as GOOD_FIT/ACCEPTED — every one
    structurally, confirmedly resolved via a real, exact
    linkedin_category="Healthcare" autocomplete match, but reported under
    a DIFFERENT taxonomy (naics_description="Optical Goods Stores") with
    no shared identifier space to verify the two correspond. This is now
    proven to generalize to EVERY structured match whose reported value
    doesn't already exact-text-match the ICP term — including this
    test's own "General Medical and Surgical Hospitals" case, which used
    to bridge to PASS purely because the test's author happened to know
    (real-world knowledge no code here ever verified) that pairing was
    true. Both must now HOLD identically — this test no longer
    distinguishes "genuinely healthcare-shaped" from
    "optical-goods-shaped" NAICS text, because nothing in Explorium's
    response lets this codebase draw that line without inventing a
    relationship."""
    icp = _icp(industries=("Healthcare",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "hc0000000000000000000000000002",
                "name": "Sunrise Medical Group",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "General Medical and Surgical Hospitals",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_UNKNOWN
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_saas_only_icp_holds_on_an_unverifiable_naics_structured_match():
    """Requirement 2, corrected: the naics_category branch shares the
    identical unverifiability — its resolved VALUE is a numeric NAICS
    code ("511210"), never the returned naics_description TEXT
    ("Software Publishers"); no code-to-description lookup is captured
    anywhere in this codebase, and building one would mean hardcoding
    NAICS knowledge. Generic (not hardcoded) — same mechanism, a
    different single term and a different taxonomy branch, same honest
    HOLD outcome."""
    icp = _icp(industries=("SaaS",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": [{"query": "SaaS", "label": "SaaS", "value": "511210"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "sa0000000000000000000000000001",
                "name": "CloudStack Software Inc",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Software Publishers",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry_match_branch"] == "naics_category"

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_multi_term_icp_also_holds_when_every_term_resolves_into_the_same_branch():
    """Corrected: even when every stated ICP term genuinely, unambiguously
    contributed to the SAME structured branch request
    (resolved_category_count == len(icp_industries), previously this
    codebase's own bar for trusting a bridge), the candidate's reported
    NAICS description ("Offices of Physicians") still has no verified
    relationship to either literal ICP term ("Healthcare", "Medical
    Practices") — count-based proof-of-participation was never the same
    thing as proof the returned taxonomy label corresponds to the ICP's
    own words. Must HOLD, not PASS, exactly like the single-term case."""
    icp = _icp(industries=("Healthcare", "Medical Practices"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
        {"field": "linkedin_category", "query": "Medical Practices", "response": [{"query": "Medical Practices", "label": "Medical Practices", "value": "medical-practice"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "mp0000000000000000000000000001",
                "name": "Lakeside Family Clinic",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Offices of Physicians",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["industry_match_branch"] == "linkedin_category"
    assert record.attributes["industry_match_resolved_category_count"] == 2

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_compound_icp_industries_are_and_not_or_generic_fintech_saas():
    """Requirement 3 + generalization check: the SAME fix, a DIFFERENT
    compound pair (Fintech SaaS, never Healthcare/SaaS-specific in the
    fix itself) — a company matched via the Fintech branch alone must not
    silently satisfy a Fintech+SaaS requirement."""
    icp = _icp(industries=("Fintech", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Fintech", "response": [{"query": "Fintech", "label": "Fintech", "value": "fintech"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "fn0000000000000000000000000001",
                "name": "Community Trust Credit Union",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Credit Unions",
            }
        ]
    }
    # Phase 37: the keyword branch (SaaS) genuinely returns nothing —
    # this test's intent is a company found via the Fintech branch ALONE.
    record = _discover_one(icp, autocomplete_mocks, search_response, keyword_branch_search_response={"data": []})
    # Phase 37: only "Fintech" resolved into this branch — "SaaS" never
    # did (no exact taxonomy match anywhere for it in this scenario).
    assert record.attributes["industry_match_terms"] == ["Fintech"]
    assert record.attributes["industry_match_resolved_category_count"] == 1

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert industry_rule.status == RuleStatus.HOLD  # partial evidence — neither proven nor disproven
    assert result.evaluation.overall_result != OverallResult.PASS


def test_compound_icp_with_matching_exact_value_still_passes_honestly():
    """The fix must not become an over-correction: if a candidate's OWN
    resolved industry value happens to exact-match one of the compound
    ICP's own literal terms (no bridging needed at all — this is the
    plain, pre-Phase-11 exact-match path evaluate_hard_rules always had),
    it must still PASS. Confirms the fix only removes the BRIDGED
    widening, never the underlying exact-match check itself."""
    icp = _icp(industries=("Fintech", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Fintech", "response": [{"query": "Fintech", "label": "Fintech", "value": "fintech"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "fn0000000000000000000000000002",
                "name": "Fintech Direct Co",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                # Deliberately exact-matches the ICP's own literal term —
                # no bridge involvement needed for this to PASS.
                "naics_description": "Fintech",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.PASS


def test_us_healthcare_saas_employee_range_all_constrain_together():
    """Requirement 4: location + industry + employee range must ALL
    constrain the company. A candidate that PASSes geography and
    employee_range but has only PARTIAL industry evidence must still
    HOLD overall — a satisfied criterion never compensates for an
    unresolved one; every hard rule is independently authoritative."""
    icp = _icp(industries=("Healthcare", "SaaS"), employee_range=EmployeeRange(min=11, max=200))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "us0000000000000000000000000001",
                "name": "MedTech Software Solutions",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Software Publishers",
            }
        ]
    }
    # Phase 37: the keyword branch (SaaS) genuinely returns nothing — this
    # test's intent is a candidate with only PARTIAL industry evidence
    # (Healthcare branch alone).
    record = _discover_one(icp, autocomplete_mocks, search_response, keyword_branch_search_response={"data": []})
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    by_rule = {r.rule: r for r in result.evaluation.rule_results}
    assert by_rule["geography"].status == RuleStatus.PASS
    assert by_rule["employee_range"].status == RuleStatus.PASS
    assert by_rule["industry"].status == RuleStatus.HOLD
    # Geography and employee_range both genuinely passing does NOT rescue
    # the still-unresolved industry criterion — overall must reflect the
    # weakest link, exactly like every other hard-rule combination already
    # does (a single FAIL/HOLD anywhere always dominates PASS elsewhere).
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_wrong_geography_still_fails_even_with_full_industry_match():
    """The inverse of the above: a candidate whose industry criterion is
    UNRESOLVED (live-test audit fix — a structured match no longer
    bridges to an unverifiable NAICS description; see
    _bridged_industry_terms's own docstring) must still FAIL overall if
    geography is wrong — FAIL always wins over an unresolved (HOLD) rule,
    confirming an unresolved-but-not-disproven industry criterion never
    masks or gets masked by a genuinely failed, unrelated criterion.
    Generic, single-term ICP (not the compound case) — this guards the
    baseline AND-across-criteria / FAIL-always-wins behavior itself,
    independent of the industry-bridging fix."""
    icp = _icp(industries=("Healthcare",), geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "ca0000000000000000000000000001",
                "name": "Northern Health Systems",
                "country_name": "Canada",
                "number_of_employees_range": "51-200",
                "naics_description": "General Medical and Surgical Hospitals",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    by_rule = {r.rule: r for r in result.evaluation.rule_results}
    assert by_rule["industry"].status == RuleStatus.HOLD  # unverifiable cross-taxonomy match — never guessed PASS
    assert by_rule["geography"].status == RuleStatus.FAIL
    assert result.evaluation.overall_result == OverallResult.FAIL


def test_broad_keyword_only_candidate_holds_when_second_term_unconfirmed():
    """Requirement 8: a broad keyword-fallback candidate that only ever
    demonstrates ONE half of a compound requirement (no structured
    provenance at all — the weakest discovery tier) must not be presented
    as a strong ICP match. Never PASS, and — since keyword-fallback
    carries no provenance tag for the Phase 25 partial-coverage signal at
    all — this exercises the plain, pre-existing exact-match FAIL path,
    confirming it still correctly rejects a company whose only evidence
    doesn't even literally name either required term."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": []},
        {"field": "naics_category", "query": "Healthcare", "response": []},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "kw0000000000000000000000000001",
                "name": "Generic Wellness Retail",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                # A broad, unrelated NAICS value a pure keyword substring
                # hit on "Healthcare"/"SaaS" website copy could plausibly
                # surface — no structured taxonomy backing at all.
                "naics_description": "All Other General Merchandise Stores",
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert "industry_match_branch" not in record.attributes  # confirms this really is the keyword-fallback tier

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert result.evaluation.overall_result != OverallResult.PASS


def test_keyword_fallback_does_not_gain_structured_trust():
    """A term with no exact taxonomy match anywhere falls to
    website_keywords — that record must NOT be tagged with
    industry_match_branch/industry_match_terms, and a resolved industry
    label differing from the search term must still FAIL, exactly as
    before this phase (the keyword tier's imprecision must not gain a
    free pass)."""
    icp = _icp(industries=("D2C skincare",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "D2C skincare", "response": []},
        {"field": "naics_category", "query": "D2C skincare", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "kw0000000000000000000000000001",
                "name": "Growth Marketing Agency",
                "country_name": "United States",
                "number_of_employees_range": "11-50",
                "naics_description": "Advertising Services",  # a keyword hit, no structured resolution
            }
        ]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert "industry_match_branch" not in record.attributes
    assert "industry_match_terms" not in record.attributes

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.FAIL
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_MISMATCH


def test_ai_expanded_term_provenance_remains_distinguishable_from_user_term():
    """Phase 10's merge_strategy_into_hard_rules already puts user-typed
    and AI-expanded terms into ONE indistinguishable icp.hard_rules.industries
    list, by design (see that module's own docstring). The bridge does not
    need or attempt to distinguish them — it bridges on WHATEVER term is
    actually in the ICP's own current industries list at validation time,
    whether user-typed or AI-expanded. This test proves the bridge fires
    correctly when the matching term is one Phase 10 added, not one the
    user typed directly, confirming the two sources of terms are handled
    identically and neither is silently privileged or excluded."""
    from app.services.discovery_strategy import merge_strategy_into_hard_rules
    from app.schemas.discovery_strategy import DiscoveryStrategy

    icp = _icp(industries=("D2C skincare",))  # the user's own raw term
    ai_strategy = DiscoveryStrategy(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        status="SUCCESS",
        industry_terms=("Cosmetics, Beauty Supplies, and Perfume Stores",),  # AI-proposed, not user-typed
        confidence=80,
        reasoning="expanded",
    )
    merged_hard_rules = merge_strategy_into_hard_rules(icp.hard_rules, ai_strategy)
    merged_icp = icp.model_copy(update={"hard_rules": merged_hard_rules})
    assert merged_icp.hard_rules.industries == ("D2C skincare", "Cosmetics, Beauty Supplies, and Perfume Stores")

    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "D2C skincare", "response": []},
        {"field": "naics_category", "query": "D2C skincare", "response": []},
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {
            "field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores",
            "response": [{"query": "Cosmetics, Beauty Supplies, and Perfume Stores", "label": "Cosmetics, Beauty Supplies, and Perfume Stores", "value": "446120"}],
        },
    ]
    search_response = {
        "data": [
            {
                "business_id": "d2c0000000000000000000000000099",
                "name": "Glow & Co Skincare",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
            }
        ]
    }
    record = _discover_one(merged_icp, autocomplete_mocks, search_response)
    # Phase 37: only the AI-expanded term actually resolved into this
    # branch ("D2C skincare" has no exact taxonomy match on either
    # taxonomy in this scenario, so it never touched this branch at all).
    assert record.attributes["industry_match_terms"] == ["Cosmetics, Beauty Supplies, and Perfume Stores"]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(merged_icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.PASS


def test_bridge_never_fires_on_a_stale_or_unrelated_provenance_tag():
    """A provenance tag whose icp_terms don't overlap the CURRENT ICP's
    industries at all (e.g. the ICP was edited after discovery) must not
    bridge — this is the "never bridge on a stale tag" guard."""
    icp = _icp(industries=("Completely Different Industry",))
    evidence = [
        _evidence(
            "company-1", "industry", "General Medical and Surgical Hospitals",
            evidence_text='{"industry_match": {"taxonomy_field": "linkedin_category", "icp_terms": ["Healthcare"]}}',
        ),
        _evidence("company-1", "country", "United States"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.FAIL


def test_malformed_provenance_json_is_ignored_never_crashes():
    icp = _icp(industries=("Healthcare",))
    evidence = [
        _evidence("company-1", "industry", "General Medical and Surgical Hospitals", evidence_text="not json at all {{{"),
        _evidence("company-1", "country", "United States"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.FAIL  # falls back to the unmodified exact-match behavior, no crash


def test_evidence_text_provenance_key_matches_between_writer_and_reader():
    # Guards against the two modules' hardcoded JSON key drifting apart
    # silently (evidence_import.py writes it, hard_icp_validation.py reads
    # it back — there is no shared schema field enforcing this).
    provenance = _industry_match_provenance({"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]})
    assert provenance is not None
    assert INDUSTRY_MATCH_PROVENANCE_KEY in provenance


# ============================================================================
# Existing behavior unchanged
# ============================================================================


def test_hard_rule_engine_never_touched_industry_still_exact_match_only_without_a_bridge_tag():
    """No provenance tag at all (e.g. evidence from a non-Explorium
    provider, or from before Phase 11) -> industry validation behaves
    exactly as it always has: plain exact-string match, no bridge, no
    fuzzy logic."""
    icp = _icp(industries=("Healthcare",))
    evidence = [
        _evidence("company-1", "industry", "General Medical and Surgical Hospitals"),  # no evidence_text at all
        _evidence("company-1", "country", "United States"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.FAIL
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_MISMATCH


def test_exact_industry_match_still_passes_without_needing_the_bridge():
    icp = _icp(industries=("Healthcare",))
    evidence = [
        _evidence("company-1", "industry", "Healthcare"),  # exact match, no bridge needed
        _evidence("company-1", "country", "United States"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])

    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.PASS


# ============================================================================
# Phase 37 — cross-branch discovery corroboration
#
# Root cause: for a compound ICP like ("Healthcare", "SaaS"), Explorium
# resolves each term independently — "Healthcare" via a real
# linkedin_category match, "SaaS" (no exact taxonomy anywhere) via the
# keyword fallback — as two genuinely SEPARATE branch requests, later
# OR-merged. Before this phase, when Explorium returned the SAME real
# company from BOTH branches within one call, app/providers/explorium.py's
# own cross-branch dedup silently kept only the FIRST branch's record and
# DISCARDED the second branch's provenance entirely — making the
# corroboration invisible to hard_icp_validation.py no matter what
# validation logic existed. Phase 37 fixes the discovery-layer merge to
# combine (never drop) provenance from every branch that found the same
# company, and adds _cross_branch_corroborated_terms to recognize when
# that COMBINED evidence — never a single branch alone — proves the full
# compound ICP.
# ============================================================================


def _discover_one_with_shared_business_id(icp: CanonicalICP, autocomplete_mocks: list[dict], structured_record: dict, keyword_record: dict):
    """Like _discover_one, but the structured AND keyword branches genuinely
    return the SAME real company (same business_id) — the concrete
    "discovered via multiple branches" scenario Phase 37's cross-branch
    merge exists for. `structured_record`/`keyword_record` must share the
    same `business_id`; only the structured branch's record fields (name,
    naics_description, etc.) end up on the merged record, per
    _merge_cross_branch_attributes' own "first-seen scalar wins" rule —
    both dicts are accepted separately only so a caller can assert on
    which branch's response supplied which fields if needed."""
    assert structured_record["business_id"] == keyword_record["business_id"]
    with respx.mock:
        for mock in autocomplete_mocks:
            respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": mock["field"], "query": mock["query"]}).mock(
                return_value=httpx.Response(200, json=mock["response"])
            )
        respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))

        def _search_side_effect(request):
            body = json.loads(request.content)
            if "website_keywords" in body.get("filters", {}):
                return httpx.Response(200, json={"data": [keyword_record]})
            return httpx.Response(200, json={"data": [structured_record]})

        respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_search_side_effect)

        provider = ExploriumCompanyDiscoveryProvider(api_key="test-key")
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_DISCOVERY,
            query={
                "industries": icp.hard_rules.industries,
                "geography_codes": tuple(e.code for e in icp.hard_rules.geography.countries),
                "min_employees": icp.hard_rules.employee_range.min,
                "max_employees": icp.hard_rules.employee_range.max,
                "limit": 10,
            },
        )
        response = provider.run(request)
    assert response.success is True
    assert len(response.data) == 1  # the two branches' hits merged into ONE record, never two
    return response.data[0]


def _healthcare_saas_autocomplete_mocks():
    return [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]


def test_healthcare_only_branch_hit_still_never_passes_healthcare_saas_icp():
    """Regression 1: Healthcare + SaaS ICP, a candidate found via the
    Healthcare branch ONLY (no keyword/SaaS branch hit at all) must not
    PASS — the exact pre-existing partial-match protection, unaffected by
    this phase's corroboration addition."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    search_response = {
        "data": [{
            "business_id": "reg1000000000000000000000001",
            "name": "Regional Hospital Group",
            "country_name": "United States",
            "number_of_employees_range": "51-200",
            "naics_description": "General Medical and Surgical Hospitals",
        }]
    }
    record = _discover_one(icp, _healthcare_saas_autocomplete_mocks(), search_response, keyword_branch_search_response={"data": []})
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert industry_rule.status == RuleStatus.HOLD


def test_saas_only_branch_hit_still_never_passes_healthcare_saas_icp():
    """Regression 2: the inverse — a candidate found via the SaaS keyword
    branch ONLY (no Healthcare structured hit at all) must not PASS
    either."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": []},
        {"field": "naics_category", "query": "Healthcare", "response": []},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [{
            "business_id": "reg2000000000000000000000001",
            "name": "Generic SaaS Tools Inc",
            "country_name": "United States",
            "number_of_employees_range": "51-200",
            "naics_description": "Software Publishers",
        }]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert "industry_match_branch" not in record.attributes  # confirms pure keyword-fallback, no structured branch involved at all
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS


def test_healthcare_and_saas_both_branches_same_company_holds_not_passes():
    """Corrected (live-test audit finding): the SAME real company (same
    business_id) is genuinely returned by BOTH the Healthcare structured
    branch AND the SaaS keyword branch. The structured half no longer
    proves the "Healthcare" term (see _record_covered_icp_terms's own
    docstring — an unverifiable cross-taxonomy resolution), so combined
    coverage across both branches now proves only "SaaS", never the full
    compound ICP — _cross_branch_corroborated_terms correctly withholds
    (not every term covered), and the candidate's real, literal industry
    value doesn't exact-match either ICP term, so
    _industry_has_unprovable_compound_match suppresses it to HOLD, never
    a guessed PASS or FAIL."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    shared = {
        "business_id": "corr0000000000000000000000001",
        "name": "Acme Health SaaS Inc",
        "country_name": "United States",
        "number_of_employees_range": "51-200",
        "naics_description": "General Medical and Surgical Hospitals",
    }
    record = _discover_one_with_shared_business_id(icp, _healthcare_saas_autocomplete_mocks(), structured_record=shared, keyword_record=shared)
    assert record.attributes["industry_match_terms"] == ["Healthcare"]
    assert record.attributes["keyword_match_terms"] == ["SaaS"]

    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_fintech_saas_no_longer_corroborates_on_an_unverifiable_structured_half():
    """Corrected — Regression 5: the SAME mechanism, a DIFFERENT compound
    pair (Fintech + SaaS) — proves nothing in the fix itself is
    Healthcare/SaaS specific. Now HOLDs for the identical structural
    reason as the Healthcare+SaaS case above."""
    icp = _icp(industries=("Fintech", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Fintech", "response": [{"query": "Fintech", "label": "Fintech", "value": "fintech"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    shared = {
        "business_id": "fscorr00000000000000000000001",
        "name": "PayFlow SaaS Inc",
        "country_name": "United States",
        "number_of_employees_range": "51-200",
        "naics_description": "Credit Unions",
    }
    record = _discover_one_with_shared_business_id(icp, autocomplete_mocks, structured_record=shared, keyword_record=shared)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_d2c_fmcg_no_longer_corroborates_on_an_unverifiable_structured_half():
    """Corrected — Regression 6: a third, unrelated compound pair
    (D2C + FMCG), same corrected HOLD outcome."""
    icp = _icp(industries=("D2C", "FMCG"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "D2C", "response": [{"query": "D2C", "label": "D2C", "value": "d2c"}]},
        {"field": "linkedin_category", "query": "FMCG", "response": []},
        {"field": "naics_category", "query": "FMCG", "response": []},
    ]
    shared = {
        "business_id": "dfcorr00000000000000000000001",
        "name": "Snacko FMCG Direct",
        "country_name": "United States",
        "number_of_employees_range": "51-200",
        "naics_description": "Direct Selling Establishments",
    }
    record = _discover_one_with_shared_business_id(icp, autocomplete_mocks, structured_record=shared, keyword_record=shared)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_single_industry_icp_unaffected_by_cross_branch_corroboration():
    """Regression 7: a single-term ICP has no "compound" concept at all —
    _cross_branch_corroborated_terms must be a pure no-op for it
    regardless of this fix (len(icp_industries) <= 1 still short-circuits
    it, unchanged). This candidate's industry outcome is governed
    entirely by _industry_has_unprovable_compound_match instead (same as
    every single-term structured-mismatch case — see
    test_healthcare_only_icp_holds_on_an_unverifiable_structured_match)
    -> HOLD, not the old bridge PASS."""
    icp = _icp(industries=("Healthcare",))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
    ]
    search_response = {
        "data": [{
            "business_id": "single00000000000000000000001",
            "name": "Community Clinic Co",
            "country_name": "United States",
            "number_of_employees_range": "51-200",
            "naics_description": "General Medical and Surgical Hospitals",
        }]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_confirmed_violation_still_fails_even_with_cross_branch_style_evidence():
    """Regression 8: existing FAIL cases remain FAIL — a candidate whose
    industry evidence actively, honestly contradicts the ICP (e.g. wrong
    industry entirely, SUPPORTED_STRUCTURED, no partial-coverage tag at
    all) must still FAIL. Cross-branch corroboration only ever ADDS a
    widened allowed value; it never suppresses or downgrades a genuine
    FAIL the plain exact-match check would otherwise reach on its own —
    this scenario has no industry_match/keyword_match tag at all, so
    neither the bridge nor the corroboration check has anything to work
    with, and the plain check correctly reports FAIL."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence = [
        _evidence("company-1", "industry", "Restaurants"),  # trusted single sighting, no partial-coverage tag at all
        _evidence("company-1", "company_identity", "Totally Unrelated Diner"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.FAIL
    assert result.evaluation.overall_result == OverallResult.FAIL


def test_ambiguous_multi_term_keyword_orlist_never_corroborates_even_from_one_record():
    """Regression 9 (also the exact bug this phase's own implementation
    caught and fixed before landing): existing HOLD cases must not become
    PASS just because a single keyword branch's OR-list happens to
    literally contain every ICP term's text — Explorium provides NO
    per-term match attribution within one OR-list, so a keyword_match tag
    listing 2+ terms is NEVER safe to treat as "this candidate matched
    every one of them." Both ICP terms fail to resolve structurally here,
    so they merge into ONE keyword branch OR-list request — genuinely
    weaker evidence than two separate branch hits, and must still HOLD."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": []},
        {"field": "naics_category", "query": "Healthcare", "response": []},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [{
            "business_id": "ambig0000000000000000000001",
            "name": "Generic Wellness Retail",
            "country_name": "United States",
            "number_of_employees_range": "51-200",
            "naics_description": "All Other General Merchandise Stores",
        }]
    }
    record = _discover_one(icp, autocomplete_mocks, search_response)
    assert record.attributes["keyword_match_terms"] == ["Healthcare", "SaaS"]  # the whole OR-list, not per-term proof
    evidence = _company_evidence_from_attributes("company-1", record.attributes)
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS


def test_hermes_evidence_cannot_exploit_cross_branch_corroboration():
    """Regression 4: Hermes must not bypass trust via this NEW mechanism
    either. Hermes never populates industry_match_branch/keyword_match_terms
    at all (see app/providers/hermes.py — its records carry only a plain
    "industry" attribute string, no Explorium branch provenance of any
    kind), so a Hermes-sourced industry evidence record's evidence_text is
    always None here — _cross_branch_corroborated_terms has nothing to
    read and correctly returns (). Even a Hermes record whose industry
    VALUE happens to literally equal one ICP term does not "corroborate" a
    compound ICP through this function; that is the separate, deliberately
    much stricter _free_text_industry_bridge's job (description-only,
    literal substring of EVERY term), never this one."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    hermes_record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field="industry", value="Healthcare",
        source_provider_id="hermes-icp-search-v1", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        # No evidence_text at all — Hermes never writes industry_match/
        # keyword_match provenance, unlike Explorium.
    )
    identity_record = _evidence("company-1", "company_identity", "HealthTech SaaS Co", source_provider_id="hermes-icp-search-v1")
    result = validate_against_icp(icp, "company-1", [hermes_record, identity_record], None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    # A single Hermes sighting for "industry" never even reaches
    # SUPPORTED/SUPPORTED_STRUCTURED (Hermes is not in
    # _TRUSTED_STRUCTURED_PROVIDERS) — HOLD, never PASS, regardless of
    # cross-branch corroboration logic.
    assert industry_rule.status != RuleStatus.PASS
    assert industry_rule.status == RuleStatus.HOLD


def test_cross_branch_merge_preserves_provenance_from_both_providers():
    """Regression 10: after a cross-branch merge, the SINGLE resulting
    NormalizedRecord still carries BOTH branches' real provenance tags —
    provenance is combined, never lost, and the evidence_text written from
    it round-trips both tags intact."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    shared = {
        "business_id": "prov0000000000000000000000001",
        "name": "Acme Health SaaS Inc",
        "country_name": "United States",
        "number_of_employees_range": "51-200",
        "naics_description": "General Medical and Surgical Hospitals",
    }
    record = _discover_one_with_shared_business_id(icp, _healthcare_saas_autocomplete_mocks(), structured_record=shared, keyword_record=shared)
    # Both branches' provenance survive on the ONE merged record.
    assert record.attributes["industry_match_branch"] == "linkedin_category"
    assert record.attributes["industry_match_terms"] == ["Healthcare"]
    assert record.attributes["keyword_match_terms"] == ["SaaS"]

    ev_text = _industry_match_provenance(record.attributes)
    parsed = json.loads(ev_text)
    assert parsed[INDUSTRY_MATCH_PROVENANCE_KEY]["icp_terms"] == ["Healthcare"]
    assert parsed[KEYWORD_MATCH_PROVENANCE_KEY]["terms"] == ["SaaS"]


# ============================================================================
# PHASE 38 — cross-ROUND + cross-PROVIDER corroboration at the canonical
# company level (Phase 37 above only corroborated hits merged within a
# SINGLE Explorium API call; these regressions cover the same real company
# discovered across SEPARATE rounds/calls/providers, resolved via
# company_resolution.py to one canonical company_id, with each round's own
# EvidenceRecord surviving independently into evidence_import.py's
# per-candidate mapping — see _cross_branch_corroborated_terms's own
# "CROSS-RECORD union, not just per-record" docstring in
# hard_icp_validation.py for the full mechanism).
# ============================================================================


def _round_evidence(company_id: str, industry_value: str, attributes: dict, **overrides) -> EvidenceRecord:
    """One discovery round/candidate's own "industry" EvidenceRecord, built
    via the real evidence_import.py provenance writer — mirrors exactly
    what collect_company_evidence produces for each DiscoveryCandidateModel
    row resolved to the same canonical company_id, across however many
    separate rounds/calls/providers actually found it."""
    return _evidence(company_id, "industry", industry_value, evidence_text=_industry_match_provenance(attributes), **overrides)


def test_healthcare_round1_saas_round2_same_company_now_holds_across_rounds():
    """Corrected (live-test audit finding): Round 1 (separate Explorium
    call) finds the company via the Healthcare structured branch; Round 2
    (a LATER, separate Explorium call/round) finds the SAME real company
    via the SaaS keyword branch. The structured half no longer proves
    "Healthcare" is covered (unverifiable cross-taxonomy resolution — see
    _record_covered_icp_terms's own docstring), so the union across
    rounds now proves only "SaaS" — never the full compound ICP. Must
    HOLD, not PASS."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence = [
        _evidence("company-1", "company_identity", "Acme Health SaaS Inc"),
        _evidence("company-1", "country", "United States"),
        _evidence("company-1", "employee_range", "51-200"),
        _round_evidence(
            "company-1", "General Medical and Surgical Hospitals",
            {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]},
            external_id="round-1-ext",
        ),
        _round_evidence(
            "company-1", "Software Publishers",
            {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
            external_id="round-2-ext",
        ),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_explorium_healthcare_plus_hermes_saas_same_company_now_holds():
    """Corrected: Explorium (structured, Healthcare) + Hermes (untrusted,
    SaaS-labeled industry value) resolve to the SAME canonical company.
    Hermes never writes industry_match/keyword_match provenance, so its
    own record still contributes nothing on its own — but now neither
    does the structured Explorium half (unverifiable cross-taxonomy
    resolution), so nothing here can prove the compound ICP anymore.
    Must HOLD."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence = [
        _evidence("company-1", "company_identity", "Acme Health SaaS Inc"),
        _round_evidence(
            "company-1", "General Medical and Surgical Hospitals",
            {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]},
            external_id="explorium-ext",
        ),
        _round_evidence(
            "company-1", "Software Publishers",
            {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
            external_id="hermes-shadow-keyword-ext",
        ),
        # A genuine Hermes sighting of the same company, no provenance tag
        # at all — present to prove it neither helps nor is required.
        _evidence("company-1", "industry", "Healthcare", source_provider_id="hermes-icp-search-v1", external_id="hermes-ext"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_healthcare_only_across_two_rounds_still_holds():
    """Scenario 3: TWO separate rounds both only ever find Healthcare
    evidence for the same company (e.g. re-discovered on a later page/call)
    — no round, and no union of rounds, ever covers "SaaS". Must still
    HOLD, never PASS, exactly as the single-round case already does."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence = [
        _evidence("company-1", "company_identity", "Acme Health Systems"),
        _round_evidence(
            "company-1", "General Medical and Surgical Hospitals",
            {
                "industry_match_branch": "linkedin_category",
                "industry_match_terms": ["Healthcare"],
                # A genuine single-branch structured hit: only "Healthcare"
                # (1 of the 2 required terms) resolved here — the real
                # shape _industry_has_unprovable_compound_match reads to
                # tell "provably partial" apart from "no proof either way".
                "industry_match_resolved_category_count": 1,
            },
            external_id="round-1-ext",
        ),
        _round_evidence(
            "company-1", "General Medical and Surgical Hospitals",
            {
                "industry_match_branch": "linkedin_category",
                "industry_match_terms": ["Healthcare"],
                "industry_match_resolved_category_count": 1,
            },
            external_id="round-2-ext",
        ),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert industry_rule.status == RuleStatus.HOLD


def test_saas_only_across_two_rounds_still_holds():
    """Scenario 4: the inverse — two rounds, both SaaS-only, never
    Healthcare. Must still HOLD.

    A pure keyword-fallback record carries no industry_match tag at all
    (no taxonomy branch ever resolved), so
    _industry_has_unprovable_compound_match's own "partial structured
    match" softening does not apply here either — this scenario instead
    relies on the plain exact-match FAIL never firing PASS, and on
    _cross_branch_corroborated_terms correctly refusing to treat two
    keyword-only SaaS sightings as proof of Healthcare. Both rounds report
    the exact-match FAIL that a wrong industry value always produces
    (see test_confirmed_violation_across_rounds_still_fails_fail_wins for
    the analogous no-tag-at-all case) — genuinely correct, conservative
    behavior, not a HOLD."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence = [
        _evidence("company-1", "company_identity", "Generic SaaS Tools Inc"),
        _round_evidence(
            "company-1", "Software Publishers",
            {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
            external_id="round-1-ext",
        ),
        _round_evidence(
            "company-1", "Software Publishers",
            {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
            external_id="round-2-ext",
        ),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert industry_rule.status == RuleStatus.FAIL


def test_two_different_companies_never_combine_evidence_across_rounds():
    """Scenario 5: Company A found Healthcare-only, Company B (a genuinely
    DIFFERENT canonical company) found SaaS-only. Evidence is scoped per
    company_id (validate_against_icp is called once per company, with only
    that company's own evidence records), so there is no code path by which
    A's Healthcare evidence could ever combine with B's SaaS evidence —
    both must independently HOLD."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence_a = [
        _evidence("company-a", "company_identity", "Regional Hospital Group"),
        _round_evidence(
            "company-a", "General Medical and Surgical Hospitals",
            {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]},
            external_id="a-ext",
        ),
    ]
    evidence_b = [
        _evidence("company-b", "company_identity", "Generic SaaS Tools Inc"),
        _round_evidence(
            "company-b", "Software Publishers",
            {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
            external_id="b-ext",
        ),
    ]
    result_a = validate_against_icp(icp, "company-a", evidence_a, None, [])
    result_b = validate_against_icp(icp, "company-b", evidence_b, None, [])
    rule_a = next(r for r in result_a.evaluation.rule_results if r.rule == "industry")
    rule_b = next(r for r in result_b.evaluation.rule_results if r.rule == "industry")
    assert rule_a.status != RuleStatus.PASS
    assert rule_b.status != RuleStatus.PASS


def test_same_company_multiple_rounds_no_longer_fabricates_a_cross_round_pass():
    """Corrected — Scenario 6: since the structured round no longer
    proves "Healthcare" is covered, this candidate can no longer reach
    PASS via cross-round corroboration at all. Must HOLD, and — since no
    rule PASSed via cited industry evidence — the industry evidence_ids
    entry must be empty, never citing either round's id as if it had
    proven something it didn't."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    round1 = _round_evidence(
        "company-1", "General Medical and Surgical Hospitals",
        {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]},
        external_id="round-1-ext",
    )
    round2 = _round_evidence(
        "company-1", "Software Publishers",
        {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
        external_id="round-2-ext",
    )
    evidence = [_evidence("company-1", "company_identity", "Acme Health SaaS Inc"), round1, round2]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evidence_ids["industry"] == ()


def test_same_company_explorium_and_hermes_no_longer_fabricates_a_cross_round_pass():
    """Corrected — Scenario 7: same corrected outcome across an Explorium
    round and a (provenance-free) Hermes sighting of the same company —
    HOLD, with no fabricated evidence citation."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    explorium_round1 = _round_evidence(
        "company-1", "General Medical and Surgical Hospitals",
        {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]},
        external_id="explorium-1-ext",
    )
    explorium_round2 = _round_evidence(
        "company-1", "Software Publishers",
        {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
        external_id="explorium-2-ext",
    )
    hermes_record = _evidence("company-1", "industry", "Healthcare", source_provider_id="hermes-icp-search-v1", external_id="hermes-ext")
    evidence = [_evidence("company-1", "company_identity", "Acme Health SaaS Inc"), explorium_round1, explorium_round2, hermes_record]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evidence_ids["industry"] == ()


def test_fintech_saas_cross_round_no_longer_corroborates_generically():
    """Corrected — Scenario 8: the same cross-round mechanism, a different
    compound pair (Fintech + SaaS), proving the fix is not
    Healthcare/SaaS-specific — now HOLDs for the identical reason."""
    icp = _icp(industries=("Fintech", "SaaS"))
    evidence = [
        _evidence("company-1", "company_identity", "PayFlow SaaS Inc"),
        _round_evidence(
            "company-1", "Credit Unions",
            {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Fintech"]},
            external_id="round-1-ext",
        ),
        _round_evidence(
            "company-1", "Software Publishers",
            {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]},
            external_id="round-2-ext",
        ),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_single_industry_icp_unaffected_by_cross_round_corroboration():
    """Scenario 9: a single-term ICP found across two separate rounds must
    behave exactly as the pre-existing single-round, single-term outcome
    already does — cross-round corroboration is a strict no-op for
    len(industries) <= 1 (unchanged), and the candidate's structured but
    unverifiable match now HOLDs via
    _industry_has_unprovable_compound_match instead of the old bridge."""
    icp = _icp(industries=("Healthcare",))
    evidence = [
        _evidence("company-1", "company_identity", "Community Clinic Co"),
        _round_evidence(
            "company-1", "General Medical and Surgical Hospitals",
            {
                "industry_match_branch": "linkedin_category",
                "industry_match_terms": ["Healthcare"],
                "industry_match_resolved_category_count": 1,
            },
            external_id="round-1-ext",
        ),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_confirmed_violation_across_rounds_still_fails_fail_wins():
    """Scenario 10: FAIL-wins safety is unaffected by cross-round
    corroboration. Two rounds both genuinely, honestly report an industry
    with NO partial-coverage tag at all (i.e. never touched a structured or
    keyword branch) -> the plain exact-match check still correctly FAILs;
    cross-round corroboration has no provenance to read and stays a no-op,
    exactly like its single-round counterpart
    (test_confirmed_violation_still_fails_even_with_cross_branch_style_evidence)."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    evidence = [
        _evidence("company-1", "company_identity", "Totally Unrelated Diner"),
        _evidence("company-1", "industry", "Restaurants", external_id="round-1-ext"),
        _evidence("company-1", "industry", "Restaurants", external_id="round-2-ext"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.FAIL
    assert result.evaluation.overall_result == OverallResult.FAIL


# ============================================================================
# PHASE 39 — compound-ICP recall fix: an AI-proposed combination phrase
# (icp.hard_rules.industry_combination_terms, populated only by
# app/services/discovery_strategy.py::merge_strategy_into_hard_rules) that
# structurally, exactly resolves via Explorium satisfies the FULL compound
# industry requirement on its own — see hard_icp_validation.py's
# _effective_required_industry_terms docstring for the confirmed live bug
# this fixes (a genuine, real "Clinical Software" match, sharing no words
# with either "Healthcare" or "SaaS", previously HOLD'd forever because the
# merged industries tuple counted it as a THIRD independent requirement).
# ============================================================================


def test_resolved_combination_phrase_alone_no_longer_satisfies_compound_icp():
    """Corrected (live-test audit finding): a company whose evidence
    structurally, exactly resolves the AI's own combination phrase
    ("Clinical Software") via linkedin_category still only ever reports a
    SEPARATE NAICS description ("Health Care Software Services") with no
    verified relationship to that resolved LinkedIn category — the
    identical unverifiable cross-taxonomy gap as every other structured
    match, whether or not the resolved term happens to be an AI-declared
    combination phrase. Must HOLD, never PASS, with no fabricated
    citation."""
    icp = _icp(
        industries=("Healthcare", "SaaS", "Clinical Software"),
        industry_combination_terms=("Clinical Software",),
    )
    attrs = {
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": ["Clinical Software"],
        "industry_match_resolved_category_count": 1,
    }
    evidence = [
        _evidence("company-1", "company_identity", "Acme Health SaaS Inc"),
        _evidence("company-1", "country", "United States"),
        _evidence("company-1", "employee_range", "51-200"),
        _round_evidence("company-1", "Health Care Software Services", attrs, external_id="ext-1"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evaluation.overall_result == OverallResult.HOLD
    assert result.evidence_ids["industry"] == ()


def test_single_side_match_still_holds_when_combination_terms_present():
    """The original Phase 25 false-positive protection is fully preserved:
    a candidate found via the Healthcare branch ALONE (no combination-term
    evidence at all) must still HOLD, never PASS, even though this ICP now
    also carries a combination term for a DIFFERENT, unresolved phrase."""
    icp = _icp(
        industries=("Healthcare", "SaaS", "Clinical Software"),
        industry_combination_terms=("Clinical Software",),
    )
    attrs = {
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": ["Healthcare"],
        "industry_match_resolved_category_count": 1,
    }
    evidence = [
        _evidence("company-1", "company_identity", "Precision Optical Co."),
        _round_evidence("company-1", "Optical Goods Stores", attrs, external_id="ext-1"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_combination_phrase_branch_still_requires_unambiguous_resolution():
    """A combination-phrase branch is still held to Phase 25's own
    "unambiguous, non-confounded resolution" discipline: if the SAME
    branch's OR-list resolved MORE distinct categories than the
    combination term alone accounts for (a genuine same-branch confound —
    resolved_category_count > len(this branch's own claimed terms) is
    impossible by construction, but < is a red flag this function must
    still reject), it must not bridge. Modeled here via a record whose
    tag claims the combination term but whose resolved_category_count is
    explicitly 0 (malformed/impossible provenance) — never trusted."""
    icp = _icp(
        industries=("Healthcare", "SaaS", "Clinical Software"),
        industry_combination_terms=("Clinical Software",),
    )
    attrs = {
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": ["Clinical Software"],
        "industry_match_resolved_category_count": 0,
    }
    evidence = [
        _evidence("company-1", "company_identity", "Acme Health SaaS Inc"),
        _round_evidence("company-1", "Health Care Software Services", attrs, external_id="ext-1"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS


def test_combination_term_generalizes_to_fintech_saas_no_hardcoding():
    """Corrected: the same mechanism, a different compound pair — proving
    nothing is Healthcare/SaaS-specific, including the corrected HOLD
    outcome for an AI-declared combination phrase's own unverifiable
    cross-taxonomy resolution."""
    icp = _icp(
        industries=("Fintech", "SaaS", "Financial Software"),
        industry_combination_terms=("Financial Software",),
    )
    attrs = {
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": ["Financial Software"],
        "industry_match_resolved_category_count": 1,
    }
    evidence = [
        _evidence("company-1", "company_identity", "PayFlow SaaS Inc"),
        _round_evidence("company-1", "Credit Unions", attrs, external_id="ext-1"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_no_combination_terms_recorded_behaves_exactly_as_pre_phase_39():
    """A pre-Phase-39 ICP (industry_combination_terms defaults to ()) must
    behave byte-for-byte as before this phase — the exact original Phase 25
    false-positive scenario, with no combination terms recorded at all."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    attrs = {
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": ["Healthcare"],
        "industry_match_resolved_category_count": 1,
    }
    evidence = [
        _evidence("company-1", "company_identity", "Precision Optical Co."),
        _round_evidence("company-1", "Optical Goods Stores", attrs, external_id="ext-1"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


def test_combination_term_via_cross_round_corroboration_now_holds():
    """Corrected: the combination-term recognition still composes with
    the cross-round union mechanically, but since a combination-phrase
    structured match is no longer trusted at all (unverifiable
    cross-taxonomy resolution — see _bridged_industry_terms's own
    docstring), TWO rounds of the same unverifiable evidence still cannot
    prove anything. Must HOLD, with no fabricated citation."""
    icp = _icp(
        industries=("Healthcare", "SaaS", "Clinical Software"),
        industry_combination_terms=("Clinical Software",),
    )
    combo_attrs = {
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": ["Clinical Software"],
        "industry_match_resolved_category_count": 1,
    }
    evidence = [
        _evidence("company-1", "company_identity", "Acme Health SaaS Inc"),
        _round_evidence("company-1", "Health Care Software Services", combo_attrs, external_id="round-1-ext"),
        _round_evidence("company-1", "Health Care Software Services", combo_attrs, external_id="round-2-ext"),
    ]
    result = validate_against_icp(icp, "company-1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert result.evidence_ids["industry"] == ()
