"""Phase 23 — Lead Export & Output Contract.

Export reads what Phases 1-22 already produced and reshapes it into a
stable, versioned output row — it never scores, qualifies, resolves
identity, deduplicates, ranks, or reviews anything itself. Every field
here is either copied from an already-persisted row or explicitly left
None/UNKNOWN when that pipeline stage never ran; nothing is guessed or
backfilled to make an export row look more complete than the underlying
data actually is.

SCHEMA_VERSION exists specifically so a future consumer (a company-website
integration, explicitly out of scope for this phase) can detect a shape
change without guessing — see ExportMetadata. Bump it whenever a field is
added, removed, or its meaning changes; never reuse an old version number
for a changed shape.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1.0.0"


class ExportFormat(str, Enum):
    JSON = "JSON"
    CSV = "CSV"


class ExportIdentity(BaseModel):
    """Identity fields copied verbatim from Phase 7/10's canonical rows —
    never re-resolved, never re-derived. A None field here means that
    canonical attribute was never populated, not that it doesn't apply."""

    model_config = ConfigDict(frozen=True)

    company_id: str
    company_name: str | None
    company_domain: str | None
    person_id: str | None
    person_name: str | None
    person_title: str | None
    person_linkedin_id: str | None


class ExportEvidenceSummary(BaseModel):
    """A compact, evidence-backed view of company/person attributes —
    populated ONLY from fields whose Phase 11 status is SUPPORTED (the
    same bar Phase 12's hard validation itself requires); anything
    CONFLICT/INSUFFICIENT/UNKNOWN is represented by the field being absent
    here, never guessed at or filled with an unconfirmed value."""

    model_config = ConfigDict(frozen=True)

    verified_fields: dict[str, str]
    conflicting_fields: tuple[str, ...]
    missing_critical_fields: tuple[str, ...]
    evidence_ids: tuple[str, ...]


class ExportScoreComponents(BaseModel):
    model_config = ConfigDict(frozen=True)

    final_score: float | None
    icp_score: float | None
    commercial_score: float | None
    evidence_score: float | None
    freshness_score: float | None
    identity_confidence: float | None


class ExportProvenance(BaseModel):
    """Pointers back to the exact rows an export row was built from — an
    auditor can follow every one of these ids back to its own append-only
    history table; nothing here is a copy that could drift from the
    source of truth."""

    model_config = ConfigDict(frozen=True)

    hard_validation_id: str | None
    score_id: str | None
    qualification_id: str | None
    adversarial_review_id: str | None
    verification_ids: tuple[str, ...]
    human_review_id: str | None
    deduplication_ids: tuple[str, ...]
    company_resolution_id: str | None
    person_resolution_id: str | None
    source_provider_ids: tuple[str, ...]


class ExportedLead(BaseModel):
    """One lead's complete, stable export row. Every optional field is
    None/empty precisely when that pipeline stage has not produced a
    usable result for this (icp_id, company_id, person_id) — never
    fabricated to look complete."""

    model_config = ConfigDict(frozen=True)

    schema_version: str
    icp_id: str
    icp_version: int
    batch_id: str | None
    lead_id: str

    identity: ExportIdentity
    evidence: ExportEvidenceSummary

    hard_rule_result: str | None
    hard_rule_reason_codes: tuple[str, ...]

    scores: ExportScoreComponents

    qualification_decision: str | None
    qualification_confidence: float | None
    qualification_summary: str | None

    adversarial_result: str | None
    adversarial_confidence: float | None

    verification_status: str | None

    human_review_decision: str | None
    human_review_reason_codes: tuple[str, ...]

    rank: int | None
    tier: str | None
    ranking_reason_codes: tuple[str, ...]

    is_duplicate_occurrence: bool

    provenance: ExportProvenance

    exported_at: datetime


class ExportMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str
    icp_id: str
    icp_version: int
    batch_id: str | None
    lead_count: int
    generated_at: datetime


class ExportResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: ExportMetadata
    leads: tuple[ExportedLead, ...]


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str
    batch_id: str | None = None
    format: ExportFormat = ExportFormat.JSON
