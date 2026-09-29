from app.schemas.feedback import FeedbackDecision
from app.schemas.feedback_learning import ConfidenceLevel
from app.services.feedback_learning import FeedbackObservation, analyze_feedback


def _obs(decision, reason_codes=(), signals=()) -> FeedbackObservation:
    return FeedbackObservation(decision=decision, reason_codes=tuple(reason_codes), signals=frozenset(signals))


# --- GOOD_FIT vs NOT_FIT patterns --------------------------------------


def test_good_fit_and_not_fit_counts_are_tracked_separately():
    observations = [
        _obs(FeedbackDecision.GOOD_FIT),
        _obs(FeedbackDecision.GOOD_FIT),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["B2B_HEALTHCARE"]),
    ]
    result = analyze_feedback("icp-1", 1, observations, min_sample_size=1)
    assert result.decision_counts.good_fit == 2
    assert result.decision_counts.not_fit == 1
    assert result.decision_counts.total == 3


def test_signal_positive_rate_reflects_good_fit_vs_not_fit_ratio():
    observations = [
        _obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"]),
        _obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"]),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["X"], signals=["business_model:DTC"]),
    ]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    dtc = next(c for c in result.signal_correlations if c.signal_name == "business_model:DTC")
    assert dtc.positive_count == 2
    assert dtc.negative_count == 1
    assert dtc.positive_rate == 2 / 3


def test_weak_fit_and_hold_count_toward_total_but_not_positive_or_negative():
    observations = [
        _obs(FeedbackDecision.WEAK_FIT, reason_codes=["X"], signals=["business_model:B2B"]),
        _obs(FeedbackDecision.HOLD, reason_codes=["Y"], signals=["business_model:B2B"]),
    ]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    b2b = next(c for c in result.signal_correlations if c.signal_name == "business_model:B2B")
    assert b2b.positive_count == 0
    assert b2b.negative_count == 0
    assert b2b.total_count == 2
    assert b2b.positive_rate is None  # never fabricated as 0.0 or 0.5


# --- reason-code analysis -----------------------------------------------


def test_reason_code_frequencies_broken_down_by_decision():
    observations = [
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["B2B_HEALTHCARE"]),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["B2B_HEALTHCARE"]),
        _obs(FeedbackDecision.WEAK_FIT, reason_codes=["B2B_HEALTHCARE"]),
        _obs(FeedbackDecision.GOOD_FIT, reason_codes=["FOUNDER_LED"]),
    ]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    b2b_freq = next(f for f in result.reason_code_frequencies if f.reason_code == "B2B_HEALTHCARE")
    assert b2b_freq.not_fit_count == 2
    assert b2b_freq.weak_fit_count == 1
    assert b2b_freq.total_count == 3


def test_reason_code_frequencies_sorted_deterministically():
    observations = [
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["ZEBRA"]),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["ALPHA"]),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["ALPHA"]),
    ]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    codes_in_order = [f.reason_code for f in result.reason_code_frequencies]
    assert codes_in_order == ["ALPHA", "ZEBRA"]  # ALPHA has higher count (2 vs 1) -> sorts first


# --- small sample / cold start ------------------------------------------


def test_cold_start_when_below_min_sample_size():
    observations = [_obs(FeedbackDecision.GOOD_FIT)]
    result = analyze_feedback("icp-1", 1, observations, min_sample_size=5)
    assert result.is_cold_start is True
    assert result.overall_confidence == ConfidenceLevel.INSUFFICIENT_DATA


def test_not_cold_start_when_meeting_min_sample_size():
    observations = [_obs(FeedbackDecision.GOOD_FIT) for _ in range(5)]
    result = analyze_feedback("icp-1", 1, observations, min_sample_size=5)
    assert result.is_cold_start is False


def test_single_observation_never_reported_as_a_reliable_pattern():
    """A single decision perfectly matching a signal must never be
    reported as a HIGH-confidence pattern — this is the core
    anti-overfitting guarantee."""
    observations = [_obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"])]
    result = analyze_feedback(None, None, observations, min_sample_size=5)
    dtc = next(c for c in result.signal_correlations if c.signal_name == "business_model:DTC")
    assert dtc.confidence == ConfidenceLevel.INSUFFICIENT_DATA
    assert dtc.positive_rate == 1.0  # the rate is still shown honestly...
    # ...but confidence makes clear it should not be trusted


def test_confidence_scales_with_sample_size():
    def make(n):
        return [_obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"]) for _ in range(n)]

    result_low = analyze_feedback(None, None, make(6), min_sample_size=5)
    result_moderate = analyze_feedback(None, None, make(15), min_sample_size=5)
    result_high = analyze_feedback(None, None, make(25), min_sample_size=5)

    conf_low = next(c for c in result_low.signal_correlations if c.signal_name == "business_model:DTC").confidence
    conf_moderate = next(c for c in result_moderate.signal_correlations if c.signal_name == "business_model:DTC").confidence
    conf_high = next(c for c in result_high.signal_correlations if c.signal_name == "business_model:DTC").confidence

    assert conf_low == ConfidenceLevel.LOW
    assert conf_moderate == ConfidenceLevel.MODERATE
    assert conf_high == ConfidenceLevel.HIGH


def test_empty_feedback_produces_a_well_formed_cold_start_result():
    result = analyze_feedback("icp-1", 1, [], min_sample_size=5)
    assert result.is_cold_start is True
    assert result.decision_counts.total == 0
    assert result.signal_correlations == ()
    assert result.reason_code_frequencies == ()


# --- repeated feedback ----------------------------------------------------


def test_repeated_identical_feedback_accumulates_counts_not_deduplicated():
    observations = [_obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"]) for _ in range(3)]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    dtc = next(c for c in result.signal_correlations if c.signal_name == "business_model:DTC")
    assert dtc.total_count == 3  # each repeated observation counts, never collapsed to one


# --- conflicting feedback --------------------------------------------------


def test_conflicting_feedback_on_the_same_signal_is_preserved_not_resolved():
    observations = [
        _obs(FeedbackDecision.GOOD_FIT, signals=["business_model:AGENCY"]),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["AGENCY"], signals=["business_model:AGENCY"]),
    ]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    agency = next(c for c in result.signal_correlations if c.signal_name == "business_model:AGENCY")
    assert agency.positive_count == 1
    assert agency.negative_count == 1
    assert agency.positive_rate == 0.5  # both sides visible, neither silently dropped


# --- multiple ICPs / scope isolation ---------------------------------------


def test_scope_is_carried_through_unchanged():
    result_a = analyze_feedback("icp-a", 1, [_obs(FeedbackDecision.GOOD_FIT)], min_sample_size=1)
    result_b = analyze_feedback("icp-b", 3, [_obs(FeedbackDecision.GOOD_FIT)], min_sample_size=1)
    assert result_a.scope.icp_id == "icp-a"
    assert result_a.scope.icp_version == 1
    assert result_b.scope.icp_id == "icp-b"
    assert result_b.scope.icp_version == 3


def test_global_scope_has_none_icp_id():
    result = analyze_feedback(None, None, [_obs(FeedbackDecision.GOOD_FIT)], min_sample_size=1)
    assert result.scope.icp_id is None
    assert result.scope.icp_version is None


# --- hard-rule immunity: nothing here touches a hard rule -------------------


def test_analysis_never_imports_hard_rule_or_scoring_or_qualification_logic():
    import ast
    import inspect

    import app.services.feedback_learning as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    forbidden = {"evaluate_hard_rules", "validate_against_icp", "score_lead", "qualify_lead", "run_adversarial_review", "decide_review"}
    assert not (forbidden & imported_names)


def test_result_schema_has_no_accept_reject_field():
    from app.schemas.feedback_learning import FeedbackLearningResult

    fields = FeedbackLearningResult.model_fields.keys()
    for forbidden_word in ("accept", "reject", "decision_override", "hard_rule"):
        assert not any(forbidden_word in f.lower() for f in fields)


# --- no evidence fabrication -------------------------------------------


def test_signal_names_are_always_namespaced_never_free_text_guesses():
    observations = [_obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC", "commercial_signal:META_ADVERTISING"])]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    for correlation in result.signal_correlations:
        assert ":" in correlation.signal_name


def test_no_signal_correlation_is_produced_for_a_signal_never_observed():
    observations = [_obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"])]
    result = analyze_feedback(None, None, observations, min_sample_size=1)
    signal_names = {c.signal_name for c in result.signal_correlations}
    assert "business_model:B2B" not in signal_names  # never invented just because it's a plausible category


# --- deterministic results -----------------------------------------------


def test_analysis_is_deterministic():
    observations = [
        _obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"]),
        _obs(FeedbackDecision.NOT_FIT, reason_codes=["X"], signals=["business_model:B2B"]),
    ]
    first = analyze_feedback("icp-1", 1, observations, min_sample_size=1)
    second = analyze_feedback("icp-1", 1, observations, min_sample_size=1)
    assert first == second


# --- historical feedback immutability (structural guarantee) ---------------


def test_feedback_observation_is_read_only_and_never_mutates_input_list():
    observations = [_obs(FeedbackDecision.GOOD_FIT, signals=["business_model:DTC"])]
    original_len = len(observations)
    analyze_feedback(None, None, observations, min_sample_size=1)
    assert len(observations) == original_len
    assert observations[0].decision == FeedbackDecision.GOOD_FIT


def test_module_has_no_write_or_persistence_calls():
    import inspect

    import app.services.feedback_learning as module

    source = inspect.getsource(module)
    for forbidden in ("db.add(", "db.commit(", ".delete(", "UPDATE "):
        assert forbidden not in source
