"""Projects a generic filters[] list down into the existing HardRules /
SoftPreferences shape, so every downstream consumer (hard_rule_engine,
icp_normalization, company_discovery, people_discovery,
qualification_context, hard_icp_validation) keeps working completely
unchanged.

This is a deliberate, temporary compatibility layer - not a permanent second
source of truth. filters[] is authoritative; hard_rules/soft_preferences on
ICPCreate become OPTIONAL, server-computed fields once filters[] is supplied.
"""
from __future__ import annotations

from app.schemas.filter_criterion import FilterCriterion
from app.schemas.icp import CustomRule, HardRules, SoftPreferences
from app.services.filter_catalog import (
    CUSTOM_FILTER_KEY,
    UnknownFilterKeyError,
    get_filter_definition,
    validate_criterion,
)

# Known keys that map onto a HardRules list field via IN/NOT_IN.
_HARD_LIST_FIELDS = {
    "industry": "industry",
    "geography": "geography",
    "allowed_titles": "allowed_titles",
    "company_type": "company_type",
}
_SOFT_LIST_FIELDS = {
    "business_model": "business_model_preferences",
    "commercial_signals": "commercial_signals",
    "marketing_signals": "marketing_signals",
    # funding/hiring/technology/revenue/website: no dedicated
    # HardRules/SoftPreferences field exists yet. Until a dedicated field or
    # pipeline stage is added, they fall through to
    # soft_preferences.other_preferences as a labeled custom entry (see
    # _describe) so the intent is never silently dropped.
}


class FilterProjectionError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def project_filters(filters: list[FilterCriterion]) -> tuple[HardRules, SoftPreferences]:
    errors: list[str] = []
    industry: list[str] = []
    geography: list[str] = []
    allowed_titles: list[str] = []
    company_type: list[str] = []
    exclusions: list[str] = []
    hard_custom: list[CustomRule] = []
    min_employees: int | None = None
    max_employees: int | None = None

    business_model_preferences: list[str] = []
    commercial_signals: list[str] = []
    growth_signals: list[str] = []
    marketing_signals: list[str] = []
    soft_custom: list[CustomRule] = []

    for criterion in filters:
        try:
            validate_criterion(criterion)
        except UnknownFilterKeyError as exc:
            errors.append(str(exc))
            continue
        except ValueError as exc:  # InvalidFilterOperatorError
            errors.append(str(exc))
            continue

        key = criterion.key
        definition = get_filter_definition(key)

        if key == "employee_range":
            value = criterion.value or {}
            min_employees = value.get("min")
            max_employees = value.get("max")
            continue

        if key == CUSTOM_FILTER_KEY:
            hard_custom.append(
                CustomRule(label=criterion.label or "Custom requirement", description=str(criterion.value))
            )
            continue

        if key == "exclusions":
            exclusions = list(criterion.value or [])
            continue

        if key in _HARD_LIST_FIELDS:
            target = {
                "industry": industry,
                "geography": geography,
                "allowed_titles": allowed_titles,
                "company_type": company_type,
            }[key]
            target.extend(criterion.value or [])
            continue

        if key in _SOFT_LIST_FIELDS:
            target = {
                "business_model": business_model_preferences,
                "commercial_signals": commercial_signals,
                "marketing_signals": marketing_signals,
            }[key]
            target.extend(criterion.value or [])
            continue

        # Recognized-but-not-yet-field-backed (funding, hiring, technology,
        # revenue, website): preserve as a labeled soft custom preference so
        # nothing the user asked for is silently dropped, even though no
        # dedicated pipeline stage consumes it yet.
        label = criterion.label or (definition.display_label if definition else key)
        soft_custom.append(CustomRule(label=label, description=_describe(criterion)))

    if errors:
        raise FilterProjectionError(errors)

    hard = HardRules(
        industry=industry,
        geography=geography,
        min_employees=min_employees,
        max_employees=max_employees,
        allowed_titles=allowed_titles,
        company_type=company_type,
        exclusions=exclusions,
        custom_rules=hard_custom,
    )
    soft = SoftPreferences(
        business_model_preferences=business_model_preferences,
        commercial_signals=commercial_signals,
        growth_signals=growth_signals,
        marketing_signals=marketing_signals,
        other_preferences=soft_custom,
    )
    return hard, soft


def _describe(criterion: FilterCriterion) -> str:
    return f"{criterion.operator.value}: {criterion.value!r}"
