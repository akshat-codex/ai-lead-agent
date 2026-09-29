"""Phase 29 — end-to-end pipeline run schemas.

A PipelineRun wraps exactly one Phase 21 BatchModel (Discovery through
Deduplication, entirely unchanged) and then, for every lead that ends up
under the ICP as a result, records the read-only status of the three
downstream inspection stages that already exist as separate phases:
Phase 28 confidence, Phase 22 ranking, and Phase 23 export. Human Review
(Phase 20) is a human-in-the-loop action with no automatic trigger — the
run reports how many leads are still awaiting it, but never submits a
review itself.

No new stage logic, scoring, qualification, identity, evidence, ranking,
or deduplication rule is introduced anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PipelineRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"


class PipelineStageName(str, Enum):
    """The full advertised pipeline, in order. DISCOVERY..DEDUPLICATION are
    executed by the unchanged Phase 21 batch orchestrator; HUMAN_REVIEW is
    manual and only ever observed, never advanced by this module; RANKING,
    CONFIDENCE, and EXPORT are computed read-only from whatever the prior
    stages already produced."""

    DISCOVERY = "DISCOVERY"
    COMPANY_RESOLUTION = "COMPANY_RESOLUTION"
    ENRICHMENT = "ENRICHMENT"
    PEOPLE_DISCOVERY_RESOLUTION = "PEOPLE_DISCOVERY_RESOLUTION"
    EVIDENCE = "EVIDENCE"
    HARD_VALIDATION = "HARD_VALIDATION"
    VERIFICATION = "VERIFICATION"
    BUSINESS_MODEL_SIGNALS = "BUSINESS_MODEL_SIGNALS"
    SCORING = "SCORING"
    LLM_QUALIFICATION = "LLM_QUALIFICATION"
    ADVERSARIAL_REVIEW = "ADVERSARIAL_REVIEW"
    DEDUPLICATION = "DEDUPLICATION"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    RANKING = "RANKING"
    CONFIDENCE = "CONFIDENCE"
    EXPORT = "EXPORT"


class PipelineStageStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: PipelineStageName
    attempted: int
    succeeded: int
    failed: int
    pending: int = 0
    note: str | None = None


class PipelineLeadStatus(BaseModel):
    """Per-lead terminal snapshot of the downstream (post-batch) stages,
    built entirely from already-persisted Phase 20/22/28 rows — never a
    second copy of their data."""

    model_config = ConfigDict(frozen=True)

    lead_id: str
    company_id: str
    person_id: str | None
    batch_outcome: str | None
    hard_rule_result: str | None
    readiness: str
    ranking_tier: str | None
    human_review_decision: str | None
    human_review_pending: bool


class PipelineRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    target_count: int = Field(ge=1, le=1000)
    discovery_limit: int = Field(default=20, ge=1, le=100)
    people_limit_per_company: int = Field(default=5, ge=1, le=100)


class PipelineRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    batch_id: str
    status: PipelineRunStatus
    lead_count: int
    accepted_count: int
    held_count: int
    rejected_count: int
    duplicate_count: int
    failed_count: int
    human_review_pending_count: int
    started_at: datetime
    completed_at: datetime | None


class PipelineRunDetail(PipelineRunRead):
    stage_statuses: tuple[PipelineStageStatus, ...]
    leads: tuple[PipelineLeadStatus, ...]
