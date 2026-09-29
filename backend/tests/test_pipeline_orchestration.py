import inspect

import app.services.pipeline_orchestration as module
from app.schemas.pipeline import PipelineStageName


def test_orchestration_module_never_reimplements_pipeline_logic():
    """Phase 29 must call the existing API-layer helpers (run_batch,
    _compute_ranking, _build_result, _build_export_result) rather than
    reimplementing any stage's own rules directly."""
    source = inspect.getsource(module)
    forbidden = [
        "evaluate_hard_rules(", "resolve_candidate(", "run_company_enrichment(", "run_people_discovery(",
        "collect_company_evidence(", "collect_person_evidence(", "classify_business_model(",
        "extract_commercial_signals(", "score_lead(", "qualify_lead(", "run_adversarial_review(",
        "verify_field(", "deduplicate_lead(", "decide_review(", "rank_leads(", "classify_readiness(",
        "build_exported_lead(",
    ]
    for forbidden_call in forbidden:
        assert forbidden_call not in source, f"pipeline orchestration must call the API layer, not {forbidden_call}"


def test_orchestration_module_reuses_existing_phase_entry_points():
    source = inspect.getsource(module)
    required = ["run_batch", "_compute_ranking", "_build_result", "_latest_human_review"]
    for name in required:
        assert name in source, f"pipeline orchestration should reuse {name} rather than reimplement it"


def test_stage_name_enum_covers_the_full_advertised_pipeline():
    names = {stage.value for stage in PipelineStageName}
    expected = {
        "DISCOVERY", "COMPANY_RESOLUTION", "ENRICHMENT", "PEOPLE_DISCOVERY_RESOLUTION",
        "EVIDENCE", "HARD_VALIDATION", "VERIFICATION", "BUSINESS_MODEL_SIGNALS", "SCORING",
        "LLM_QUALIFICATION", "ADVERSARIAL_REVIEW", "DEDUPLICATION", "HUMAN_REVIEW",
        "RANKING", "CONFIDENCE", "EXPORT",
    }
    assert names == expected
