"""Phase 20 — pipeline snapshot builder.

Assembles the compact PipelineSnapshot a human reviewer needs from
already-persisted Phase 11/12/15/16/17/18 rows. Never re-runs any pipeline
stage, never re-evaluates a hard rule, never re-scores, never re-qualifies
— this module only reads and compresses what already exists, exactly like
Phase 16's qualification_context.py does for the LLM layer.

Missing pipeline stages are represented as None fields, never fabricated —
a lead a reviewer opens before scoring/qualification has run simply shows
those fields empty, which is the honest state.
"""
from __future__ import annotations

from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.human_review import PipelineSnapshot
from app.services.evidence_engine import summarize_entity


def build_pipeline_snapshot(
    company_id: str,
    person_id: str | None,
    company_evidence: list[EvidenceRecord],
    person_evidence: list[EvidenceRecord],
    latest_hard_validation: dict | None,
    latest_score: dict | None,
    latest_qualification: dict | None,
    latest_adversarial_review: dict | None,
    latest_verification_outcome: str | None,
) -> PipelineSnapshot:
    """Every `latest_*` argument is a plain dict of the already-persisted
    row's relevant fields (or None if that stage never ran) — passed in by
    the caller rather than a SQLAlchemy model, keeping this function DB-free
    and independently testable, exactly like Phase 16's context builder."""
    company_summary = summarize_entity(EntityType.COMPANY, company_id, company_evidence)
    conflicts = [f.field for f in company_summary.fields if f.status == EvidenceStatus.CONFLICT]
    missing = [f.field for f in company_summary.fields if f.status == EvidenceStatus.UNKNOWN]

    if person_id is not None:
        person_summary = summarize_entity(EntityType.PERSON, person_id, person_evidence)
        conflicts += [f.field for f in person_summary.fields if f.status == EvidenceStatus.CONFLICT]
        missing += [f.field for f in person_summary.fields if f.status == EvidenceStatus.UNKNOWN]

    unresolved_explanations: tuple[str, ...] = ()
    if latest_hard_validation:
        rule_results = latest_hard_validation.get("rule_results") or []
        unresolved_explanations = tuple(
            f"{r['rule']}: {r['explanation']}"
            for r in rule_results
            if isinstance(r, dict) and r.get("status") in ("FAIL", "HOLD") and r.get("explanation")
        )

    return PipelineSnapshot(
        company_id=company_id,
        person_id=person_id,
        hard_rule_result=latest_hard_validation.get("overall_result") if latest_hard_validation else None,
        hard_rule_reason_codes=tuple(latest_hard_validation.get("reason_codes", [])) if latest_hard_validation else (),
        hard_rule_unresolved_explanations=unresolved_explanations,
        final_score=latest_score.get("final_score") if latest_score else None,
        icp_score=latest_score.get("icp_score") if latest_score else None,
        commercial_score=latest_score.get("commercial_score") if latest_score else None,
        evidence_score=latest_score.get("evidence_score") if latest_score else None,
        qualification_decision=latest_qualification.get("decision") if latest_qualification else None,
        qualification_confidence=latest_qualification.get("confidence") if latest_qualification else None,
        qualification_summary=latest_qualification.get("summary") if latest_qualification else None,
        adversarial_result=latest_adversarial_review.get("adversarial_result") if latest_adversarial_review else None,
        adversarial_confidence=latest_adversarial_review.get("confidence") if latest_adversarial_review else None,
        evidence_conflicts=tuple(dict.fromkeys(conflicts)),
        evidence_missing_critical_fields=tuple(dict.fromkeys(missing)),
        latest_verification_outcome=latest_verification_outcome,
    )
