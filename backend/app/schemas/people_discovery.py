"""Phase 9 — people discovery run contracts.

PeopleDiscoveryRunResult is the pure, in-memory output of
app/services/people_discovery.py. The Read schemas are the API's
persisted-and-retrieved shape (app/api/people_discovery.py).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.providers.contracts import ProviderError
from app.schemas.candidate_person import CandidatePerson


class PeopleDiscoveryStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class ProviderRunOutcome(BaseModel):
    """One provider's contribution to a discovery run — mirrors Phase 6's
    ProviderRunOutcome for consistency."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    success: bool
    requested: int
    returned: int
    latency_ms: float | None = None
    error: ProviderError | None = None


class PeopleDiscoveryRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    icp_id: str
    icp_version: int
    company_id: str
    started_at: datetime
    status: PeopleDiscoveryStatus
    provider_outcomes: tuple[ProviderRunOutcome, ...]
    candidates: tuple[CandidatePerson, ...]


class PeopleDiscoveryRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)


class ProviderOutcomeRead(BaseModel):
    provider_id: str
    success: bool
    requested: int
    returned: int
    latency_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class CandidatePersonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    icp_id: str
    icp_version: int
    provider_id: str
    external_id: str
    name: str
    title: str | None
    attributes: dict
    discovered_at: datetime


class PeopleDiscoveryRunRead(BaseModel):
    id: str
    icp_id: str
    icp_version: int
    company_id: str
    status: str
    started_at: datetime
    requested_limit: int
    total_returned: int
    provider_outcomes: list[ProviderOutcomeRead]
    candidates: list[CandidatePersonRead]
