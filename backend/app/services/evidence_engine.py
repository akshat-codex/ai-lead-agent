"""Phase 11 — Evidence Engine aggregation logic.

Pure and DB-free: given a list of EvidenceRecord objects already in memory,
this module groups them, detects agreement/conflict, and computes
completeness. Persistence (app/models/evidence.py, app/api/evidence.py) and
the functions that turn existing Phase 6-10 records into EvidenceRecords
(app/services/evidence_import.py) live elsewhere — this module never
touches a database and never calls a provider.

Status derivation, per entity+field:
  - UNKNOWN               — no evidence records exist at all.
  - CONFLICT              — two or more records disagree on the value
                            (compared via _comparison_key, which never
                            broadens or narrows meaning — see its
                            docstring).
  - SUPPORTED             — every record agrees, AND either (a) two or
                            more independent records corroborate it, or
                            (b) a single record states a MEDIUM/HIGH
                            confidence. Multi-source agreement is itself a
                            meaningful signal even when no source states a
                            numeric confidence.
  - SUPPORTED_STRUCTURED  — exactly one record, honestly UNKNOWN
                            confidence, but from a provider this codebase
                            has explicitly named (_TRUSTED_STRUCTURED_PROVIDERS)
                            as a trusted structured-data source for that
                            specific field (_TRUSTED_STRUCTURED_FIELDS).
                            This never fabricates a confidence the source
                            never gave — the record still honestly says
                            UNKNOWN — it only lets a specific, named,
                            structured field from a specific, named
                            provider gate a hard rule the same way a
                            genuinely SUPPORTED field already does. An
                            untrusted provider or an untrusted field still
                            falls straight through to INSUFFICIENT below.
  - INSUFFICIENT          — exactly one record, agreeing with nothing
                            else, whose confidence is LOW or UNKNOWN, and
                            either its field or its provider is not on the
                            trusted-structured allowlist — a single,
                            unconfirmed data point that is not yet
                            corroborated.

This mirrors docs/quality-contract.md's "evidence completeness" KPI and
docs/evidence-policy.md's minimum-evidence-bar concept, adapted to what
this project's mock-based providers can actually supply today (almost
always an UNKNOWN confidence) without pretending otherwise.

Per-field freshness (market-standard "last verified" pattern, e.g. Clay/
Clearbit's per-field staleness indicators): summarize_field/summarize_entity
also attach a `freshness_score`/`is_stale` to each FieldEvidenceSummary (see
_field_freshness_score below), reusing app/schemas/scoring.py's own
FreshnessConfig decay curve — the same one app/services/lead_scoring.py's
whole-lead freshness_score already applies, just scoped to one field's own
newest record instead of an entire entity's newest evidence overall.
Deliberately additive, never folded into the status derivation above: an
old field's SUPPORTED/SUPPORTED_STRUCTURED/INSUFFICIENT status must never
silently flip to something else purely because time passed with no new
evidence, since app/services/hard_rule_engine.py reads that status to gate
PASS/HOLD — decaying it directly would mean a lead could flip from PASS to
HOLD with no new conflicting data at all, for a reason no reviewer could
see in the evidence itself. A caller that wants staleness to actually
affect a decision reads freshness_score/is_stale explicitly, the same
opt-in way lead_scoring.py's own freshness_score already works.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.schemas.evidence import (
    ConfidenceLevel,
    EntityEvidenceSummary,
    EntityType,
    EvidenceRecord,
    EvidenceStatus,
    FieldEvidenceSummary,
)
from app.schemas.scoring import DEFAULT_FRESHNESS_CONFIG, FreshnessConfig
from app.services.icp_normalization import clean_text, resolve_geography_alias

# The critical fields per entity type, per the Phase 11 task's own list —
# these drive completeness and ensure a field with zero evidence still
# shows up (as UNKNOWN) rather than silently disappearing.
COMPANY_CRITICAL_FIELDS: tuple[str, ...] = (
    "company_identity",
    "domain",
    "industry",
    "employee_range",
    "country",
    "company_type",
    "business_model",
    "linkedin_id",
    # Phase 7P: revenue_range has been a real, persisted evidence field
    # since Phase 7G (Explorium's yearly_revenue_range, mapped by
    # evidence_import.py) but was never listed here — it still contributed
    # to lead_scoring.py's evidence_score whenever a record for it
    # happened to exist (summarize_entity's field union is not limited to
    # this list), but a company with no revenue evidence at all had no
    # visible UNKNOWN entry the way domain/industry/employee_range already
    # do. Adding it here makes that gap visible and auditable, consistent
    # with every other field a real discovery provider may supply.
    "revenue_range",
)
PERSON_CRITICAL_FIELDS: tuple[str, ...] = (
    "person_identity",
    "current_title",
    "company_association",
    "linkedin_id",
)

_GEOGRAPHY_FIELDS = frozenset({"country", "geography"})

# Providers this codebase has explicitly, deliberately named as trusted
# structured-data sources — a small, auditable allowlist maintained here
# by hand, never a registry/reliability-profile lookup (Phase 5's
# ProviderReliabilityProfile.accuracy exists but is unset on the real
# adapter and unread here; keeping this as a plain constant is the
# smallest, most visible mechanism, and adding a second real structured
# provider later is a one-line addition). Explorium's own structured
# discovery fields (industry, country — see
# app/providers/explorium.py's _BUSINESS_ATTRIBUTE_MAP) are the reason
# this exists: Explorium states them directly, with no vendor-supplied
# confidence number, and a single honest UNKNOWN-confidence record from a
# provider NOT on this list must still fall through to INSUFFICIENT below
# — this allowlist is what draws that line, not confidence level alone.
_TRUSTED_STRUCTURED_PROVIDERS = frozenset({"explorium-company-discovery-v1"})

# Only these fields — the ones a real hard rule actually gates on
# (industry, geography, employee size, company type) — are eligible for
# SUPPORTED_STRUCTURED. Every other field (domain, ...) still requires
# real corroboration or a stated confidence, even from a trusted
# provider: this is a deliberate per-field decision, not "trust
# everything this provider ever says."
#
# P1 fix (company_type trust): company_type was excluded here at first
# because Explorium's /businesses response has no field that states a
# company's type at all (see app/providers/explorium.py's own
# _BUSINESS_ATTRIBUTE_MAP — unlike naics_description -> industry, there is
# no company_type key), so nothing could ever be written as company_type
# evidence, and the hard rule (app/services/hard_rule_engine.py's
# allowed_titles/company_type _evaluate_choice_field) could NEVER resolve
# from Explorium-only evidence, always falling through to
# COMPANY_TYPE_UNKNOWN/HOLD regardless of what Explorium actually
# classified the company under. Adding it here does NOT fabricate a
# value: app/services/evidence_import.py::_company_type_structured_match
# only ever writes company_type evidence from a real, live-verified
# linkedin_category/naics_category taxonomy match — the exact same
# server-side classification guarantee industry's structured branch
# already relies on — and only when the match is unambiguous (exactly one
# ICP term resolved into that branch; see that function's own OR-list
# confound guard, mirroring hard_icp_validation.py's
# _bridged_industry_terms discipline). The low-precision
# website_keywords fallback tier is never eligible here; it still writes
# no company_type evidence at all, exactly as before this fix.
#
# Phase 11 fix (employee_range trust): employee_range was deliberately
# excluded here at first, but Explorium NEVER supplies a precise
# employee_count (only a documented bucket string, e.g. "51-200" — see
# app/providers/explorium.py's module docstring) — so excluding
# employee_range meant a single, genuinely structured Explorium sighting
# of it could never even reach SUPPORTED_STRUCTURED, and the employee_range
# hard rule (app/services/hard_rule_engine.py::_evaluate_employee_range)
# could then NEVER resolve from Explorium-only evidence, always falling
# through to EMPLOYEE_COUNT_UNKNOWN/HOLD regardless of what Explorium
# actually returned (confirmed by a controlled discovery-quality
# evaluation run against realistic mocked data: 0/11 candidates reached
# PASS across 4 ICP scenarios, entirely because of this gap). Adding it
# here does NOT fabricate a count: employee_range is still only ever
# populated from a genuine attributes["employee_range"] value Explorium's
# own response actually stated (see app/services/evidence_import.py), the
# hard rule still only ever does OVERLAP comparison on the bucket (never a
# guessed exact count), and an exact employee_count still takes precedence
# over the bucket whenever one is available (unchanged, see
# _evaluate_employee_range's own docstring) — this only lets the
# already-existing bucket-overlap logic actually run instead of being
# starved of eligible evidence.
_TRUSTED_STRUCTURED_FIELDS = frozenset({"industry", "country", "employee_range", "company_type"})


def critical_fields_for(entity_type: EntityType) -> tuple[str, ...]:
    return COMPANY_CRITICAL_FIELDS if entity_type == EntityType.COMPANY else PERSON_CRITICAL_FIELDS


def _comparison_key(field: str, value: Any) -> str:
    """A stable key for deciding whether two evidence values "agree."

    Reuses Phase 2's geography alias table (app.services.icp_normalization)
    so "US" and "United States" are recognized as the same real-world value
    for geography-like fields — the existing normalization utility this
    kind of comparison calls for — without ever broadening a value into a
    region or narrowing a region into one country, since
    resolve_geography_alias never does either. The stored records
    themselves are never altered by this — only the derived status is.
    """
    if isinstance(value, str):
        if field in _GEOGRAPHY_FIELDS:
            resolved = resolve_geography_alias(value)
            if resolved:
                return f"geo:{resolved[0]}"
        return clean_text(value).lower()
    return json.dumps(value, sort_keys=True, default=str)


def group_by_field(records: list[EvidenceRecord]) -> dict[str, list[EvidenceRecord]]:
    grouped: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.field].append(record)
    return dict(grouped)


def compute_field_status(field: str, records: list[EvidenceRecord]) -> EvidenceStatus:
    if not records:
        return EvidenceStatus.UNKNOWN

    distinct_values = {_comparison_key(field, record.value) for record in records}
    if len(distinct_values) > 1:
        return EvidenceStatus.CONFLICT

    if len(records) >= 2:
        return EvidenceStatus.SUPPORTED  # independent corroboration

    if records[0].confidence in (ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH):
        return EvidenceStatus.SUPPORTED

    if field in _TRUSTED_STRUCTURED_FIELDS and records[0].source_provider_id in _TRUSTED_STRUCTURED_PROVIDERS:
        return EvidenceStatus.SUPPORTED_STRUCTURED

    return EvidenceStatus.INSUFFICIENT


def _as_naive_utc(value: datetime) -> datetime:
    """Matches app/services/lead_scoring.py's own _as_naive_utc exactly —
    evidence timestamps may come back timezone-naive after a SQLite
    round-trip while `now` is typically aware; both are compared as naive
    UTC so neither source needs to guess the other's tzinfo convention."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _field_freshness_score(
    records: list[EvidenceRecord],
    now: datetime,
    config: FreshnessConfig,
) -> float | None:
    """Per-field counterpart of app/services/lead_scoring.py's
    _compute_freshness_score, reusing the exact same FreshnessConfig decay
    curve — full credit within full_credit_within_days, linear decay to
    zero by zero_credit_after_days — applied to THIS field's own newest
    record instead of an entire entity's newest evidence across every
    field. None (never a fabricated number) when the field has no records
    at all, mirroring that function's own convention for "nothing to
    date." This is purely additive observability: it never changes
    compute_field_status's own SUPPORTED/CONFLICT/etc. derivation (see
    that function's own docstring for why status must never flip from
    elapsed time alone) — a caller that wants staleness to affect a
    decision (e.g. a future verification trigger) reads this score
    explicitly, the same opt-in way lead_scoring.py's whole-lead
    freshness_score already works.
    """
    if not records:
        return None
    most_recent = max(_as_naive_utc(r.retrieved_at) for r in records)
    age_days = max(0.0, (_as_naive_utc(now) - most_recent).total_seconds() / 86400.0)
    if age_days <= config.full_credit_within_days:
        return 100.0
    if age_days >= config.zero_credit_after_days:
        return 0.0
    span = config.zero_credit_after_days - config.full_credit_within_days
    return max(0.0, min(100.0, 100.0 * (1 - (age_days - config.full_credit_within_days) / span)))


def summarize_field(
    field: str,
    records: list[EvidenceRecord],
    now: datetime | None = None,
    freshness_config: FreshnessConfig = DEFAULT_FRESHNESS_CONFIG,
) -> FieldEvidenceSummary:
    freshness_score = _field_freshness_score(records, now or datetime.now(timezone.utc), freshness_config)
    return FieldEvidenceSummary(
        field=field,
        status=compute_field_status(field, records),
        records=tuple(records),
        freshness_score=freshness_score,
        is_stale=freshness_score is not None and freshness_score <= 0.0,
    )


def summarize_entity(
    entity_type: EntityType,
    entity_id: str,
    records: list[EvidenceRecord],
    now: datetime | None = None,
    freshness_config: FreshnessConfig = DEFAULT_FRESHNESS_CONFIG,
) -> EntityEvidenceSummary:
    """Builds the complete evidence picture for one entity: every field
    that has at least one record, plus every critical field for this
    entity type even when it has none (shown as UNKNOWN)."""
    grouped = group_by_field(records)
    critical_fields = critical_fields_for(entity_type)
    all_fields = sorted(set(grouped.keys()) | set(critical_fields))

    resolved_now = now or datetime.now(timezone.utc)
    field_summaries = tuple(
        summarize_field(field, grouped.get(field, []), now=resolved_now, freshness_config=freshness_config)
        for field in all_fields
    )

    covered = sum(
        1
        for field in critical_fields
        if compute_field_status(field, grouped.get(field, [])) != EvidenceStatus.UNKNOWN
    )
    completeness = covered / len(critical_fields) if critical_fields else 0.0

    return EntityEvidenceSummary(
        entity_type=entity_type,
        entity_id=entity_id,
        fields=field_summaries,
        completeness=completeness,
    )
