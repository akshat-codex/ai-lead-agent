"""Phase 28 — evidence-backed lead confidence/readiness.

Pure and DB-free, mirroring app/services/review_snapshot.py's own pattern
exactly: given a PipelineSnapshot (Phase 20's unchanged builder) plus the
raw evidence records and a handful of already-loaded Phase 19/22 facts,
classify_readiness() returns a deterministic LeadConfidenceResult with no
side effects, no database access, and no re-evaluation of any earlier
phase's own result.

WHY THIS CAN NEVER BECOME A SECOND SCORING/QUALIFICATION/RANKING SYSTEM:
this module reads exactly five already-computed signals — hard_rule_result,
evidence field statuses, qualification_decision, adversarial_result, and
human_review_decision — and maps them to one of five ReadinessLevel labels
via a fixed decision table (see classify_readiness). It never computes a
score, never re-runs evaluate_hard_rules/score_lead/qualify_lead/
rank_leads, and never resolves identity or deduplicates anything.

READINESS DECISION TABLE (checked in this exact order — first match wins):
  1. No pipeline data at all (hard_rule_result is None AND no evidence
     records at all) -> UNKNOWN. This is the honest "we know nothing yet"
     state, never confused with a confirmed HOLD or a real conflict.
  2. Any critical-field evidence CONFLICT -> CONFLICTED, regardless of the
     hard-rule result. An unresolved conflict is a red flag on its own
     terms; it does not matter whether the hard gate happens to have
     passed on a different field.
  3. hard_rule_result == FAIL or HOLD -> INSUFFICIENT_EVIDENCE. A FAIL/HOLD
     lead can never be reported as VERIFIED/PARTIALLY_VERIFIED, no matter
     how much unrelated evidence, scoring, or qualification exists — this
     is the concrete, structural meaning of "confidence must never
     override a hard FAIL/HOLD."
  4. hard_rule_result == PASS and every critical field is SUPPORTED (no
     missing, no insufficient) -> VERIFIED, UNLESS a live Phase 18
     verification is itself still unresolved, in which case it drops to
     PARTIALLY_VERIFIED (an unresolved verification is itself a form of
     "not fully settled yet").
  5. hard_rule_result == PASS but one or more critical fields are missing/
     insufficient -> PARTIALLY_VERIFIED.
  6. Anything else (hard_rule_result is None but SOME evidence exists) ->
     INSUFFICIENT_EVIDENCE.

Human review and duplicate/ranking status never change the READINESS
LEVEL itself (they cannot rescue a FAIL or manufacture missing evidence)
but are surfaced as additional reason codes for context, exactly like
Phase 22's own diagnostic-vs-tier separation.
"""
from __future__ import annotations

from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.human_review import PipelineSnapshot
from app.schemas.lead_confidence import (
    LeadConfidenceResult,
    ReadinessLevel,
    ReadinessReasonCode,
    SupportingEvidenceItem,
)
from app.services.evidence_engine import critical_fields_for, summarize_entity


def _supporting_evidence(
    company_id: str,
    person_id: str | None,
    company_evidence: list[EvidenceRecord],
    person_evidence: list[EvidenceRecord],
) -> tuple[SupportingEvidenceItem, ...]:
    items: list[SupportingEvidenceItem] = []

    company_summary = summarize_entity(EntityType.COMPANY, company_id, company_evidence)
    company_critical = set(critical_fields_for(EntityType.COMPANY))
    for field_summary in company_summary.fields:
        if field_summary.field in company_critical:
            items.append(
                SupportingEvidenceItem(
                    field=field_summary.field,
                    entity_type=EntityType.COMPANY.value,
                    status=field_summary.status.value,
                    evidence_ids=tuple(r.id for r in field_summary.records),
                )
            )

    if person_id is not None:
        person_summary = summarize_entity(EntityType.PERSON, person_id, person_evidence)
        person_critical = set(critical_fields_for(EntityType.PERSON))
        for field_summary in person_summary.fields:
            if field_summary.field in person_critical:
                items.append(
                    SupportingEvidenceItem(
                        field=field_summary.field,
                        entity_type=EntityType.PERSON.value,
                        status=field_summary.status.value,
                        evidence_ids=tuple(r.id for r in field_summary.records),
                    )
                )

    items.sort(key=lambda i: (i.entity_type, i.field))
    return tuple(items)


def classify_readiness(
    icp_id: str,
    icp_version: int,
    snapshot: PipelineSnapshot,
    lead_id: str | None,
    company_evidence: list[EvidenceRecord],
    person_evidence: list[EvidenceRecord],
    human_review_decision: str | None = None,
    is_duplicate_occurrence: bool = False,
    ranking_tier: str | None = None,
    generated_at=None,
) -> LeadConfidenceResult:
    """Pure: takes the already-built Phase 20 PipelineSnapshot (never
    re-derives it) plus raw evidence (for per-field provenance detail the
    snapshot itself compresses away) and Phase 19/22 facts the caller
    already looked up. `generated_at` is an explicit parameter, never a
    wall-clock read, so identical inputs always produce an identical
    result — the same determinism discipline Phase 15's score_lead()
    established for its own `now` parameter.
    """
    supporting = _supporting_evidence(snapshot.company_id, snapshot.person_id, company_evidence, person_evidence)
    reason_codes: list[ReadinessReasonCode] = []

    has_any_evidence = bool(company_evidence) or bool(person_evidence)
    has_conflicts = bool(snapshot.evidence_conflicts)
    has_missing_critical = bool(snapshot.evidence_missing_critical_fields)

    # 1. No pipeline data at all.
    if snapshot.hard_rule_result is None and not has_any_evidence:
        reason_codes.append(ReadinessReasonCode.NO_PIPELINE_DATA)
        readiness = ReadinessLevel.UNKNOWN

    # 2. Any evidence conflict takes priority over the hard-rule result.
    elif has_conflicts:
        reason_codes.append(ReadinessReasonCode.EVIDENCE_CONFLICTS_PRESENT)
        readiness = ReadinessLevel.CONFLICTED

    # 3. A confirmed FAIL or an unresolved HOLD can never be VERIFIED/PARTIALLY_VERIFIED.
    elif snapshot.hard_rule_result == "FAIL":
        reason_codes.append(ReadinessReasonCode.HARD_RULE_FAIL)
        readiness = ReadinessLevel.INSUFFICIENT_EVIDENCE
    elif snapshot.hard_rule_result == "HOLD":
        reason_codes.append(ReadinessReasonCode.HARD_RULE_HOLD)
        readiness = ReadinessLevel.INSUFFICIENT_EVIDENCE

    # 4/5. A confirmed PASS: fully supported -> VERIFIED (unless an
    # unresolved verification is itself outstanding), else PARTIALLY_VERIFIED.
    elif snapshot.hard_rule_result == "PASS":
        reason_codes.append(ReadinessReasonCode.HARD_RULE_PASS)
        if not has_missing_critical:
            reason_codes.append(ReadinessReasonCode.CRITICAL_FIELDS_FULLY_SUPPORTED)
            if snapshot.latest_verification_outcome in ("UNRESOLVED", "HOLD"):
                reason_codes.append(ReadinessReasonCode.VERIFICATION_UNRESOLVED)
                readiness = ReadinessLevel.PARTIALLY_VERIFIED
            else:
                readiness = ReadinessLevel.VERIFIED
        else:
            reason_codes.append(ReadinessReasonCode.CRITICAL_FIELDS_MISSING)
            readiness = ReadinessLevel.PARTIALLY_VERIFIED

    # 6. hard_rule_result is None but some evidence does exist.
    else:
        reason_codes.append(ReadinessReasonCode.HARD_RULE_UNKNOWN)
        readiness = ReadinessLevel.INSUFFICIENT_EVIDENCE

    # Diagnostic-only additions — never change `readiness` itself, exactly
    # like Phase 22's own tier-vs-diagnostic-reason separation.
    if snapshot.qualification_decision is None and snapshot.hard_rule_result == "PASS":
        reason_codes.append(ReadinessReasonCode.QUALIFICATION_MISSING)
    if (
        snapshot.qualification_decision == "GOOD_FIT"
        and snapshot.adversarial_result in ("DISPROVED", "WEAKENED")
    ) or (
        snapshot.qualification_decision == "NOT_FIT" and snapshot.adversarial_result == "SURVIVES"
    ):
        reason_codes.append(ReadinessReasonCode.QUALIFICATION_ADVERSARIAL_DISAGREEMENT)
    if human_review_decision == "ACCEPT":
        reason_codes.append(ReadinessReasonCode.HUMAN_ACCEPTED)
    elif human_review_decision == "REJECT":
        reason_codes.append(ReadinessReasonCode.HUMAN_REJECTED)
    if is_duplicate_occurrence:
        reason_codes.append(ReadinessReasonCode.DUPLICATE_OCCURRENCE)

    reason_text = ", ".join(code.value for code in reason_codes)
    explanation = f"Readiness {readiness.value} — {reason_text}."

    return LeadConfidenceResult(
        icp_id=icp_id,
        icp_version=icp_version,
        company_id=snapshot.company_id,
        person_id=snapshot.person_id,
        lead_id=lead_id,
        readiness=readiness,
        reason_codes=tuple(reason_codes),
        hard_rule_result=snapshot.hard_rule_result,
        qualification_decision=snapshot.qualification_decision,
        adversarial_result=snapshot.adversarial_result,
        human_review_decision=human_review_decision,
        verification_status=snapshot.latest_verification_outcome,
        ranking_tier=ranking_tier,
        supporting_evidence=supporting,
        conflicting_fields=snapshot.evidence_conflicts,
        missing_critical_fields=snapshot.evidence_missing_critical_fields,
        explanation=explanation,
        generated_at=generated_at,
    )
