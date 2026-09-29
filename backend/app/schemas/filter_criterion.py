"""Generic, extensible search-criterion contract.

A FilterCriterion represents one filter in a dynamic ICP/search definition -
either a "known" filter (one of the catalog's first-class keys, e.g.
industry, employee_range) or an arbitrary backend-supported filter added to
the catalog later (funding, hiring, business_model, revenue, technology,
website), or a free-text custom requirement with no dedicated filter
(key="custom", operator=CONTAINS, value=<free text>, label=<user title>).

This schema is intentionally decoupled from HardRules/SoftPreferences's fixed
fields - see app/services/filter_projection.py for the deterministic mapping
between the two. Nothing here assumes which keys exist; that is the filter
catalog's (app/services/filter_catalog.py) job.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FilterOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"  # value: list - membership
    NOT_IN = "not_in"  # value: list - exclusion
    CONTAINS = "contains"  # value: str - substring/free-text match
    RANGE = "range"  # value: {"min": num|None, "max": num|None}
    GTE = "gte"
    LTE = "lte"
    EXISTS = "exists"  # value ignored - "has any signal for this key"


class FilterValueType(str, Enum):
    """What kind of value this filter's UI control edits - drives which
    specialized control the frontend renders, and which operators are valid."""

    MULTI_SELECT = "multi_select"  # value: list[str], typically operator=IN
    SINGLE_SELECT = "single_select"  # value: str, typically operator=EQUALS
    RANGE = "range"  # value: {min,max}, operator=RANGE
    TEXT = "text"  # value: str, operator=CONTAINS/EQUALS
    BOOLEAN = "boolean"  # value: bool, operator=EQUALS/EXISTS
    ENUM = "enum"  # value: str from a fixed option list


class FilterCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    # "custom" is the reserved key for free-text requirements with no
    # dedicated catalog entry - see filter_catalog.CUSTOM_FILTER_KEY.
    operator: FilterOperator
    value: Any
    # Optional human label - required when key == "custom" (the user names
    # their own ad hoc requirement); optional override for known keys
    # (defaults to the catalog's display_label if omitted).
    label: str | None = None

    @model_validator(mode="after")
    def _validate_custom_has_label(self) -> "FilterCriterion":
        if self.key == "custom" and not (self.label and self.label.strip()):
            raise ValueError("a custom filter requires a non-blank label")
        return self
