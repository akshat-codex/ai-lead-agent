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

CRM-shaped export (HUBSPOT_CSV/SALESFORCE_CSV): see the "CRM-shaped
bulk-import CSVs" section below, near render_hubspot_csv/render_salesforce_csv,
for the verified column mapping and its honest limits.
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


# --- CRM-shaped bulk-import CSVs ------------------------------------------
#
# Column names verified live against each CRM's own official import docs
# (2026-09-30): HubSpot's Import & Export knowledge base
# (knowledge.hubspot.com/import-and-export/set-up-your-import-file) and
# Salesforce's Data Import Wizard docs (help.salesforce.com). These are
# FILE FORMATS ONLY — the user still uploads the result through that CRM's
# own import wizard; nothing here makes a network call to a CRM, stores an
# OAuth token, or sends anything anywhere. See ExportFormat's own docstring.
#
# Only fields with a genuine, documented, CRM-native column are mapped to
# that CRM's own standard header name. A field with no standard home in
# either CRM (person_linkedin_id, qualification_summary, final_score, rank,
# tier — confirmed against both CRMs' own docs, neither has a default
# contact/company property for any of these) is exported under a clearly
# custom-looking header (prefixed, e.g. "Lead Agent - Final Score") rather
# than mapped to a lookalike standard field — silently mislabeling one of
# these as if it were a native CRM field would risk the importer target
# it at the wrong existing property, or a user assuming a mapping exists
# that doesn't.

_NAME_SPLIT_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"})


def _split_person_name(full_name: str | None) -> tuple[str, str]:
    """Best-effort First/Last Name split for CRMs whose Contact object has
    no single "full name" field (confirmed for both HubSpot and
    Salesforce — Last Name is Salesforce's only hard-required Contact
    field, so this must never return an empty last name when any name text
    exists at all).

    HONEST LIMITATION, not silently glossed over: a full name is not
    reliably splittable — multi-word surnames ("Maria Garcia Lopez"),
    particles ("Van Der Berg"), and name-order conventions this codebase
    has no locale signal to detect all defeat a purely mechanical split.
    This uses the simplest defensible rule (last whitespace-separated
    token is the last name, a trailing generational suffix is folded into
    it rather than mistaken for a surname) and accepts it will sometimes
    be wrong — exactly why render_hubspot_csv/render_salesforce_csv ALSO
    always emit the untouched original full name in its own column (see
    _CRM_FULL_NAME_HEADER), so nothing is ever silently lost even when the
    split itself is imperfect. This mirrors this codebase's own "never let
    a derived value hide the raw source it was derived from" discipline
    (e.g. app/services/evidence_import.py's evidence_text provenance).

    Returns ("", "") only when there is no name at all — matching this
    module's existing "absent, never fabricated" convention elsewhere.
    """
    if not full_name or not full_name.strip():
        return "", ""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return "", parts[0]
    last_token = parts[-1]
    if last_token.lower().rstrip(".") in _NAME_SPLIT_SUFFIXES and len(parts) >= 3:
        return " ".join(parts[:-2]), f"{parts[-2]} {last_token}"
    return " ".join(parts[:-1]), last_token


_CRM_FULL_NAME_HEADER = "Lead Agent - Full Name (unsplit, authoritative)"


def _crm_custom_field_rows(lead: ExportedLead) -> dict[str, str]:
    """Fields neither CRM has a standard column for — see this section's
    own header comment for why these get a clearly-custom label instead of
    a lookalike standard one."""
    return {
        "Lead Agent - LinkedIn URL": _none_to_unknown(lead.identity.person_linkedin_id),
        "Lead Agent - Qualification Summary": _none_to_unknown(lead.qualification_summary),
        "Lead Agent - Final Score": _none_to_unknown(lead.scores.final_score),
        "Lead Agent - Rank": _none_to_unknown(lead.rank),
        "Lead Agent - Tier": _none_to_unknown(lead.tier),
    }


HUBSPOT_CSV_COLUMNS: tuple[str, ...] = (
    "Email",
    "First Name",
    "Last Name",
    _CRM_FULL_NAME_HEADER,
    "Job Title",
    "Company name",
    "Company domain name",
    "Lead Agent - LinkedIn URL",
    "Lead Agent - Qualification Summary",
    "Lead Agent - Final Score",
    "Lead Agent - Rank",
    "Lead Agent - Tier",
)

SALESFORCE_CSV_COLUMNS: tuple[str, ...] = (
    "Email",
    "First Name",
    "Last Name",
    _CRM_FULL_NAME_HEADER,
    "Title",
    "Account Name",
    "Website",
    "Lead Agent - LinkedIn URL",
    "Lead Agent - Qualification Summary",
    "Lead Agent - Final Score",
    "Lead Agent - Rank",
    "Lead Agent - Tier",
)


def _lead_email(lead: ExportedLead) -> str | None:
    """Email has no dedicated ExportIdentity field (see that schema's own
    docstring — it mirrors person_title's pattern of coming from evidence,
    not a canonical-row column); it is read from the same SUPPORTED-only
    evidence.verified_fields map every other evidence-backed value already
    uses, under the "person.email" key app/api/export.py's own
    _evidence_summary writes (see that function — the same "person." prefix
    convention as "person.current_title")."""
    return lead.evidence.verified_fields.get("person.email")


def _lead_to_hubspot_row(lead: ExportedLead) -> dict[str, str]:
    first_name, last_name = _split_person_name(lead.identity.person_name)
    row = {
        "Email": _none_to_unknown(_lead_email(lead)),
        "First Name": first_name or _UNKNOWN,
        "Last Name": last_name or _UNKNOWN,
        _CRM_FULL_NAME_HEADER: _none_to_unknown(lead.identity.person_name),
        "Job Title": _none_to_unknown(lead.identity.person_title),
        "Company name": _none_to_unknown(lead.identity.company_name),
        "Company domain name": _none_to_unknown(lead.identity.company_domain),
    }
    row.update(_crm_custom_field_rows(lead))
    return row


def _lead_to_salesforce_row(lead: ExportedLead) -> dict[str, str]:
    first_name, last_name = _split_person_name(lead.identity.person_name)
    row = {
        "Email": _none_to_unknown(_lead_email(lead)),
        "First Name": first_name or _UNKNOWN,
        # Salesforce's own Contact object hard-requires Last Name (its only
        # mandatory Contact field, confirmed against Salesforce's own Data
        # Import Wizard docs) — never left as the generic _UNKNOWN sentinel
        # when NO name is known at all would still literally satisfy that
        # requirement, but this module makes no attempt to guess further;
        # an operator reviewing "UNKNOWN" before import is the honest
        # outcome, not a blocked export.
        "Last Name": last_name or _UNKNOWN,
        _CRM_FULL_NAME_HEADER: _none_to_unknown(lead.identity.person_name),
        "Title": _none_to_unknown(lead.identity.person_title),
        "Account Name": _none_to_unknown(lead.identity.company_name),
        "Website": _none_to_unknown(lead.identity.company_domain),
    }
    row.update(_crm_custom_field_rows(lead))
    return row


def render_hubspot_csv(result: ExportResult) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(HUBSPOT_CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for lead in result.leads:
        writer.writerow(_lead_to_hubspot_row(lead))
    return buffer.getvalue()


def render_salesforce_csv(result: ExportResult) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(SALESFORCE_CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for lead in result.leads:
        writer.writerow(_lead_to_salesforce_row(lead))
    return buffer.getvalue()


def render_export(result: ExportResult, export_format: ExportFormat) -> str:
    if export_format == ExportFormat.CSV:
        return render_csv(result)
    if export_format == ExportFormat.HUBSPOT_CSV:
        return render_hubspot_csv(result)
    if export_format == ExportFormat.SALESFORCE_CSV:
        return render_salesforce_csv(result)
    return render_json(result)
