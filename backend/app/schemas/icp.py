"""ICP draft API contract.

Validates the *shape* of a submitted ICP draft (required fields, employee
range, empty-ICP, and the two contradiction checks called out in the Phase 0
quality contract's ICP rules). This is not the Hard ICP Rule Engine (Phase 3)
— that evaluates *leads* against a saved ICP; this only guards against an
internally-inconsistent ICP being saved in the first place.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.filter_criterion import FilterCriterion
from app.services.icp_rules import (
    validate_employee_range,
    validate_exclusion_contradictions,
    validate_geography_duplicates,
    validate_not_empty,
)


class CustomRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class HardRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    industry: list[str] = Field(default_factory=list)
    geography: list[str] = Field(default_factory=list)
    min_employees: int | None = Field(default=None, ge=0)
    max_employees: int | None = Field(default=None, ge=0)
    allowed_titles: list[str] = Field(default_factory=list)
    company_type: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    custom_rules: list[CustomRule] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.industry,
                self.geography,
                self.min_employees is not None,
                self.max_employees is not None,
                self.allowed_titles,
                self.company_type,
                self.exclusions,
                self.custom_rules,
            ]
        )


class SoftPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_model_preferences: list[str] = Field(default_factory=list)
    commercial_signals: list[str] = Field(default_factory=list)
    growth_signals: list[str] = Field(default_factory=list)
    marketing_signals: list[str] = Field(default_factory=list)
    other_preferences: list[CustomRule] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.business_model_preferences,
                self.commercial_signals,
                self.growth_signals,
                self.marketing_signals,
                self.other_preferences,
            ]
        )


class ICPCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Either supply filters (the dynamic, extensible search-criterion list -
    # see app/schemas/filter_criterion.py) OR hard_rules/soft_preferences
    # directly (the original fixed-field shape) - not both. When filters is
    # supplied, hard_rules/soft_preferences are computed server-side; see
    # _resolve_filters below.
    filters: list[FilterCriterion] = Field(default_factory=list)
    hard_rules: HardRules | None = None
    soft_preferences: SoftPreferences | None = None

    @model_validator(mode="after")
    def _validate(self) -> "ICPCreate":
        if not self.name.strip():
            raise ValueError("name must not be blank")

        if self.filters:
            if self.hard_rules is not None or self.soft_preferences is not None:
                raise ValueError("provide either filters or hard_rules/soft_preferences, not both")
            # Imported here (not at module scope) to avoid a circular import:
            # filter_projection.py imports HardRules/SoftPreferences/CustomRule
            # from this module.
            from app.services.filter_projection import FilterProjectionError, project_filters

            try:
                self.hard_rules, self.soft_preferences = project_filters(self.filters)
            except FilterProjectionError as exc:
                raise ValueError("; ".join(exc.errors)) from exc
        elif self.hard_rules is None or self.soft_preferences is None:
            raise ValueError("an ICP must include either filters or both hard_rules and soft_preferences")

        hard, soft = self.hard_rules, self.soft_preferences

        validate_employee_range(hard.min_employees, hard.max_employees)
        validate_not_empty(hard.is_empty(), soft.is_empty())
        validate_geography_duplicates(hard.geography)
        validate_exclusion_contradictions(hard)

        return self


class ICPRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    version: int
    filters: list[FilterCriterion] = Field(default_factory=list)
    hard_rules: HardRules
    soft_preferences: SoftPreferences
    created_at: datetime
