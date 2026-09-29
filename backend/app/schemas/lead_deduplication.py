"""Phase 19 — Lead Deduplication contracts.

A "lead" here is nothing more than the pairing of an already-resolved
Phase 7 canonical company and an already-resolved Phase 10 canonical
person (or a company alone, when no person is involved yet). This module
invents no new identity signal and no new matching heuristic: company/
person sameness was already decided, conservatively, by Phase 7/10's own
resolve_candidate() — deduplication here is just the deterministic
observation "this (company_id, person_id) pair has been seen before."

Because Phase 7/10 already refuse to merge on a weak signal (returning
UNRESOLVED with no canonical id), an ambiguous underlying identity can
never even reach this module with a usable company_id/person_id — so this
layer cannot "invent" a merge Phase 7/10 wouldn't already stand behind.
The one genuinely new decision this module makes is entity-pairing, not
entity-identity: the same company with a different (or absent) person is a
different lead, and the same person at a materially different company
association is a different lead unless existing identity evidence (the
person's own associated_company_ids, Phase 10) already proves otherwise.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeadDeduplicationDecision(str, Enum):
    MATCHED_EXISTING_LEAD = "MATCHED_EXISTING_LEAD"
    NEW_LEAD = "NEW_LEAD"
    UNRESOLVED = "UNRESOLVED"


class LeadDeduplicationReasonCode(str, Enum):
    EXACT_COMPANY_PERSON_MATCH = "EXACT_COMPANY_PERSON_MATCH"
    EXACT_COMPANY_ONLY_MATCH = "EXACT_COMPANY_ONLY_MATCH"
    NEW_COMPANY_PERSON_PAIR = "NEW_COMPANY_PERSON_PAIR"
    NEW_COMPANY_ONLY = "NEW_COMPANY_ONLY"
    PERSON_NOT_CURRENTLY_ASSOCIATED_WITH_COMPANY = "PERSON_NOT_CURRENTLY_ASSOCIATED_WITH_COMPANY"
    COMPANY_IDENTITY_UNRESOLVED = "COMPANY_IDENTITY_UNRESOLVED"
    PERSON_IDENTITY_UNRESOLVED = "PERSON_IDENTITY_UNRESOLVED"


class ExistingLead(BaseModel):
    """The identity-relevant slice of an already-canonicalized lead —
    everything deduplicate_lead() needs to compare against. Deliberately
    excludes any scoring/qualification field; this module only ever
    reasons about company+person pairing."""

    model_config = ConfigDict(frozen=True)

    id: str
    company_id: str
    person_id: str | None = None


class LeadSourceReference(BaseModel):
    """Provenance for the candidate(s) that produced this deduplication
    attempt — the original discovery/resolution records are never deleted
    or modified; this is purely a pointer back to them."""

    model_config = ConfigDict(frozen=True)

    company_candidate_id: str | None = None
    company_resolution_id: str | None = None
    person_candidate_id: str | None = None
    person_resolution_id: str | None = None


class LeadDeduplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    company_id: str | None = Field(default=None, min_length=1)
    person_id: str | None = Field(default=None, min_length=1)
    source: LeadSourceReference = Field(default_factory=LeadSourceReference)

    @model_validator(mode="after")
    def _at_least_one_id(self) -> "LeadDeduplicationRequest":
        if self.company_id is None and self.person_id is None:
            raise ValueError("at least one of company_id or person_id is required")
        return self


class LeadDeduplicationResult(BaseModel):
    """The full, in-memory result of one deduplication attempt. Always
    produced, even for UNRESOLVED — decision distinguishes a genuine
    pairing judgment from an identity that isn't resolvable yet."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    company_id: str | None
    person_id: str | None

    decision: LeadDeduplicationDecision
    lead_id: str | None
    confidence: str | None
    matched_signals: tuple[str, ...]
    reason_code: LeadDeduplicationReasonCode
    explanation: str

    source: LeadSourceReference


class LeadDeduplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str | None
    person_id: str | None
    decision: str
    lead_id: str | None
    confidence: str | None
    matched_signals: list[str]
    reason_code: str
    explanation: str
    company_candidate_id: str | None
    company_resolution_id: str | None
    person_candidate_id: str | None
    person_resolution_id: str | None
    created_at: datetime


class CanonicalLeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    person_id: str | None
    created_at: datetime


class LeadIcpMembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    icp_id: str
    icp_version: int
    first_seen_at: datetime
