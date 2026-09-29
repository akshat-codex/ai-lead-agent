"""Cross-field ICP consistency rules.

Shared between the Phase 1 save-time validation (app/schemas/icp.py) and the
Phase 2 normalization engine (app/services/icp_normalization.py) so the two
layers can never drift apart on what counts as an internally-inconsistent
ICP. Each function raises ValueError with a human-readable message; callers
decide how to surface it (a pydantic model_validator turns it into a 422, the
normalization engine collects it into an IcpNormalizationError).
"""
from __future__ import annotations

from typing import Protocol


class _HasHardRuleFields(Protocol):
    industry: list[str]
    geography: list[str]
    min_employees: int | None
    max_employees: int | None
    allowed_titles: list[str]
    company_type: list[str]
    exclusions: list[str]


def validate_employee_range(min_employees: int | None, max_employees: int | None) -> None:
    if min_employees is not None and max_employees is not None:
        if min_employees > max_employees:
            raise ValueError("min_employees must not be greater than max_employees")


def validate_not_empty(hard_is_empty: bool, soft_is_empty: bool) -> None:
    if hard_is_empty and soft_is_empty:
        raise ValueError("an ICP must include at least one hard rule or soft preference")


def validate_geography_duplicates(geography: list[str]) -> None:
    geography_lower = [g.strip().lower() for g in geography if g.strip()]
    if len(geography_lower) != len(set(geography_lower)):
        raise ValueError("geography list contains duplicate/contradictory entries")


def validate_exclusion_contradictions(hard: _HasHardRuleFields) -> None:
    exclusions_lower = {e.strip().lower() for e in hard.exclusions if e.strip()}
    if not exclusions_lower:
        return

    allowed_terms = {
        v.strip().lower()
        for field in (hard.industry, hard.geography, hard.allowed_titles, hard.company_type)
        for v in field
        if v.strip()
    }
    conflicts = exclusions_lower & allowed_terms
    if conflicts:
        raise ValueError(f"exclusions contradict allowed values: {', '.join(sorted(conflicts))}")
