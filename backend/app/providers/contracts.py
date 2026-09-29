"""Phase 5 — provider abstraction contracts.

Everything downstream of a provider adapter (the registry, and eventually
the Phase 6+ discovery/enrichment pipeline) depends only on these types —
never on a vendor SDK's request/response shapes. A vendor-specific field
name must never appear on a ProviderResponse; app/providers/base.py's
ProviderAdapter is exactly the boundary responsible for that translation.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderCapability(str, Enum):
    COMPANY_DISCOVERY = "COMPANY_DISCOVERY"
    PEOPLE_DISCOVERY = "PEOPLE_DISCOVERY"
    COMPANY_ENRICHMENT = "COMPANY_ENRICHMENT"
    PERSON_ENRICHMENT = "PERSON_ENRICHMENT"
    WEB_SEARCH = "WEB_SEARCH"


class ProviderRequest(BaseModel):
    """A capability-scoped request to a provider.

    `query` is deliberately an opaque mapping — Phase 6+ will define
    capability-specific input schemas (e.g. a CompanyDiscoveryQuery); this
    phase only fixes the envelope around whatever that input ends up being,
    so it must not assume a shape no capability has defined yet.
    """

    model_config = ConfigDict(frozen=True)

    capability: ProviderCapability
    query: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    cursor: str | None = None


class NormalizedRecord(BaseModel):
    """One normalized result item, in a shape common to every provider.

    external_id / name / attributes are the only fields — no vendor-specific
    key names are allowed here. A real adapter (Phase 6+) is responsible for
    mapping its vendor's response into exactly this shape before it ever
    reaches core code.
    """

    model_config = ConfigDict(frozen=True)

    external_id: str
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProviderError(BaseModel):
    """Machine-readable failure information.

    `code` is a plain string, not a closed enum: this module's own codes are
    documented on ProviderErrorCode (see base.py), but a real future
    provider will have its own vendor-specific failure taxonomy we cannot
    anticipate here.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool = False


class RequestCost(BaseModel):
    """The (estimated) cost of one specific provider call."""

    model_config = ConfigDict(frozen=True)

    amount: float = 0.0
    currency: str = "USD"
    billable_units: int = 1


class SourceMetadata(BaseModel):
    """Provenance for whatever NormalizedRecord(s) a response carries."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    provider_name: str
    retrieved_at: datetime
    is_mock: bool = False
    raw_reference: str | None = None


class ProviderResponse(BaseModel):
    """`cursor` and `exhausted` are optional, capability-agnostic pagination
    signals — a provider that supports continuing a query (e.g. a real
    COMPANY_DISCOVERY provider with a next-page token) sets `cursor` to an
    opaque value the caller can pass back as `ProviderRequest.cursor` on a
    later call, and sets `exhausted=True` once it has confirmed there is
    nothing more to return for that query. A provider with no pagination
    concept (every mock, and any one-shot capability) simply leaves both at
    their defaults — `cursor=None` alone never means "exhausted," since a
    non-paginating provider always returns `cursor=None` without having
    exhausted anything; only `exhausted` is the authoritative signal."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    capability: ProviderCapability
    success: bool
    data: tuple[NormalizedRecord, ...] = ()
    error: ProviderError | None = None
    latency_ms: float | None = None
    cost: RequestCost | None = None
    source: SourceMetadata | None = None
    cursor: str | None = None
    exhausted: bool = False


class ProviderReliabilityProfile(BaseModel):
    """Static, self-declared provider-level metadata.

    Phase 5 only defines the shape so later phases have somewhere to put
    real numbers. Nothing here is measured, observed, or learned yet — that
    is Phase 27 (Provider Reliability Router).
    """

    model_config = ConfigDict(frozen=True)

    accuracy: float | None = Field(default=None, ge=0, le=1)
    coverage: float | None = Field(default=None, ge=0, le=1)
    freshness_days: float | None = Field(default=None, ge=0)
    cost_per_request: float | None = Field(default=None, ge=0)
    average_latency_ms: float | None = Field(default=None, ge=0)
    rate_limit_per_minute: int | None = Field(default=None, ge=0)
    failure_rate: float | None = Field(default=None, ge=0, le=1)
