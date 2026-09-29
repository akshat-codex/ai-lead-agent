"""Phase 6 — discovery run contracts.

DiscoveryRunResult is the pure, in-memory output of
app/services/company_discovery.py. The Read schemas below are the API's
persisted-and-retrieved shape (app/api/discovery.py); the two are separate
because a persisted run also carries storage-only bookkeeping (requested
limit, total returned) that the pure service result derives on demand.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.providers.contracts import ProviderError
from app.schemas.candidate_company import CandidateCompany


class DiscoveryStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class ProviderRunOutcome(BaseModel):
    """One provider's contribution to a discovery run — the traceable
    per-provider success/failure record the Discovery Metadata requirement
    asks for."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    success: bool
    requested: int
    returned: int
    latency_ms: float | None = None
    error: ProviderError | None = None


class DiscoveryRunResult(BaseModel):
    """The full, in-memory result of one discovery run.

    `next_cursors`/`exhausted_providers` are additive, default-empty
    pagination bookkeeping — only populated when a caller passed cursors in
    and providers actually returned pagination signals (see
    app/services/company_discovery.py::run_company_discovery). Every
    existing caller that never uses cursors gets empty dicts/tuples here,
    unchanged from before this field existed."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    icp_id: str
    icp_version: int
    started_at: datetime
    status: DiscoveryStatus
    provider_outcomes: tuple[ProviderRunOutcome, ...]
    candidates: tuple[CandidateCompany, ...]
    next_cursors: dict[str, str] = Field(default_factory=dict)
    exhausted_providers: tuple[str, ...] = ()


class DiscoveryRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)


class ProviderOutcomeRead(BaseModel):
    provider_id: str
    success: bool
    requested: int
    returned: int
    latency_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class CandidateCompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    provider_id: str
    external_id: str
    name: str
    domain: str | None
    attributes: dict
    discovered_at: datetime


class DiscoveryRunRead(BaseModel):
    id: str
    icp_id: str
    icp_version: int
    status: str
    started_at: datetime
    requested_limit: int
    total_returned: int
    provider_outcomes: list[ProviderOutcomeRead]
    candidates: list[CandidateCompanyRead]
