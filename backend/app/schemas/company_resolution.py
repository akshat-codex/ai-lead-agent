"""Phase 7 — Company Entity Resolution contracts."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ResolutionStatus(str, Enum):
    MATCH = "MATCH"
    NEW = "NEW"
    UNRESOLVED = "UNRESOLVED"


class ResolutionReasonCode(str, Enum):
    TRUSTED_PROVIDER_IDENTITY = "TRUSTED_PROVIDER_IDENTITY"
    DOMAIN_MATCH = "DOMAIN_MATCH"
    NEW_UNIQUE_DOMAIN = "NEW_UNIQUE_DOMAIN"
    NAME_ONLY_INSUFFICIENT = "NAME_ONLY_INSUFFICIENT"
    AMBIGUOUS_NAME_MULTIPLE_MATCHES = "AMBIGUOUS_NAME_MULTIPLE_MATCHES"
    PROVIDER_IDENTITY_NAME_CONFLICT = "PROVIDER_IDENTITY_NAME_CONFLICT"
    NO_IDENTITY_SIGNALS = "NO_IDENTITY_SIGNALS"


class ExistingCompanyIdentity(BaseModel):
    """The identity-relevant slice of a canonical company — everything
    resolve_candidate() needs to compare against. Deliberately excludes any
    Phase 8 enrichment field; this module only ever reasons about identity,
    never about fit, size, or anything else."""

    model_config = ConfigDict(frozen=True)

    id: str
    canonical_name: str
    canonical_domain: str | None = None
    aliases: tuple[str, ...] = ()
    provider_identities: dict[str, str] = Field(default_factory=dict)


class ResolutionResult(BaseModel):
    """One candidate's auditable resolution decision.

    canonical_company_id is populated for MATCH (the existing company) and
    NEW (the freshly minted one) — never for UNRESOLVED, since an unresolved
    candidate must not be attached to any company yet. matched_company_id is
    a separate, narrower field: the specific existing company this candidate
    appears to relate to, if any — populated for MATCH, and optionally for
    an UNRESOLVED candidate with exactly one plausible (but unconfirmed)
    match, purely as an audit hint for a future human/evidence process.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    status: ResolutionStatus
    canonical_company_id: str | None = None
    matched_company_id: str | None = None
    confidence: str | None = None
    matched_signals: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    reason_code: ResolutionReasonCode
    explanation: str


class CompanyResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovery_run_id: str = Field(min_length=1)


class ResolutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    discovery_run_id: str
    status: str
    canonical_company_id: str | None
    matched_company_id: str | None
    confidence: str | None
    matched_signals: list[str]
    conflicting_signals: list[str]
    reason_code: str
    explanation: str
    resolved_at: datetime


class CanonicalCompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    canonical_name: str
    canonical_domain: str | None
    aliases: list[str]
    provider_identities: dict[str, str]
    created_at: datetime
    updated_at: datetime
