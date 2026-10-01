"""Phase 11 — Evidence Engine contracts.

A generic, entity-agnostic provenance layer over facts already collected by
earlier phases (Phase 6/8 discovery+enrichment for companies, Phase 9/10
discovery+resolution for people). This module answers "what do we know,
where from, when, how confidently" — never "is this a good lead." Nothing
here performs qualification, scoring, or business-model classification.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EntityType(str, Enum):
    COMPANY = "COMPANY"
    PERSON = "PERSON"


class SourceType(str, Enum):
    """Structured, extensible source categories — not arbitrary free text.
    Every source this project currently has access to is a PROVIDER call
    (Phase 5's abstraction); the other categories exist so a future real
    source (a company's own site, a news mention, ...) has somewhere to go
    without inventing a new field."""

    PROVIDER = "provider"
    OFFICIAL_COMPANY_SITE = "official_company_site"
    LINKEDIN = "linkedin"
    SEARCH = "search"
    NEWS = "news"
    FUNDING = "funding"
    HIRING = "hiring"
    DIRECTORY = "directory"
    OTHER = "other"


class ConfidenceLevel(str, Enum):
    """A structured confidence representation, per docs/evidence-policy.md.
    UNKNOWN is not a weak guess at confidence — it is the honest state when
    the underlying source did not supply one; nothing here ever invents a
    precise number the source never gave."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceStatus(str, Enum):
    """The field-level status app/services/evidence_engine.py derives from
    a field's evidence records. Deliberately excludes anything resembling
    lead acceptance (no ACCEPTED_LEAD/QUALIFIED here) — see
    docs/lead-decision-policy.md for where that belongs instead.

    SUPPORTED_STRUCTURED is distinct from SUPPORTED: it means exactly one
    record, honestly ConfidenceLevel.UNKNOWN (the source never stated a
    confidence), from a provider this codebase has explicitly named as a
    trusted structured-data source for a specific field (see
    evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS/_TRUSTED_STRUCTURED_FIELDS).
    It never claims real corroboration or a stated confidence that doesn't
    exist — it only lets a caller (Phase 12's hard-rule engine) treat this
    specific, named, structured source's own field as eligible to gate a
    hard rule, the same way a genuinely SUPPORTED field already is."""

    SUPPORTED = "SUPPORTED"
    SUPPORTED_STRUCTURED = "SUPPORTED_STRUCTURED"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    UNKNOWN = "UNKNOWN"


class EvidenceRecord(BaseModel):
    """One immutable, append-only observation of a single field's value.

    Never overwritten and never merged with another record — corroborating
    or conflicting records for the same entity+field simply accumulate; see
    app/services/evidence_engine.py for how status is derived from them
    without ever discarding one in favor of another.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    entity_type: EntityType
    entity_id: str
    field: str
    value: Any
    source_provider_id: str | None = None
    source_type: SourceType
    source_url: str | None = None
    external_id: str | None = None
    retrieved_at: datetime
    evidence_text: str | None = None
    confidence: ConfidenceLevel = ConfidenceLevel.UNKNOWN
    created_at: datetime


class EvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: EntityType
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: Any
    source_provider_id: str | None = None
    source_type: SourceType
    source_url: str | None = None
    external_id: str | None = None
    retrieved_at: datetime
    evidence_text: str | None = None
    confidence: ConfidenceLevel = ConfidenceLevel.UNKNOWN


class FieldEvidenceSummary(BaseModel):
    """Every record gathered for one field, plus the derived status. Never
    picks a single "true" value — records is the full, unfiltered set.

    freshness_score/is_stale are additive, per-field staleness signals
    (see app/services/evidence_engine.py::_field_freshness_score) — they
    reuse app/schemas/scoring.py's own FreshnessConfig decay curve, the
    same one app/services/lead_scoring.py's whole-lead freshness_score
    already applies across an entity's newest evidence overall. This is
    deliberately NOT folded into `status` above: an old field's
    SUPPORTED/SUPPORTED_STRUCTURED/INSUFFICIENT status must never flip
    purely from elapsed time with no new evidence, since hard_rule_engine.py
    reads that status to gate PASS/HOLD — see this field's own field-level
    docstring in evidence_engine.py for the full rationale. Both are None
    when the field has no records at all (nothing to date), matching
    lead_scoring.py's own "None, never a fabricated number" convention."""

    model_config = ConfigDict(frozen=True)

    field: str
    status: EvidenceStatus
    records: tuple[EvidenceRecord, ...]
    freshness_score: float | None = None
    is_stale: bool = False


class EntityEvidenceSummary(BaseModel):
    """The complete evidence picture for one entity: every field seen (plus
    every critical field expected for its entity type, even if UNKNOWN),
    and a completeness score."""

    model_config = ConfigDict(frozen=True)

    entity_type: EntityType
    entity_id: str
    fields: tuple[FieldEvidenceSummary, ...]
    completeness: float
