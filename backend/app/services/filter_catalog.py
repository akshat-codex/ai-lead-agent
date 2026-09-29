"""Filter catalog - the backend-owned list of filters the ICP/search builder
supports. Adding a new filter here is the ONLY change needed to make it
selectable in the frontend's '+ Add filter' search - no frontend redesign,
no schema change to FilterCriterion.

A plain, static, in-memory registry (same pattern as
app/providers/default_registry.py) - filter definitions are a
developer-controlled capability surface, not user data, so no DB table or
migration is needed to add one.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.schemas.filter_criterion import FilterCriterion, FilterOperator, FilterValueType


class FilterCategory(str, Enum):
    HARD = "hard"  # maps into HardRules - mandatory/disqualifying
    SOFT = "soft"  # maps into SoftPreferences - non-blocking ranking signal


class FilterDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    display_label: str
    description: str
    category: FilterCategory
    value_type: FilterValueType
    allowed_operators: tuple[FilterOperator, ...]
    # For ENUM/SINGLE_SELECT/MULTI_SELECT with a closed option set. None means
    # free-form (any string the user types), e.g. industry/geography today.
    options: tuple[str, ...] | None = None
    # True for the 6 fixed hard_rule fields + custom, which have bespoke
    # frontend controls. False means the frontend renders a generic control
    # for value_type.
    has_specialized_ui: bool = False


CUSTOM_FILTER_KEY = "custom"

_CATALOG: tuple[FilterDefinition, ...] = (
    # --- known/first-class filters (map 1:1 onto today's HardRules fields) ---
    FilterDefinition(
        key="industry",
        display_label="Industry",
        description="Allowed industries.",
        category=FilterCategory.HARD,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="geography",
        display_label="Location",
        description="Allowed countries/regions.",
        category=FilterCategory.HARD,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="employee_range",
        display_label="Employees",
        description="Employee headcount range.",
        category=FilterCategory.HARD,
        value_type=FilterValueType.RANGE,
        allowed_operators=(FilterOperator.RANGE,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="allowed_titles",
        display_label="Titles / Decision makers",
        description="Allowed job titles for the decision-maker being evaluated.",
        category=FilterCategory.HARD,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="company_type",
        display_label="Company type",
        description="Allowed company types (e.g. D2C, B2B).",
        category=FilterCategory.HARD,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="exclusions",
        display_label="Exclusions",
        description="Companies/domains/terms to always reject.",
        category=FilterCategory.HARD,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.NOT_IN,),
        has_specialized_ui=True,
    ),
    # --- soft/commercial signal filters (map 1:1 onto today's SoftPreferences fields) ---
    FilterDefinition(
        key="business_model",
        display_label="Business model",
        description="Preferred business models (e.g. SaaS, D2C, Subscription).",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="commercial_signals",
        display_label="Commercial signals",
        description="Buying triggers, tooling spend, tech stack signals.",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    FilterDefinition(
        key="funding",
        display_label="Funding",
        description="Funding/growth signals (e.g. recently raised).",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=False,
    ),
    FilterDefinition(
        key="hiring",
        display_label="Hiring",
        description="Hiring signals (e.g. hiring for a function).",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=False,
    ),
    FilterDefinition(
        key="marketing_signals",
        display_label="Marketing signals",
        description="Marketing activity/channel signals.",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=True,
    ),
    # --- catalog-visible, not yet backed by a dedicated pipeline stage: the
    # ICP can *record* this intent (roundtrips into soft_preferences.other_
    # preferences, see filter_projection.py), but it does not yet influence
    # scoring until a dedicated extractor/scorer exists. ---
    FilterDefinition(
        key="revenue",
        display_label="Revenue",
        description="Revenue range or bracket.",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.RANGE,
        allowed_operators=(FilterOperator.RANGE,),
        has_specialized_ui=False,
    ),
    FilterDefinition(
        key="technology",
        display_label="Technology",
        description="Technology/tool usage signals.",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.MULTI_SELECT,
        allowed_operators=(FilterOperator.IN,),
        has_specialized_ui=False,
    ),
    FilterDefinition(
        key="website",
        display_label="Website",
        description="Website/domain-based criteria.",
        category=FilterCategory.SOFT,
        value_type=FilterValueType.TEXT,
        allowed_operators=(FilterOperator.CONTAINS,),
        has_specialized_ui=False,
    ),
    # --- escape hatch: free-text custom requirement, always available ---
    FilterDefinition(
        key=CUSTOM_FILTER_KEY,
        display_label="Custom requirement",
        description="Any requirement with no dedicated filter yet - free text.",
        category=FilterCategory.HARD,
        value_type=FilterValueType.TEXT,
        allowed_operators=(FilterOperator.CONTAINS,),
        has_specialized_ui=True,
    ),
)

_BY_KEY: dict[str, FilterDefinition] = {d.key: d for d in _CATALOG}


def list_filter_definitions() -> tuple[FilterDefinition, ...]:
    return _CATALOG


def get_filter_definition(key: str) -> FilterDefinition | None:
    return _BY_KEY.get(key)


def is_known_key(key: str) -> bool:
    return key in _BY_KEY


class UnknownFilterKeyError(ValueError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"unknown filter key: {key!r}")


class InvalidFilterOperatorError(ValueError):
    def __init__(self, key: str, operator: str, allowed: tuple[str, ...]):
        self.key = key
        self.operator = operator
        self.allowed = allowed
        super().__init__(f"operator {operator!r} not valid for filter {key!r}; allowed: {allowed}")


def validate_criterion(criterion: FilterCriterion) -> None:
    """Raises UnknownFilterKeyError / InvalidFilterOperatorError if the
    criterion references a key the catalog does not know, or uses an operator
    not allowed for that key."""
    definition = get_filter_definition(criterion.key)
    if definition is None:
        raise UnknownFilterKeyError(criterion.key)
    if criterion.operator not in definition.allowed_operators:
        raise InvalidFilterOperatorError(
            criterion.key,
            criterion.operator.value,
            tuple(op.value for op in definition.allowed_operators),
        )
