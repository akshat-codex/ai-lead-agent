"""Phase 18 — Multi-Source Verification contracts.

A conservative escalation: when Phase 11's own status for one entity+field
is CONFLICT or INSUFFICIENT, this layer calls whatever enrichment providers
have NOT already contributed evidence for that field, and re-evaluates
status with the combined (old + new) evidence — using Phase 11's own
compute_field_status() unchanged. It never invents a resolution and never
silently prefers one existing source over another: RESOLVED only happens
when the combined evidence itself reaches SUPPORTED.

Nothing here duplicates Phase 3/12's hard-rule authority. Verification
never touches a HardRuleEvaluation and never marks any ICP rule PASS/FAIL —
it only adds evidence a later call to Phase 12's validate_against_icp() may
use. The `icp_id`/`icp_version` recorded here are provenance ("this
verification was triggered while evaluating this ICP"), not a rule result.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus


class VerificationTrigger(str, Enum):
    """Why verification was worth attempting at all — mirrors Phase 11's
    own EvidenceStatus values that justify escalation; SUPPORTED/UNKNOWN
    never trigger verification (see app/services/field_verification.py)."""

    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"


class VerificationOutcome(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    HOLD = "HOLD"


class VerificationExecutionStatus(str, Enum):
    """How the verification attempt actually went — kept separate from
    `outcome` so "we tried and still couldn't resolve it" (a genuine
    UNRESOLVED/HOLD outcome) is never confused with "we couldn't even
    attempt it" (no providers, or every provider failed)."""

    SUCCESS = "SUCCESS"  # at least one new, independent provider call succeeded
    NOT_TRIGGERED = "NOT_TRIGGERED"  # the field was already SUPPORTED/UNKNOWN - nothing to verify
    NO_INDEPENDENT_PROVIDERS = "NO_INDEPENDENT_PROVIDERS"  # every capable provider already contributed to this field
    ALL_PROVIDERS_FAILED = "ALL_PROVIDERS_FAILED"  # at least one independent provider existed, but every call failed


class ProviderAttempt(BaseModel):
    """One provider's contribution (or failure) during a verification
    attempt — recorded regardless of outcome, so a failed call is never
    silently dropped from the audit trail."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    success: bool
    new_evidence_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str = Field(min_length=1)
    entity_type: EntityType
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)


class VerificationResult(BaseModel):
    """The full, in-memory result of one verification attempt. Always
    produced, even when nothing could be attempted — status distinguishes
    a genuine RESOLVED/UNRESOLVED/HOLD judgment from an execution failure,
    exactly like Phase 16/17's status/decision separation."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    entity_type: EntityType
    entity_id: str
    field: str

    trigger: VerificationTrigger | None
    execution_status: VerificationExecutionStatus
    outcome: VerificationOutcome | None

    original_status: EvidenceStatus
    original_evidence_ids: tuple[str, ...]
    resulting_status: EvidenceStatus
    new_evidence_ids: tuple[str, ...]
    all_evidence_ids: tuple[str, ...]

    # The concrete new evidence records this attempt produced (empty
    # whenever new_evidence_ids is empty) — carried here, not just as ids,
    # so the caller (app/api/field_verification.py) can persist them via
    # the exact Phase 11 EvidenceModel pattern without a second round of
    # provider calls or any re-derivation.
    new_evidence_records: tuple[EvidenceRecord, ...] = ()

    providers_consulted: tuple[ProviderAttempt, ...]
    explanation: str


class VerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    entity_type: str
    entity_id: str
    field: str
    trigger: str | None
    execution_status: str
    outcome: str | None
    original_status: str
    original_evidence_ids: list[str]
    resulting_status: str
    new_evidence_ids: list[str]
    all_evidence_ids: list[str]
    providers_consulted: list[dict]
    explanation: str
    created_at: datetime
