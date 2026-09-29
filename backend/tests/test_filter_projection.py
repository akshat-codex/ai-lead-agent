import pytest

from app.schemas.filter_criterion import FilterCriterion, FilterOperator
from app.services.filter_projection import FilterProjectionError, project_filters


def test_industry_maps_to_hard_rules_industry():
    hard, soft = project_filters([FilterCriterion(key="industry", operator=FilterOperator.IN, value=["D2C"])])
    assert hard.industry == ["D2C"]


def test_geography_maps_to_hard_rules_geography():
    hard, _ = project_filters([FilterCriterion(key="geography", operator=FilterOperator.IN, value=["United States"])])
    assert hard.geography == ["United States"]


def test_allowed_titles_maps_to_hard_rules_allowed_titles():
    hard, _ = project_filters([FilterCriterion(key="allowed_titles", operator=FilterOperator.IN, value=["CEO"])])
    assert hard.allowed_titles == ["CEO"]


def test_company_type_maps_to_hard_rules_company_type():
    hard, _ = project_filters([FilterCriterion(key="company_type", operator=FilterOperator.IN, value=["D2C"])])
    assert hard.company_type == ["D2C"]


def test_exclusions_maps_to_hard_rules_exclusions():
    hard, _ = project_filters([FilterCriterion(key="exclusions", operator=FilterOperator.NOT_IN, value=["Agencies"])])
    assert hard.exclusions == ["Agencies"]


def test_employee_range_with_both_bounds():
    hard, _ = project_filters(
        [FilterCriterion(key="employee_range", operator=FilterOperator.RANGE, value={"min": 10, "max": 300})]
    )
    assert hard.min_employees == 10
    assert hard.max_employees == 300


def test_employee_range_with_only_min():
    hard, _ = project_filters(
        [FilterCriterion(key="employee_range", operator=FilterOperator.RANGE, value={"min": 10, "max": None})]
    )
    assert hard.min_employees == 10
    assert hard.max_employees is None


def test_employee_range_with_only_max():
    hard, _ = project_filters(
        [FilterCriterion(key="employee_range", operator=FilterOperator.RANGE, value={"min": None, "max": 300})]
    )
    assert hard.min_employees is None
    assert hard.max_employees == 300


def test_employee_range_with_neither():
    hard, _ = project_filters(
        [FilterCriterion(key="employee_range", operator=FilterOperator.RANGE, value={"min": None, "max": None})]
    )
    assert hard.min_employees is None
    assert hard.max_employees is None


def test_business_model_maps_to_soft_preferences():
    _, soft = project_filters([FilterCriterion(key="business_model", operator=FilterOperator.IN, value=["SaaS"])])
    assert soft.business_model_preferences == ["SaaS"]


def test_commercial_signals_maps_to_soft_preferences():
    _, soft = project_filters(
        [FilterCriterion(key="commercial_signals", operator=FilterOperator.IN, value=["High tool spend"])]
    )
    assert soft.commercial_signals == ["High tool spend"]


def test_marketing_signals_maps_to_soft_preferences():
    _, soft = project_filters(
        [FilterCriterion(key="marketing_signals", operator=FilterOperator.IN, value=["Paid social"])]
    )
    assert soft.marketing_signals == ["Paid social"]


def test_custom_key_maps_to_hard_rules_custom_rules():
    hard, _ = project_filters(
        [FilterCriterion(key="custom", operator=FilterOperator.CONTAINS, value="Must use Shopify", label="Uses Shopify")]
    )
    assert len(hard.custom_rules) == 1
    assert hard.custom_rules[0].label == "Uses Shopify"
    assert "Must use Shopify" in hard.custom_rules[0].description


@pytest.mark.parametrize(
    ("key", "operator", "value"),
    [
        ("funding", FilterOperator.IN, ["signal"]),
        ("hiring", FilterOperator.IN, ["signal"]),
        ("revenue", FilterOperator.RANGE, {"min": 1_000_000, "max": None}),
        ("technology", FilterOperator.IN, ["signal"]),
        ("website", FilterOperator.CONTAINS, "shopify.com"),
    ],
)
def test_not_yet_field_backed_keys_land_in_other_preferences(key, operator, value):
    _, soft = project_filters([FilterCriterion(key=key, operator=operator, value=value)])
    assert len(soft.other_preferences) == 1
    assert soft.other_preferences[0].description  # readable, non-empty


def test_unknown_key_raises_filter_projection_error_naming_the_key():
    with pytest.raises(FilterProjectionError) as exc_info:
        project_filters([FilterCriterion(key="not_a_real_key", operator=FilterOperator.IN, value=["x"])])
    assert "not_a_real_key" in str(exc_info.value)


def test_empty_filters_list_produces_empty_hard_and_soft():
    hard, soft = project_filters([])
    assert hard.is_empty()
    assert soft.is_empty()
