from datetime import datetime, timezone

from app.schemas.feedback_learning import (
    ConfidenceLevel,
    DecisionCounts,
    FeedbackLearningResult,
    FeedbackLearningScope,
    ReasonCodeFrequency,
    SignalCorrelation,
)
from app.schemas.optimization import ExpectedEffect, OptimizationRecommendationType
from app.schemas.ranking import RankedLead, RankedLeadSignals, RankingReasonCode, RankingResult, RankTier
from app.services.feedback_optimization import (
    apply_adjustments_preview,
    build_optimization_result,
    build_provider_priority_hints,
    build_recommendations,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _correlation(signal_name, positive, negative, confidence) -> SignalCorrelation:
    total = positive + negative
    rate = positive / total if total > 0 else None
    return SignalCorrelation(
        signal_name=signal_name, positive_count=positive, negative_count=negative,
        total_count=total, positive_rate=rate, confidence=confidence,
    )


def _learning(icp_id="icp-1", icp_version=1, correlations=(), is_cold_start=False, total=10) -> FeedbackLearningResult:
    return FeedbackLearningResult(
        scope=FeedbackLearningScope(icp_id=icp_id, icp_version=icp_version),
        min_sample_size=5,
        decision_counts=DecisionCounts(good_fit=total, weak_fit=0, not_fit=0, hold=0, total=total),
        is_cold_start=is_cold_start,
        overall_confidence=ConfidenceLevel.MODERATE,
        reason_code_frequencies=(),
        signal_correlations=tuple(correlations),
        explanation="test",
    )


def _signals(**overrides) -> RankedLeadSignals:
    base = dict(
        hard_rule_result="PASS", final_score=80.0, icp_score=80.0, commercial_score=80.0,
        evidence_score=80.0, freshness_score=80.0, identity_confidence=80.0,
        qualification_decision="GOOD_FIT", qualification_confidence=80.0,
        adversarial_result="SURVIVES", adversarial_confidence=80.0,
        evidence_has_conflicts=False, verification_unresolved=False,
        human_review_decision=None, batch_outcome=None,
    )
    base.update(overrides)
    return RankedLeadSignals(**base)


def _ranked_lead(lead_id, rank, tier=RankTier.QUALIFIED_STRONG, **signal_overrides) -> RankedLead:
    return RankedLead(
        rank=rank, lead_id=lead_id, company_id=f"company-{lead_id}", person_id=None,
        icp_id="icp-1", icp_version=1, tier=tier, tie_break_key=(80.0, 80.0, 80.0, 80.0, lead_id),
        reason_codes=(RankingReasonCode.HARD_RULE_PASS,), signals=_signals(**signal_overrides),
        explanation="test",
    )


def _ranking(leads) -> RankingResult:
    return RankingResult(icp_id="icp-1", icp_version=1, batch_id=None, ranked_leads=tuple(leads))


# --- sufficient feedback improves ranking appropriately ---------------


def test_sufficient_feedback_produces_a_recommendation():
    learning = _learning(correlations=[_correlation("business_model:DTC", 8, 1, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1)
    assert len(recs) == 1
    assert recs[0].expected_effect == ExpectedEffect.INCREASE_PRIORITY
    assert recs[0].sample_count == 9


def test_negative_correlation_produces_a_decrease_priority_recommendation():
    learning = _learning(correlations=[_correlation("business_model:AGENCY", 1, 8, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1)
    assert recs[0].expected_effect == ExpectedEffect.DECREASE_PRIORITY
    assert recs[0].magnitude_hint < 0


# --- insufficient feedback -> no adjustment -----------------------------


def test_cold_start_produces_zero_recommendations():
    learning = _learning(is_cold_start=True, correlations=[_correlation("business_model:DTC", 1, 0, ConfidenceLevel.INSUFFICIENT_DATA)])
    recs = build_recommendations(learning, "icp-1", 1)
    assert recs == ()


def test_insufficient_data_confidence_never_produces_a_recommendation_even_outside_cold_start():
    """A signal can be INSUFFICIENT_DATA even when overall feedback is not
    cold start (e.g. a rare signal seen only once) - it must still never
    produce a recommendation."""
    learning = _learning(correlations=[
        _correlation("business_model:DTC", 8, 1, ConfidenceLevel.HIGH),
        _correlation("business_model:RARE", 1, 0, ConfidenceLevel.INSUFFICIENT_DATA),
    ])
    recs = build_recommendations(learning, "icp-1", 1)
    signal_names = {r.signal_name for r in recs}
    assert "business_model:DTC" in signal_names
    assert "business_model:RARE" not in signal_names


def test_ranking_preview_unchanged_on_cold_start():
    learning = _learning(is_cold_start=True, correlations=())
    ranking = _ranking([_ranked_lead("lead-a", 1), _ranked_lead("lead-b", 2)])
    result = build_optimization_result(learning, ranking, "icp-1", 1, min_sample_size=5, generated_at=NOW)
    assert result.recommendations == ()
    for preview in result.ranking_preview:
        assert preview.original_rank == preview.preview_rank


def test_neutral_rate_near_50_percent_produces_no_recommendation():
    """A 52%/48% split must not be treated as a meaningful pattern."""
    learning = _learning(correlations=[_correlation("business_model:NEUTRAL", 26, 24, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1)
    assert recs == ()


# --- positive / negative patterns ---------------------------------------


def test_positive_and_negative_patterns_coexist_independently():
    learning = _learning(correlations=[
        _correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH),
        _correlation("business_model:AGENCY", 0, 9, ConfidenceLevel.HIGH),
    ])
    recs = build_recommendations(learning, "icp-1", 1)
    effects = {r.signal_name: r.expected_effect for r in recs}
    assert effects["business_model:DTC"] == ExpectedEffect.INCREASE_PRIORITY
    assert effects["business_model:AGENCY"] == ExpectedEffect.DECREASE_PRIORITY


# --- multiple ICPs remain isolated ------------------------------------


def test_recommendations_scoped_to_the_requested_icp():
    learning = _learning(icp_id="icp-a", icp_version=2, correlations=[_correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-a", 2)
    assert all(r.icp_id == "icp-a" and r.icp_version == 2 for r in recs)


def test_global_flag_only_set_when_explicitly_requested_and_scope_is_global():
    learning_scoped = _learning(icp_id="icp-a", correlations=[_correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH)])
    recs_scoped = build_recommendations(learning_scoped, "icp-a", 1, include_global_patterns=True)
    assert all(r.is_global is False for r in recs_scoped)  # scope wasn't actually global

    learning_global = _learning(icp_id=None, icp_version=None, correlations=[_correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH)])
    recs_global = build_recommendations(learning_global, "icp-a", 1, include_global_patterns=True)
    assert all(r.is_global is True for r in recs_global)


# --- hard FAIL remains impossible to promote --------------------------


def test_hard_failed_lead_never_moves_out_of_its_tier_in_preview():
    ranking = _ranking([
        _ranked_lead("lead-fail", 1, tier=RankTier.HARD_FAILED, hard_rule_result="FAIL", qualification_decision=None, adversarial_result=None),
        _ranked_lead("lead-good", 2, tier=RankTier.QUALIFIED_STRONG),
    ])
    # even a maximal recommendation targeting whatever signal the FAIL lead might share
    learning = _learning(correlations=[_correlation("qualification_decision:GOOD_FIT", 9, 0, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1)
    preview = apply_adjustments_preview(ranking, recs)

    fail_preview = next(p for p in preview if p.lead_id == "lead-fail")
    good_preview = next(p for p in preview if p.lead_id == "lead-good")
    assert fail_preview.tier == "HARD_FAILED"
    assert good_preview.preview_rank < fail_preview.preview_rank  # FAIL always ranked after PASS-tier leads


def test_hard_rule_signal_never_produces_a_recommendation_at_all():
    """hard_rule_result correlations must never generate a ranking nudge
    — nudging based on hard-rule outcome would blur into overriding it."""
    learning = _learning(correlations=[_correlation("hard_rule_result:FAIL", 9, 0, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1)
    assert recs == ()


# --- conflicting feedback ------------------------------------------------


def test_conflicting_signal_with_near_even_split_produces_no_confident_recommendation():
    learning = _learning(correlations=[_correlation("business_model:MIXED", 5, 5, ConfidenceLevel.MODERATE)])
    recs = build_recommendations(learning, "icp-1", 1)
    assert recs == ()  # exactly 50/50 -> no deviation from neutral


# --- deterministic output -----------------------------------------------


def test_recommendations_are_deterministic():
    learning = _learning(correlations=[
        _correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH),
        _correlation("business_model:AGENCY", 0, 9, ConfidenceLevel.HIGH),
    ])
    first = build_recommendations(learning, "icp-1", 1)
    second = build_recommendations(learning, "icp-1", 1)
    assert first == second


def test_preview_is_deterministic():
    ranking = _ranking([_ranked_lead("lead-a", 1), _ranked_lead("lead-b", 2)])
    learning = _learning(correlations=[_correlation("qualification_decision:GOOD_FIT", 9, 0, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1)
    first = apply_adjustments_preview(ranking, recs)
    second = apply_adjustments_preview(ranking, recs)
    assert first == second


def test_zero_adjustment_preview_preserves_original_order_within_tier():
    ranking = _ranking([_ranked_lead("lead-a", 1), _ranked_lead("lead-b", 2)])
    preview = apply_adjustments_preview(ranking, ())
    assert [p.lead_id for p in preview] == ["lead-a", "lead-b"]
    assert all(p.original_rank == p.preview_rank for p in preview)


# --- provenance -------------------------------------------------------


def test_recommendation_carries_source_reference():
    learning = _learning(correlations=[_correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH)])
    recs = build_recommendations(learning, "icp-1", 1, learning_snapshot_id="snap-123")
    assert recs[0].source.learning_snapshot_id == "snap-123"
    assert recs[0].source.signal_name == "business_model:DTC"
    assert recs[0].sample_count == 9
    assert recs[0].confidence == ConfidenceLevel.HIGH


# --- provider prioritization -----------------------------------------


def test_provider_priority_hint_derived_from_commercial_signal():
    learning = _learning(correlations=[_correlation("commercial_signal:META_ADVERTISING", 9, 0, ConfidenceLevel.HIGH)])
    hints = build_provider_priority_hints(learning, "icp-1", 1)
    assert len(hints) == 1
    assert hints[0].recommendation_type == OptimizationRecommendationType.PROVIDER_PRIORITY_HINT
    assert hints[0].expected_effect == ExpectedEffect.INCREASE_PROVIDER_USAGE


def test_provider_priority_hint_never_produced_from_non_commercial_signal():
    learning = _learning(correlations=[_correlation("business_model:DTC", 9, 0, ConfidenceLevel.HIGH)])
    hints = build_provider_priority_hints(learning, "icp-1", 1)
    assert hints == ()


def test_provider_priority_hint_respects_cold_start():
    learning = _learning(is_cold_start=True, correlations=[_correlation("commercial_signal:X", 1, 0, ConfidenceLevel.INSUFFICIENT_DATA)])
    hints = build_provider_priority_hints(learning, "icp-1", 1)
    assert hints == ()


# --- cold start end-to-end -------------------------------------------


def test_build_optimization_result_cold_start_end_to_end():
    learning = _learning(is_cold_start=True, total=1, correlations=())
    ranking = _ranking([_ranked_lead("lead-a", 1)])
    result = build_optimization_result(learning, ranking, "icp-1", 1, min_sample_size=5, generated_at=NOW)
    assert result.is_cold_start is True
    assert result.recommendations == ()
    assert "Cold start" in result.explanation


# --- no mutation: pure function -------------------------------------------


def test_optimization_module_never_imports_mutating_pipeline_functions():
    import ast
    import inspect

    import app.services.feedback_optimization as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "score_lead", "qualify_lead", "run_adversarial_review", "verify_field",
        "deduplicate_lead", "decide_review", "evaluate_hard_rules", "validate_against_icp",
        "analyze_feedback",
    }
    assert not (forbidden & imported_names)


def test_module_has_no_write_or_persistence_calls():
    import inspect

    import app.services.feedback_optimization as module

    source = inspect.getsource(module)
    for forbidden in ("db.add(", "db.commit(", ".delete(", "UPDATE "):
        assert forbidden not in source
