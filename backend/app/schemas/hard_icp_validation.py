"""Phase 12 — evidence-backed hard ICP validation contracts.

The core payload is the unchanged Phase 3 HardRuleEvaluation, embedded
verbatim rather than re-declared — Phase 12 adds provenance and
persistence around it, never a second copy of its rule logic or result
shape.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.hard_rule_result import HardRuleEvaluation


class HardIcpValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    person_id: str | None = None


class HardIcpValidationResult(BaseModel):
    """The full, in-memory result of one validation run — Phase 3's
    unchanged evaluation, plus which evidence records backed each rule."""

    model_config = ConfigDict(frozen=True)

    company_id: str
    person_id: str | None
    evaluation: HardRuleEvaluation
    evidence_ids: dict[str, tuple[str, ...]]


class HardIcpValidationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    overall_result: str
    rule_results: list[dict]
    failed_rules: list[dict]
    unresolved_rules: list[dict]
    reason_codes: list[str]
    explanation: str
    evidence_ids: dict[str, list[str]]
    validated_at: datetime
