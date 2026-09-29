from datetime import datetime, timezone

from app.schemas.feedback_learning import ConfidenceLevel
from app.schemas.optimization import ExpectedEffect, OptimizationRecommendation, OptimizationRecommendationType, OptimizationSourceReference
from app.schemas.optimization_application import RecommendationStatus
from app.services.optimization_application import (
    build_effective_configuration,
    build_pending_recommendations,
    eligibility_for,
    fingerprint_for_recommendation,
    recommendation_fingerprint,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rec(
    icp_id="icp-1", icp_version=1, is_global=False,
    rec_type=OptimizationRecommendationType.SOFT_PREFERENCE_WEIGHT_HINT,
    signal_name="business_model:DTC", reason_code=None, sample_count=9,
    confidence=ConfidenceLevel.HIGH, expected_effect=ExpectedEffect.INCREASE_PRIORITY, magnitude_hint=1.0,
) -> OptimizationRecommendation:
    return OptimizationRecommendation(
        recommendation_type=rec_type, icp_id=icp_id, icp_version=icp_version, is_global=is_global,
        signal_name=signal_name, reason_code=reason_code, sample_count=sample_count, confidence=confidence,
        expected_effect=expected_effect, magnitude_hint=magnitude_hint, explanation="test",
        source=OptimizationSourceReference(learning_snapshot_id=None, signal_name=signal_name, reason_code=reason_code),
    )


# --- fingerprinting is deterministic and content-based --------------------


def test_fingerprint_is_deterministic():
    rec = _rec()
    first = fingerprint_for_recommendation(rec)
    second = fingerprint_for_recommendation(rec)
    assert first == second


def test_fingerprint_differs_by_icp():
    rec_a = _rec(icp_id="icp-a")
    rec_b = _rec(icp_id="icp-b")
    assert fingerprint_for_recommendation(rec_a) != fingerprint_for_recommendation(rec_b)


def test_fingerprint_differs_by_icp_version():
    rec_v1 = _rec(icp_version=1)
    rec_v2 = _rec(icp_version=2)
    assert fingerprint_for_recommendation(rec_v1) != fingerprint_for_recommendation(rec_v2)


def test_fingerprint_ignores_confidence_and_sample_count_and_magnitude():
    """The same underlying pattern must fingerprint identically even as
    more feedback arrives and its stats change — approving is about the
    pattern, not a frozen snapshot of its current strength."""
    rec_early = _rec(sample_count=6, confidence=ConfidenceLevel.LOW, magnitude_hint=0.2)
    rec_later = _rec(sample_count=40, confidence=ConfidenceLevel.HIGH, magnitude_hint=1.0)
    assert fingerprint_for_recommendation(rec_early) == fingerprint_for_recommendation(rec_later)


def test_global_and_scoped_recommendations_fingerprint_differently():
    rec_scoped = _rec(is_global=False)
    rec_global = _rec(is_global=True)
    assert fingerprint_for_recommendation(rec_scoped) != fingerprint_for_recommendation(rec_global)


# --- insufficient confidence blocked --------------------------------------


def test_insufficient_data_confidence_is_never_eligible():
    is_eligible, reason = eligibility_for("SOFT_PREFERENCE_WEIGHT_HINT", ConfidenceLevel.INSUFFICIENT_DATA)
    assert is_eligible is False
    assert reason is not None


def test_low_moderate_high_confidence_are_all_eligible():
    for level in (ConfidenceLevel.LOW, ConfidenceLevel.MODERATE, ConfidenceLevel.HIGH):
        is_eligible, _ = eligibility_for("SOFT_PREFERENCE_WEIGHT_HINT", level)
        assert is_eligible is True


def test_unknown_recommendation_type_is_never_eligible():
    is_eligible, reason = eligibility_for("SOMETHING_MADE_UP", ConfidenceLevel.HIGH)
    assert is_eligible is False
    assert reason is not None


# --- pending recommendation status derivation ------------------------------


def test_pending_recommendation_defaults_to_pending_status():
    rec = _rec()
    pending = build_pending_recommendations((rec,), status_by_fingerprint={})
    assert pending[0].status == RecommendationStatus.PENDING
    assert pending[0].is_eligible_for_approval is True


def test_pending_recommendation_reports_existing_status():
    rec = _rec()
    fp = fingerprint_for_recommendation(rec)
    pending = build_pending_recommendations((rec,), status_by_fingerprint={fp: RecommendationStatus.APPLIED})
    assert pending[0].status == RecommendationStatus.APPLIED


def test_insufficient_confidence_recommendation_is_marked_ineligible():
    rec = _rec(confidence=ConfidenceLevel.INSUFFICIENT_DATA)
    pending = build_pending_recommendations((rec,), status_by_fingerprint={})
    assert pending[0].is_eligible_for_approval is False
    assert pending[0].ineligibility_reason is not None


def test_pending_recommendations_sorted_deterministically():
    rec_high = _rec(signal_name="business_model:DTC", sample_count=20)
    rec_low = _rec(signal_name="business_model:AGENCY", sample_count=5)
    pending = build_pending_recommendations((rec_low, rec_high), status_by_fingerprint={})
    assert pending[0].signal_name == "business_model:DTC"  # higher sample_count sorts first


# --- effective configuration / cold start --------------------------------


def test_empty_applications_produce_default_configuration():
    config = build_effective_configuration("icp-1", 1, [], NOW)
    assert config.is_default is True
    assert config.ranking_adjustments == ()
    assert config.soft_preference_hints == ()
    assert config.provider_priority_hints == ()
    assert config.verification_hints == ()


def test_active_application_populates_the_correct_bucket():
    app_row = {
        "id": "app-1", "recommendation_type": "SOFT_PREFERENCE_WEIGHT_HINT", "signal_name": "business_model:DTC",
        "reason_code": None, "magnitude_hint": 1.0, "expected_effect": "INCREASE_PRIORITY", "applied_at": NOW,
    }
    config = build_effective_configuration("icp-1", 1, [app_row], NOW)
    assert config.is_default is False
    assert len(config.soft_preference_hints) == 1
    assert config.ranking_adjustments == ()


def test_provider_priority_hint_populates_its_own_bucket():
    app_row = {
        "id": "app-1", "recommendation_type": "PROVIDER_PRIORITY_HINT", "signal_name": "commercial_signal:META_ADVERTISING",
        "reason_code": None, "magnitude_hint": 0.5, "expected_effect": "INCREASE_PROVIDER_USAGE", "applied_at": NOW,
    }
    config = build_effective_configuration("icp-1", 1, [app_row], NOW)
    assert len(config.provider_priority_hints) == 1
    assert config.soft_preference_hints == ()


# --- deterministic behavior -------------------------------------------


def test_effective_configuration_is_deterministic():
    app_row = {
        "id": "app-1", "recommendation_type": "RANKING_TIE_BREAK_NUDGE", "signal_name": "qualification_decision:GOOD_FIT",
        "reason_code": None, "magnitude_hint": 0.5, "expected_effect": "INCREASE_PRIORITY", "applied_at": NOW,
    }
    first = build_effective_configuration("icp-1", 1, [app_row], NOW)
    second = build_effective_configuration("icp-1", 1, [app_row], NOW)
    assert first == second


# --- no mutation / structural safety --------------------------------------


def test_module_never_imports_hard_rule_or_scoring_or_qualification_logic():
    import ast
    import inspect

    import app.services.optimization_application as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "evaluate_hard_rules", "validate_against_icp", "score_lead", "qualify_lead",
        "run_adversarial_review", "decide_review", "rank_leads", "deduplicate_lead",
    }
    assert not (forbidden & imported_names)


def test_module_has_no_write_or_persistence_calls():
    import inspect

    import app.services.optimization_application as module

    source = inspect.getsource(module)
    for forbidden in ("db.add(", "db.commit(", ".delete(", "UPDATE "):
        assert forbidden not in source


def test_recommendation_fingerprint_is_a_pure_function():
    first = recommendation_fingerprint("icp-1", 1, "SOFT_PREFERENCE_WEIGHT_HINT", "business_model:DTC", None, False)
    second = recommendation_fingerprint("icp-1", 1, "SOFT_PREFERENCE_WEIGHT_HINT", "business_model:DTC", None, False)
    assert first == second
    assert isinstance(first, str)
    assert len(first) == 32
