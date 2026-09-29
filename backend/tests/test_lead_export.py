import csv
import io
import json
from datetime import datetime, timezone

from app.schemas.export import (
    ExportEvidenceSummary,
    ExportFormat,
    ExportIdentity,
    ExportMetadata,
    ExportProvenance,
    ExportResult,
    SCHEMA_VERSION,
)
from app.schemas.ranking import RankedLead, RankedLeadSignals, RankingReasonCode, RankTier
from app.services.lead_export import build_exported_lead, render_csv, render_export, render_json

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _signals(**overrides) -> RankedLeadSignals:
    base = dict(
        hard_rule_result="PASS",
        final_score=88.0,
        icp_score=90.0,
        commercial_score=85.0,
        evidence_score=80.0,
        freshness_score=95.0,
        identity_confidence=100.0,
        qualification_decision="GOOD_FIT",
        qualification_confidence=82.0,
        adversarial_result="SURVIVES",
        adversarial_confidence=78.0,
        evidence_has_conflicts=False,
        verification_unresolved=False,
        human_review_decision=None,
        batch_outcome=None,
    )
    base.update(overrides)
    return RankedLeadSignals(**base)


def _ranked_lead(**overrides) -> RankedLead:
    base = dict(
        rank=1,
        lead_id="lead-1",
        company_id="company-1",
        person_id="person-1",
        icp_id="icp-1",
        icp_version=1,
        tier=RankTier.QUALIFIED_STRONG,
        tie_break_key=(88.0, 80.0, 80.0, 82.0, "lead-1"),
        reason_codes=(RankingReasonCode.HARD_RULE_PASS, RankingReasonCode.QUALIFICATION_GOOD_FIT),
        signals=_signals(),
        explanation="Tier QUALIFIED_STRONG.",
    )
    base.update(overrides)
    return RankedLead(**base)


def _identity(**overrides) -> ExportIdentity:
    base = dict(
        company_id="company-1", company_name="Acme Inc", company_domain="acme.invalid",
        person_id="person-1", person_name="Jane Doe", person_title=None, person_linkedin_id="jane-doe",
    )
    base.update(overrides)
    return ExportIdentity(**base)


def _evidence(**overrides) -> ExportEvidenceSummary:
    base = dict(
        verified_fields={"industry": "Skincare"}, conflicting_fields=(), missing_critical_fields=(), evidence_ids=("ev-1",),
    )
    base.update(overrides)
    return ExportEvidenceSummary(**base)


def _provenance(**overrides) -> ExportProvenance:
    base = dict(
        hard_validation_id="hv-1", score_id="sc-1", qualification_id="ql-1", adversarial_review_id="ar-1",
        verification_ids=(), human_review_id=None, deduplication_ids=("dd-1",),
        company_resolution_id="cr-1", person_resolution_id="pr-1", source_provider_ids=("mock-provider-1",),
    )
    base.update(overrides)
    return ExportProvenance(**base)


def _exported_lead(**overrides):
    base = dict(
        ranked_lead=_ranked_lead(),
        batch_id="batch-1",
        identity=_identity(),
        evidence=_evidence(),
        qualification_summary="Strong ICP alignment.",
        hard_rule_reason_codes=(),
        human_review_reason_codes=(),
        is_duplicate_occurrence=False,
        provenance=_provenance(),
        exported_at=NOW,
    )
    base.update(overrides)
    return build_exported_lead(**base)


# --- complete lead ---------------------------------------------------


def test_complete_lead_exports_every_field():
    lead = _exported_lead()
    assert lead.schema_version == SCHEMA_VERSION
    assert lead.icp_id == "icp-1"
    assert lead.lead_id == "lead-1"
    assert lead.identity.company_name == "Acme Inc"
    assert lead.scores.final_score == 88.0
    assert lead.qualification_decision == "GOOD_FIT"
    assert lead.adversarial_result == "SURVIVES"
    assert lead.rank == 1
    assert lead.tier == "QUALIFIED_STRONG"
    assert lead.provenance.hard_validation_id == "hv-1"


# --- missing fields: never fabricated ---------------------------------


def test_missing_fields_stay_none_not_fabricated():
    ranked = _ranked_lead(
        person_id=None,
        signals=_signals(
            hard_rule_result=None, final_score=None, qualification_decision=None,
            qualification_confidence=None, adversarial_result=None, adversarial_confidence=None,
        ),
        tier=RankTier.HOLD,
        reason_codes=(RankingReasonCode.HARD_RULE_UNKNOWN,),
    )
    lead = _exported_lead(
        ranked_lead=ranked,
        identity=_identity(person_id=None, person_name=None, person_title=None, person_linkedin_id=None),
        qualification_summary=None,
        provenance=_provenance(qualification_id=None, adversarial_review_id=None),
    )
    assert lead.hard_rule_result is None
    assert lead.scores.final_score is None
    assert lead.qualification_decision is None
    assert lead.identity.person_id is None
    assert lead.provenance.qualification_id is None


def test_missing_fields_render_as_unknown_in_csv():
    result = ExportResult(
        metadata=ExportMetadata(schema_version=SCHEMA_VERSION, icp_id="icp-1", icp_version=1, batch_id=None, lead_count=1, generated_at=NOW),
        leads=(
            _exported_lead(
                ranked_lead=_ranked_lead(person_id=None, signals=_signals(final_score=None, qualification_decision=None)),
                identity=_identity(person_id=None, person_name=None, person_linkedin_id=None),
                qualification_summary=None,
            ),
        ),
    )
    csv_text = render_csv(result)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["final_score"] == "UNKNOWN"
    assert rows[0]["person_id"] == "UNKNOWN"
    assert rows[0]["qualification_decision"] == "UNKNOWN"


# --- hard-failed lead is never exported as accepted --------------------


def test_hard_failed_lead_is_exported_with_its_real_tier_never_as_accepted():
    ranked = _ranked_lead(
        signals=_signals(hard_rule_result="FAIL", qualification_decision=None, adversarial_result=None),
        tier=RankTier.HARD_FAILED,
        reason_codes=(RankingReasonCode.HARD_RULE_FAIL,),
    )
    lead = _exported_lead(ranked_lead=ranked, hard_rule_reason_codes=("EMPLOYEE_TOO_SMALL",))
    assert lead.hard_rule_result == "FAIL"
    assert lead.tier == "HARD_FAILED"
    assert lead.tier != "ACCEPTED"
    assert "EMPLOYEE_TOO_SMALL" in lead.hard_rule_reason_codes


# --- duplicate lead handling --------------------------------------------


def test_duplicate_occurrence_flag_is_set_from_tier():
    """In the real API (app/api/export.py), is_duplicate_occurrence is
    derived from ranked_lead.tier == DUPLICATE — this test exercises the
    pure builder with that same derivation applied by the caller."""
    ranked = _ranked_lead(tier=RankTier.DUPLICATE, reason_codes=(RankingReasonCode.HUMAN_DUPLICATE,))
    lead = _exported_lead(ranked_lead=ranked, is_duplicate_occurrence=(ranked.tier == RankTier.DUPLICATE))
    assert lead.is_duplicate_occurrence is True
    assert lead.tier == "DUPLICATE"


def test_non_duplicate_lead_flag_is_false():
    lead = _exported_lead()
    assert lead.is_duplicate_occurrence is False


# --- deterministic output -----------------------------------------------


def test_build_exported_lead_is_deterministic():
    first = _exported_lead()
    second = _exported_lead()
    assert first == second


def test_json_rendering_is_deterministic():
    result = ExportResult(
        metadata=ExportMetadata(schema_version=SCHEMA_VERSION, icp_id="icp-1", icp_version=1, batch_id=None, lead_count=1, generated_at=NOW),
        leads=(_exported_lead(),),
    )
    first = render_json(result)
    second = render_json(result)
    assert first == second


def test_csv_rendering_is_deterministic():
    result = ExportResult(
        metadata=ExportMetadata(schema_version=SCHEMA_VERSION, icp_id="icp-1", icp_version=1, batch_id=None, lead_count=1, generated_at=NOW),
        leads=(_exported_lead(),),
    )
    first = render_csv(result)
    second = render_csv(result)
    assert first == second


# --- CSV / JSON consistency ----------------------------------------------


def test_csv_and_json_report_the_same_core_values():
    result = ExportResult(
        metadata=ExportMetadata(schema_version=SCHEMA_VERSION, icp_id="icp-1", icp_version=1, batch_id="batch-1", lead_count=1, generated_at=NOW),
        leads=(_exported_lead(),),
    )
    json_body = json.loads(render_json(result))
    csv_rows = list(csv.DictReader(io.StringIO(render_csv(result))))

    json_lead = json_body["leads"][0]
    csv_lead = csv_rows[0]

    assert csv_lead["lead_id"] == json_lead["lead_id"]
    assert csv_lead["company_id"] == json_lead["identity"]["company_id"]
    assert float(csv_lead["final_score"]) == json_lead["scores"]["final_score"]
    assert csv_lead["qualification_decision"] == json_lead["qualification_decision"]
    assert csv_lead["tier"] == json_lead["tier"]


def test_render_export_dispatches_to_correct_format():
    result = ExportResult(
        metadata=ExportMetadata(schema_version=SCHEMA_VERSION, icp_id="icp-1", icp_version=1, batch_id=None, lead_count=1, generated_at=NOW),
        leads=(_exported_lead(),),
    )
    json_output = render_export(result, ExportFormat.JSON)
    csv_output = render_export(result, ExportFormat.CSV)
    json.loads(json_output)  # must parse as valid JSON
    assert "lead_id" in csv_output.splitlines()[0]  # CSV header present


# --- provenance preservation ---------------------------------------------


def test_provenance_ids_are_preserved_verbatim():
    lead = _exported_lead()
    assert lead.provenance.hard_validation_id == "hv-1"
    assert lead.provenance.score_id == "sc-1"
    assert lead.provenance.qualification_id == "ql-1"
    assert lead.provenance.adversarial_review_id == "ar-1"
    assert lead.provenance.deduplication_ids == ("dd-1",)
    assert lead.provenance.company_resolution_id == "cr-1"
    assert lead.provenance.person_resolution_id == "pr-1"
    assert lead.provenance.source_provider_ids == ("mock-provider-1",)


def test_evidence_ids_preserved_for_audit():
    lead = _exported_lead()
    assert "ev-1" in lead.evidence.evidence_ids


# --- CSV columns are stable/versionable -----------------------------------


def test_csv_header_matches_declared_columns():
    from app.services.lead_export import CSV_COLUMNS

    result = ExportResult(
        metadata=ExportMetadata(schema_version=SCHEMA_VERSION, icp_id="icp-1", icp_version=1, batch_id=None, lead_count=1, generated_at=NOW),
        leads=(_exported_lead(),),
    )
    csv_text = render_csv(result)
    header = csv_text.splitlines()[0].split(",")
    assert header == list(CSV_COLUMNS)


# --- no mutation: pure function -------------------------------------------


def test_export_module_never_imports_mutating_pipeline_functions():
    import ast
    import inspect

    import app.services.lead_export as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    forbidden = {
        "score_lead", "qualify_lead", "run_adversarial_review", "verify_field",
        "deduplicate_lead", "decide_review", "rank_leads",
    }
    assert not (forbidden & imported_names)
    assert not (forbidden & called_names)
    assert "db" not in inspect.signature(module.build_exported_lead).parameters
