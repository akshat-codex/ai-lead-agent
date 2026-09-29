import pytest

from app.services.icp_normalization import IcpNormalizationError, normalize_icp


def _hard(**overrides) -> dict:
    base = {
        "industry": ["Skincare", "Beauty"],
        "geography": ["United States", "Canada"],
        "min_employees": 10,
        "max_employees": 200,
        "allowed_titles": ["Head of Growth", "CMO"],
        "company_type": ["D2C"],
        "exclusions": ["Wholesale-only brands"],
        "custom_rules": [{"label": "Founded after 2015", "description": "Company founded after 2015."}],
    }
    base.update(overrides)
    return base


def _soft(**overrides) -> dict:
    base = {
        "business_model_preferences": ["Subscription"],
        "commercial_signals": ["Recent funding round"],
        "growth_signals": ["Hiring for growth roles"],
        "marketing_signals": ["Active on Instagram/TikTok"],
        "other_preferences": [],
    }
    base.update(overrides)
    return base


def test_basic_normalization_maps_all_fields():
    canonical = normalize_icp("icp-1", 1, _hard(), _soft())

    assert canonical.icp_id == "icp-1"
    assert canonical.version == 1
    assert canonical.hard_rules.industries == ("Skincare", "Beauty")
    assert canonical.hard_rules.employee_range.min == 10
    assert canonical.hard_rules.employee_range.max == 200
    assert canonical.hard_rules.allowed_titles == ("Head of Growth", "CMO")
    assert canonical.hard_rules.company_types == ("D2C",)
    assert canonical.hard_rules.exclusions == ("Wholesale-only brands",)
    assert canonical.soft_preferences.business_models == ("Subscription",)


def test_hard_and_soft_preferences_stay_separated():
    canonical = normalize_icp("icp-1", 1, _hard(), _soft())

    assert not hasattr(canonical.hard_rules, "business_models")
    assert not hasattr(canonical.soft_preferences, "industries")
    # dumping to a dict should never mix the two namespaces
    dumped = canonical.model_dump()
    assert set(dumped["hard_rules"]) == {
        "industries",
        "geography",
        "employee_range",
        "allowed_titles",
        "company_types",
        "exclusions",
        "custom_rules",
        "industry_combination_terms",
    }
    assert set(dumped["soft_preferences"]) == {
        "business_models",
        "commercial_signals",
        "growth_signals",
        "marketing_signals",
        "custom_preferences",
    }


def test_geography_normalizes_known_aliases_to_country_codes():
    canonical = normalize_icp("icp-1", 1, _hard(geography=["US", "UK"]), _soft())

    codes = {entry.code for entry in canonical.hard_rules.geography.countries}
    assert codes == {"US", "GB"}
    assert canonical.hard_rules.geography.unrecognized == ()


def test_geography_never_expands_a_country_into_a_region():
    canonical = normalize_icp("icp-1", 1, _hard(geography=["US", "UK"]), _soft())

    labels = {entry.label for entry in canonical.hard_rules.geography.countries}
    assert "Europe" not in labels
    assert len(canonical.hard_rules.geography.countries) == 2


def test_geography_never_narrows_a_region_into_a_single_country():
    canonical = normalize_icp("icp-1", 1, _hard(geography=["Europe"]), _soft())

    assert canonical.hard_rules.geography.countries == ()
    assert canonical.hard_rules.geography.unrecognized == ("Europe",)


def test_geography_deduplicates_aliases_of_the_same_country():
    canonical = normalize_icp("icp-1", 1, _hard(geography=["US", "United States"]), _soft())

    assert len(canonical.hard_rules.geography.countries) == 1
    assert canonical.hard_rules.geography.countries[0].code == "US"


def test_employee_range_is_preserved_exactly():
    canonical = normalize_icp("icp-1", 1, _hard(min_employees=10, max_employees=200), _soft())
    assert canonical.hard_rules.employee_range.min == 10
    assert canonical.hard_rules.employee_range.max == 200


def test_employee_range_allows_open_ended_bounds():
    canonical = normalize_icp("icp-1", 1, _hard(min_employees=None, max_employees=200), _soft())
    assert canonical.hard_rules.employee_range.min is None
    assert canonical.hard_rules.employee_range.max == 200


def test_titles_are_cleaned_and_deduplicated_case_insensitively():
    canonical = normalize_icp(
        "icp-1", 1, _hard(allowed_titles=["  CMO ", "Head of Growth", "cmo"]), _soft()
    )
    assert canonical.hard_rules.allowed_titles == ("CMO", "Head of Growth")


def test_exclusions_are_cleaned_but_not_reinterpreted():
    canonical = normalize_icp(
        "icp-1", 1, _hard(exclusions=["  Wholesale-only   brands  "]), _soft()
    )
    assert canonical.hard_rules.exclusions == ("Wholesale-only brands",)


def test_custom_rules_and_preferences_are_normalized():
    canonical = normalize_icp(
        "icp-1",
        1,
        _hard(custom_rules=[{"label": "  Founded after 2015 ", "description": " Est. 2015+ "}]),
        _soft(other_preferences=[{"label": "Likes dogs", "description": "Company has a dog-friendly office"}]),
    )
    assert canonical.hard_rules.custom_rules[0].label == "Founded after 2015"
    assert canonical.hard_rules.custom_rules[0].description == "Est. 2015+"
    assert canonical.soft_preferences.custom_preferences[0].label == "Likes dogs"


def test_source_icp_id_and_version_are_preserved():
    canonical_v1 = normalize_icp("icp-1", 1, _hard(), _soft())
    canonical_v2 = normalize_icp("icp-2", 2, _hard(), _soft())

    assert (canonical_v1.icp_id, canonical_v1.version) == ("icp-1", 1)
    assert (canonical_v2.icp_id, canonical_v2.version) == ("icp-2", 2)
    assert canonical_v1.icp_id != canonical_v2.icp_id


def test_rejects_min_greater_than_max():
    with pytest.raises(IcpNormalizationError):
        normalize_icp("icp-1", 1, _hard(min_employees=500, max_employees=50), _soft())


def test_rejects_duplicate_geography():
    with pytest.raises(IcpNormalizationError):
        normalize_icp("icp-1", 1, _hard(geography=["United States", "united states"]), _soft())


def test_rejects_contradictory_exclusions():
    with pytest.raises(IcpNormalizationError):
        normalize_icp(
            "icp-1", 1, _hard(industry=["Healthcare"], exclusions=["healthcare"]), _soft()
        )


def test_rejects_completely_empty_icp():
    with pytest.raises(IcpNormalizationError):
        normalize_icp("icp-1", 1, {}, {})


def test_rejects_malformed_custom_rule_missing_description():
    with pytest.raises(IcpNormalizationError):
        normalize_icp(
            "icp-1", 1, _hard(custom_rules=[{"label": "Missing description"}]), _soft()
        )


def test_rejects_custom_rule_with_empty_label():
    with pytest.raises(IcpNormalizationError):
        normalize_icp("icp-1", 1, _hard(custom_rules=[{"label": "", "description": "x"}]), _soft())


def test_error_reports_do_not_silently_fix_input():
    """A failed normalization must not return a partially-fixed ICP alongside the error."""
    with pytest.raises(IcpNormalizationError) as exc_info:
        normalize_icp("icp-1", 1, _hard(min_employees=500, max_employees=50), _soft())
    assert len(exc_info.value.errors) >= 1


def test_normalization_is_idempotent_for_identical_input():
    hard, soft = _hard(geography=["US", "United States", "UK"]), _soft()
    first = normalize_icp("icp-1", 1, hard, soft)
    second = normalize_icp("icp-1", 1, hard, soft)
    assert first == second
    assert first.model_dump() == second.model_dump()


def test_already_clean_values_pass_through_unchanged():
    canonical = normalize_icp("icp-1", 1, _hard(industry=["Skincare", "Beauty"]), _soft())
    assert canonical.hard_rules.industries == ("Skincare", "Beauty")
