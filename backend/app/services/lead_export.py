"""Phase 23 — lead export and output contract.

Pure and DB-free, mirroring every other *_engine/service module: given
already-assembled inputs (a RankedLead from Phase 22 plus a handful of
plain dicts/tuples the caller loaded from Phase 7/10/11/12 rows), builds
one deterministic ExportedLead. The caller (app/api/export.py) owns all
database access and owns calling Phase 22's ranking unchanged.

DETERMINISM: build_exported_lead() takes `exported_at` as an explicit
parameter (never reads the wall clock itself) so two calls with identical
inputs produce byte-identical output — the same discipline Phase 15's
score_lead() established for its own `now` parameter.

NEVER EXPORT A HARD-RULE VIOLATION AS ACCEPTED: this module reads
hard_rule_result exactly as Phase 12/22 already determined it and never
reinterprets it. A FAIL lead's qualification_decision/rank/tier fields are
still exported (for audit visibility — the export shows the full picture,
including why a lead was excluded), but nothing here ever relabels a FAIL
lead's tier as anything other than what Phase 22 assigned (HARD_FAILED),
and no caller-facing "accepted" flag is derived from this module — the
tier/hard_rule_result fields ARE the accepted/qualified signal, read
directly from their authoritative source.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from app.schemas.export import (
    SCHEMA_VERSION,
    ExportedLead,
    ExportEvidenceSummary,
    ExportFormat,
    ExportIdentity,
    ExportMetadata,
    ExportProvenance,
    ExportResult,
    ExportScoreComponents,
)
from app.schemas.ranking import RankedLead

CSV_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "icp_id",
    "icp_version",
    "batch_id",
    "lead_id",
    "company_id",
    "company_name",
    "company_domain",
    "person_id",
    "person_name",
    "person_title",
    "person_linkedin_id",
    "hard_rule_result",
    "hard_rule_reason_codes",
    "final_score",
    "icp_score",
    "commercial_score",
    "evidence_score",
    "freshness_score",
    "identity_confidence",
    "qualification_decision",
    "qualification_confidence",
    "qualification_summary",
    "adversarial_result",
    "adversarial_confidence",
    "verification_status",
    "human_review_decision",
    "human_review_reason_codes",
    "rank",
    "tier",
    "ranking_reason_codes",
    "is_duplicate_occurrence",
    "evidence_conflicting_fields",
    "evidence_missing_critical_fields",
    "evidence_ids",
    "hard_validation_id",
    "score_id",
    "qualification_id",
    "adversarial_review_id",
    "verification_ids",
    "human_review_id",
    "deduplication_ids",
    "company_resolution_id",
    "person_resolution_id",
    "source_provider_ids",
    "exported_at",
)

_UNKNOWN = "UNKNOWN"


def build_exported_lead(
    ranked_lead: RankedLead,
    batch_id: str | None,
    identity: ExportIdentity,
    evidence: ExportEvidenceSummary,
    qualification_summary: str | None,
    hard_rule_reason_codes: tuple[str, ...],
    human_review_reason_codes: tuple[str, ...],
    is_duplicate_occurrence: bool,
    provenance: ExportProvenance,
    exported_at: datetime,
) -> ExportedLead:
    signals = ranked_lead.signals
    return ExportedLead(
        schema_version=SCHEMA_VERSION,
        icp_id=ranked_lead.icp_id,
        icp_version=ranked_lead.icp_version,
        batch_id=batch_id,
        lead_id=ranked_lead.lead_id,
        identity=identity,
        evidence=evidence,
        hard_rule_result=signals.hard_rule_result,
        hard_rule_reason_codes=hard_rule_reason_codes,
        scores=ExportScoreComponents(
            final_score=signals.final_score,
            icp_score=signals.icp_score,
            commercial_score=signals.commercial_score,
            evidence_score=signals.evidence_score,
            freshness_score=signals.freshness_score,
            identity_confidence=signals.identity_confidence,
        ),
        qualification_decision=signals.qualification_decision,
        qualification_confidence=signals.qualification_confidence,
        qualification_summary=qualification_summary,
        adversarial_result=signals.adversarial_result,
        adversarial_confidence=signals.adversarial_confidence,
        verification_status="UNRESOLVED" if signals.verification_unresolved else None,
        human_review_decision=signals.human_review_decision,
        human_review_reason_codes=human_review_reason_codes,
        rank=ranked_lead.rank,
        tier=ranked_lead.tier.value,
        ranking_reason_codes=tuple(code.value for code in ranked_lead.reason_codes),
        is_duplicate_occurrence=is_duplicate_occurrence,
        provenance=provenance,
        exported_at=exported_at,
    )


def _none_to_unknown(value) -> str:
    if value is None:
        return _UNKNOWN
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (tuple, list)):
        return ";".join(str(v) for v in value) if value else ""
    return str(value)


def _lead_to_csv_row(lead: ExportedLead) -> dict[str, str]:
    return {
        "schema_version": lead.schema_version,
        "icp_id": lead.icp_id,
        "icp_version": str(lead.icp_version),
        "batch_id": _none_to_unknown(lead.batch_id),
        "lead_id": lead.lead_id,
        "company_id": lead.identity.company_id,
        "company_name": _none_to_unknown(lead.identity.company_name),
        "company_domain": _none_to_unknown(lead.identity.company_domain),
        "person_id": _none_to_unknown(lead.identity.person_id),
        "person_name": _none_to_unknown(lead.identity.person_name),
        "person_title": _none_to_unknown(lead.identity.person_title),
        "person_linkedin_id": _none_to_unknown(lead.identity.person_linkedin_id),
        "hard_rule_result": _none_to_unknown(lead.hard_rule_result),
        "hard_rule_reason_codes": _none_to_unknown(lead.hard_rule_reason_codes),
        "final_score": _none_to_unknown(lead.scores.final_score),
        "icp_score": _none_to_unknown(lead.scores.icp_score),
        "commercial_score": _none_to_unknown(lead.scores.commercial_score),
        "evidence_score": _none_to_unknown(lead.scores.evidence_score),
        "freshness_score": _none_to_unknown(lead.scores.freshness_score),
        "identity_confidence": _none_to_unknown(lead.scores.identity_confidence),
        "qualification_decision": _none_to_unknown(lead.qualification_decision),
        "qualification_confidence": _none_to_unknown(lead.qualification_confidence),
        "qualification_summary": _none_to_unknown(lead.qualification_summary),
        "adversarial_result": _none_to_unknown(lead.adversarial_result),
        "adversarial_confidence": _none_to_unknown(lead.adversarial_confidence),
        "verification_status": _none_to_unknown(lead.verification_status),
        "human_review_decision": _none_to_unknown(lead.human_review_decision),
        "human_review_reason_codes": _none_to_unknown(lead.human_review_reason_codes),
        "rank": _none_to_unknown(lead.rank),
        "tier": _none_to_unknown(lead.tier),
        "ranking_reason_codes": _none_to_unknown(lead.ranking_reason_codes),
        "is_duplicate_occurrence": _none_to_unknown(lead.is_duplicate_occurrence),
        "evidence_conflicting_fields": _none_to_unknown(lead.evidence.conflicting_fields),
        "evidence_missing_critical_fields": _none_to_unknown(lead.evidence.missing_critical_fields),
        "evidence_ids": _none_to_unknown(lead.evidence.evidence_ids),
        "hard_validation_id": _none_to_unknown(lead.provenance.hard_validation_id),
        "score_id": _none_to_unknown(lead.provenance.score_id),
        "qualification_id": _none_to_unknown(lead.provenance.qualification_id),
        "adversarial_review_id": _none_to_unknown(lead.provenance.adversarial_review_id),
        "verification_ids": _none_to_unknown(lead.provenance.verification_ids),
        "human_review_id": _none_to_unknown(lead.provenance.human_review_id),
        "deduplication_ids": _none_to_unknown(lead.provenance.deduplication_ids),
        "company_resolution_id": _none_to_unknown(lead.provenance.company_resolution_id),
        "person_resolution_id": _none_to_unknown(lead.provenance.person_resolution_id),
        "source_provider_ids": _none_to_unknown(lead.provenance.source_provider_ids),
        "exported_at": lead.exported_at.isoformat(),
    }


def render_json(result: ExportResult) -> str:
    """Deterministic JSON: leads are already in a fixed (rank-then-lead_id)
    order from Phase 22/this module's own assembly, and json.dumps with a
    fixed separator/no key reordering produces byte-identical output for
    byte-identical input, satisfying the export's own determinism rule."""
    return json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=False)


def render_csv(result: ExportResult) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for lead in result.leads:
        writer.writerow(_lead_to_csv_row(lead))
    return buffer.getvalue()


def render_export(result: ExportResult, export_format: ExportFormat) -> str:
    if export_format == ExportFormat.CSV:
        return render_csv(result)
    return render_json(result)
