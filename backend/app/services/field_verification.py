"""Phase 18 — multi-source verification.

Escalates ONLY a field whose Phase 11 status is CONFLICT or INSUFFICIENT
(compute_field_status(), unchanged) by calling enrichment providers that
have not already contributed to that field's existing evidence — same-
provider duplicates are structurally excluded from being "new" evidence
here, so a second call to the provider that already produced one of the
conflicting values can never masquerade as independent corroboration.

Reuses, unchanged:
  * app/providers/registry.py + app/providers/base.py (Phase 5) for every
    provider call — no second provider abstraction.
  * app/services/evidence_engine.py's compute_field_status() (Phase 11) to
    both read the original status and re-derive the resulting status from
    the combined (old + new) evidence.
  * app/schemas/company_enrichment.CompanyEnrichmentQuery (Phase 8) for the
    query shape sent to COMPANY_ENRICHMENT providers.

This module never calls Phase 3/12's hard-rule engine and never produces a
PASS/FAIL/HOLD hard-rule verdict — it only ever adds evidence and reports
whether that evidence resolved the original conflict/insufficiency.
Nothing here reads or requires the Phase 16/17 LLM infrastructure: matching
new provider output against old evidence is exactly the same deterministic
comparison Phase 11 already performs, so introducing an LLM here would
duplicate that logic rather than reuse it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
from app.schemas.company_enrichment import CompanyEnrichmentQuery
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceCreate, EvidenceRecord, EvidenceStatus, SourceType
from app.schemas.verification import (
    ProviderAttempt,
    VerificationExecutionStatus,
    VerificationOutcome,
    VerificationResult,
    VerificationTrigger,
)
from app.services.evidence_engine import compute_field_status

_CAPABILITY_BY_ENTITY_TYPE = {
    EntityType.COMPANY: ProviderCapability.COMPANY_ENRICHMENT,
    EntityType.PERSON: ProviderCapability.PERSON_ENRICHMENT,
}

_TRIGGER_BY_STATUS = {
    EvidenceStatus.CONFLICT: VerificationTrigger.CONFLICT,
    EvidenceStatus.INSUFFICIENT: VerificationTrigger.INSUFFICIENT,
}


def _confidence_from_float(value: float | None) -> ConfidenceLevel:
    """Identical bucketing to app/services/evidence_import.py — a provider
    supplying no confidence is UNKNOWN, never invented."""
    if value is None:
        return ConfidenceLevel.UNKNOWN
    if value >= 0.8:
        return ConfidenceLevel.HIGH
    if value >= 0.5:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def _existing_provider_ids(records: list[EvidenceRecord]) -> frozenset[str]:
    return frozenset(r.source_provider_id for r in records if r.source_provider_id)


def _build_query(
    entity_type: EntityType,
    entity_name: str | None,
    domain: str | None,
    external_id: str | None,
) -> dict:
    if entity_type == EntityType.COMPANY:
        return CompanyEnrichmentQuery(domain=domain, company_name=entity_name, external_id=external_id).model_dump()
    # PERSON_ENRICHMENT has no dedicated query schema yet (no phase has
    # defined one — no current provider implements the capability at all);
    # a plain dict keeps this forward-compatible without inventing a
    # contract Phase 9/10 never established.
    return {"person_name": entity_name, "external_id": external_id}


def verify_field(
    icp_id: str,
    icp_version: int,
    entity_type: EntityType,
    entity_id: str,
    field: str,
    existing_records: list[EvidenceRecord],
    registry: ProviderRegistry,
    entity_name: str | None = None,
    domain: str | None = None,
    provider_identities: dict[str, str] | None = None,
) -> VerificationResult:
    """Pure orchestration except for the provider calls themselves (which
    go through the unchanged Phase 5 `provider.run()` — never raises, and
    every failure is already converted into a clean ProviderResponse). The
    caller (app/api/field_verification.py) owns persistence.
    """
    provider_identities = provider_identities or {}
    field_records = [r for r in existing_records if r.field == field]
    original_status = compute_field_status(field, field_records)
    original_evidence_ids = tuple(r.id for r in field_records)

    if original_status not in _TRIGGER_BY_STATUS:
        return VerificationResult(
            icp_id=icp_id,
            icp_version=icp_version,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            trigger=None,
            execution_status=VerificationExecutionStatus.NOT_TRIGGERED,
            outcome=None,
            original_status=original_status,
            original_evidence_ids=original_evidence_ids,
            resulting_status=original_status,
            new_evidence_ids=(),
            all_evidence_ids=original_evidence_ids,
            providers_consulted=(),
            explanation=f"Field status is already {original_status.value}; verification is only triggered by CONFLICT or INSUFFICIENT.",
        )

    trigger = _TRIGGER_BY_STATUS[original_status]
    capability = _CAPABILITY_BY_ENTITY_TYPE[entity_type]
    already_consulted = _existing_provider_ids(field_records)
    candidate_providers = [
        p for p in registry.find_by_capability(capability) if p.provider_id not in already_consulted
    ]

    if not candidate_providers:
        return VerificationResult(
            icp_id=icp_id,
            icp_version=icp_version,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            trigger=trigger,
            execution_status=VerificationExecutionStatus.NO_INDEPENDENT_PROVIDERS,
            outcome=VerificationOutcome.HOLD,
            original_status=original_status,
            original_evidence_ids=original_evidence_ids,
            resulting_status=original_status,
            new_evidence_ids=(),
            all_evidence_ids=original_evidence_ids,
            providers_consulted=(),
            explanation=(
                "No enrichment provider independent of the ones already consulted for this field is "
                "available; the existing conflict/insufficiency cannot be safely investigated further."
            ),
        )

    new_records: list[EvidenceRecord] = []
    attempts: list[ProviderAttempt] = []

    for provider in candidate_providers:
        query = _build_query(
            entity_type,
            entity_name=entity_name,
            domain=domain,
            external_id=provider_identities.get(provider.provider_id),
        )
        response = provider.run(ProviderRequest(capability=capability, query=query))

        if not response.success:
            attempts.append(
                ProviderAttempt(
                    provider_id=provider.provider_id,
                    success=False,
                    error_code=response.error.code if response.error else None,
                    error_message=response.error.message if response.error else None,
                )
            )
            continue

        retrieved_at = response.source.retrieved_at if response.source else datetime.now(timezone.utc)
        provider_new_ids: list[str] = []
        for record in response.data:
            if field not in record.attributes:
                continue
            new_id = str(uuid4())
            new_records.append(
                EvidenceRecord(
                    id=new_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field=field,
                    value=record.attributes[field],
                    source_provider_id=provider.provider_id,
                    source_type=SourceType.PROVIDER,
                    external_id=record.external_id,
                    retrieved_at=retrieved_at,
                    confidence=_confidence_from_float(None),
                    created_at=datetime.now(timezone.utc),
                )
            )
            provider_new_ids.append(new_id)

        attempts.append(ProviderAttempt(provider_id=provider.provider_id, success=True, new_evidence_ids=tuple(provider_new_ids)))

    if not new_records:
        return VerificationResult(
            icp_id=icp_id,
            icp_version=icp_version,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            trigger=trigger,
            execution_status=VerificationExecutionStatus.ALL_PROVIDERS_FAILED,
            outcome=VerificationOutcome.HOLD,
            original_status=original_status,
            original_evidence_ids=original_evidence_ids,
            resulting_status=original_status,
            new_evidence_ids=(),
            all_evidence_ids=original_evidence_ids,
            providers_consulted=tuple(attempts),
            explanation="Every independent provider call failed or returned nothing for this field; the conflict/insufficiency remains unresolved.",
        )

    combined_records = [*field_records, *new_records]
    resulting_status = compute_field_status(field, combined_records)
    all_evidence_ids = tuple(r.id for r in combined_records)

    if resulting_status == EvidenceStatus.SUPPORTED:
        outcome = VerificationOutcome.RESOLVED
        explanation = "Additional independent evidence now supports a single agreed value; the original conflict/insufficiency is resolved."
    elif resulting_status == EvidenceStatus.CONFLICT:
        outcome = VerificationOutcome.HOLD
        explanation = "New independent evidence was gathered but sources still disagree; the conflict remains unresolved and must not be guessed away."
    else:  # still INSUFFICIENT (or, defensively, UNKNOWN)
        outcome = VerificationOutcome.UNRESOLVED
        explanation = "New evidence was gathered but is still not enough to reach a supported value."

    return VerificationResult(
        icp_id=icp_id,
        icp_version=icp_version,
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        trigger=trigger,
        execution_status=VerificationExecutionStatus.SUCCESS,
        outcome=outcome,
        original_status=original_status,
        original_evidence_ids=original_evidence_ids,
        resulting_status=resulting_status,
        new_evidence_ids=tuple(r.id for r in new_records),
        all_evidence_ids=all_evidence_ids,
        new_evidence_records=tuple(new_records),
        providers_consulted=tuple(attempts),
        explanation=explanation,
    )
