"""Phase 10 — Person Identity Resolution contracts.

Reuses Phase 7's ResolutionStatus (MATCH/NEW/UNRESOLVED) — the same
three-way entity-resolution outcome, just applied to people instead of
companies; there is no reason to define a second, identical enum. Reason
codes are person-specific since the underlying signals differ (provider
identity + LinkedIn identifier + name/company context, vs. provider
identity + domain for companies).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.company_resolution import ResolutionStatus


class PersonResolutionReasonCode(str, Enum):
    TRUSTED_PROVIDER_IDENTITY = "TRUSTED_PROVIDER_IDENTITY"
    LINKEDIN_ID_MATCH = "LINKEDIN_ID_MATCH"
    NEW_UNIQUE_LINKEDIN_ID = "NEW_UNIQUE_LINKEDIN_ID"
    NAME_AND_COMPANY_INSUFFICIENT = "NAME_AND_COMPANY_INSUFFICIENT"
    AMBIGUOUS_NAME_MULTIPLE_MATCHES = "AMBIGUOUS_NAME_MULTIPLE_MATCHES"
    PROVIDER_IDENTITY_CONFLICT = "PROVIDER_IDENTITY_CONFLICT"
    IDENTITY_SIGNAL_CONFLICT = "IDENTITY_SIGNAL_CONFLICT"
    NEW_DIFFERENT_COMPANY_CONTEXT = "NEW_DIFFERENT_COMPANY_CONTEXT"
    NO_IDENTITY_SIGNALS = "NO_IDENTITY_SIGNALS"


class ExistingPersonIdentity(BaseModel):
    """The identity-relevant slice of a canonical person — everything
    resolve_candidate() needs to compare against. Deliberately excludes any
    verification/qualification field; this module only ever reasons about
    identity, never fit or fitness.

    associated_company_ids always includes canonical_company_id once it is
    set (see app/services/person_resolution.py) — it is the full, growing
    history of every company this person has been seen at, since a person
    may legitimately change companies over time and none of that history is
    ever discarded.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    canonical_name: str
    canonical_company_id: str | None = None
    associated_company_ids: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    linkedin_id: str | None = None
    provider_identities: dict[str, str] = Field(default_factory=dict)


class PersonResolutionResult(BaseModel):
    """One candidate's auditable resolution decision.

    Mirrors Phase 7's ResolutionResult exactly: canonical_person_id is
    populated for MATCH (the existing person) and NEW (the freshly minted
    one) — never for UNRESOLVED. matched_person_id is a separate, narrower
    field that may additionally appear on an UNRESOLVED result as an audit
    hint, without ever committing to it.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    status: ResolutionStatus
    canonical_person_id: str | None = None
    matched_person_id: str | None = None
    confidence: str | None = None
    matched_signals: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    reason_code: PersonResolutionReasonCode
    explanation: str


class PersonResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    people_discovery_run_id: str = Field(min_length=1)


class PersonResolutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    people_discovery_run_id: str
    status: str
    canonical_person_id: str | None
    matched_person_id: str | None
    confidence: str | None
    matched_signals: list[str]
    conflicting_signals: list[str]
    reason_code: str
    explanation: str
    resolved_at: datetime


class CanonicalPersonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    canonical_name: str
    canonical_company_id: str | None
    associated_company_ids: list[str]
    aliases: list[str]
    linkedin_id: str | None
    provider_identities: dict[str, str]
    created_at: datetime
    updated_at: datetime
