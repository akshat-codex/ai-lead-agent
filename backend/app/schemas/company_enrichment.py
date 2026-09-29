"""Phase 8 — Company Enrichment contracts.

Enrichment discovers facts about an already-resolved canonical company
(Phase 7); it never decides identity, fitness, or fit. Every fact keeps
enough provenance to later support the Phase 11 Evidence Engine — but this
phase does not verify, score, or resolve anything itself.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.providers.contracts import ProviderError


class EnrichmentRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class CompanyEnrichmentQuery(BaseModel):
    """The COMPANY_ENRICHMENT capability's input shape.

    external_id is populated only when the target provider already has a
    recorded identity for this company (from Phase 7's provider_identities)
    — giving a real provider a precise lookup instead of a fuzzy one,
    exactly like a real enrichment API would prefer.
    """

    model_config = ConfigDict(frozen=True)

    domain: str | None = None
    company_name: str | None = None
    external_id: str | None = None


class EnrichmentFact(BaseModel):
    """One (field, value) pair as reported by exactly one provider.

    confidence is None unless a provider itself supplies one — it is never
    invented here, and returning a value is never treated as "verified."
    """

    model_config = ConfigDict(frozen=True)

    field: str
    value: Any
    provider_id: str
    external_id: str | None = None
    retrieved_at: datetime
    confidence: float | None = Field(default=None, ge=0, le=1)


class EnrichmentField(BaseModel):
    """Every fact gathered for one field, across every provider that
    returned it. If more than one distinct value was returned, `conflict`
    is true and no single value is chosen as truth — resolving that is a
    later verification/evidence phase's job, not this one's."""

    model_config = ConfigDict(frozen=True)

    field: str
    facts: tuple[EnrichmentFact, ...]
    conflict: bool


class ProviderEnrichmentOutcome(BaseModel):
    """One provider's contribution to an enrichment run — mirrors Phase 6's
    ProviderRunOutcome for consistency."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    success: bool
    fields_returned: int
    latency_ms: float | None = None
    error: ProviderError | None = None


class EnrichmentResult(BaseModel):
    """The full, in-memory result of one enrichment run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    company_id: str
    started_at: datetime
    status: EnrichmentRunStatus
    provider_outcomes: tuple[ProviderEnrichmentOutcome, ...]
    fields: tuple[EnrichmentField, ...]


# --- API read/request schemas ---------------------------------------------


class ProviderEnrichmentOutcomeRead(BaseModel):
    provider_id: str
    success: bool
    fields_returned: int
    latency_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class EnrichmentRunRead(BaseModel):
    id: str
    company_id: str
    status: str
    started_at: datetime
    provider_outcomes: list[ProviderEnrichmentOutcomeRead]


class EnrichmentFactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    field: str
    value: Any
    provider_id: str
    external_id: str | None
    confidence: float | None
    retrieved_at: datetime


class CompanyFactsRead(BaseModel):
    """The aggregated, current view of everything ever enriched for a
    company — every fact from every run to date, grouped by field."""

    company_id: str
    fields: list[EnrichmentField]
