import pytest

from app.schemas.filter_criterion import FilterCriterion, FilterOperator
from app.services.filter_catalog import (
    InvalidFilterOperatorError,
    UnknownFilterKeyError,
    get_filter_definition,
    is_known_key,
    list_filter_definitions,
    validate_criterion,
)


def test_list_filter_definitions_includes_all_six_known_hard_fields():
    keys = {d.key for d in list_filter_definitions()}
    for known in ("industry", "geography", "employee_range", "allowed_titles", "company_type", "exclusions"):
        assert known in keys


def test_list_filter_definitions_includes_catalog_visible_soft_filters():
    keys = {d.key for d in list_filter_definitions()}
    for key in ("funding", "hiring", "revenue", "technology", "website", "custom"):
        assert key in keys


def test_get_filter_definition_unknown_key_returns_none():
    assert get_filter_definition("nonexistent") is None


def test_is_known_key():
    assert is_known_key("industry") is True
    assert is_known_key("nonexistent") is False


def test_validate_criterion_unknown_key_raises():
    criterion = FilterCriterion(key="nonexistent", operator=FilterOperator.IN, value=["x"])
    with pytest.raises(UnknownFilterKeyError):
        validate_criterion(criterion)


def test_validate_criterion_disallowed_operator_raises():
    criterion = FilterCriterion(key="geography", operator=FilterOperator.EQUALS, value="US")
    with pytest.raises(InvalidFilterOperatorError):
        validate_criterion(criterion)


def test_validate_criterion_valid_passes():
    criterion = FilterCriterion(key="geography", operator=FilterOperator.IN, value=["US"])
    validate_criterion(criterion)  # should not raise


def test_custom_key_is_exempt_from_key_validation_but_not_operator_validation():
    valid = FilterCriterion(key="custom", operator=FilterOperator.CONTAINS, value="anything", label="My rule")
    validate_criterion(valid)  # should not raise

    invalid = FilterCriterion(key="custom", operator=FilterOperator.IN, value=["x"], label="My rule")
    with pytest.raises(InvalidFilterOperatorError):
        validate_criterion(invalid)
