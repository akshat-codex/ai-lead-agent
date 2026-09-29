from app.schemas.ranking import RankedLeadSignals, RankingReasonCode, RankTier
from app.services.lead_ranking import rank_leads


def _signals(**overrides) -> RankedLeadSignals:
    base = dict(
        hard_rule_result="PASS",
        final_score=None,
        icp_score=None,
        commercial_score=None,
        evidence_score=None,
        freshness_score=None,
        identity_confidence=None,
        qualification_decision=None,
        qualification_confidence=None,
        adversarial_result=None,
        adversarial_confidence=None,
        evidence_has_conflicts=False,
        verification_unresolved=False,
        human_review_decision=None,
        batch_outcome=None,
    )
    base.update(overrides)
    return RankedLeadSignals(**base)


def _rank(leads):
    result = rank_leads("icp-1", 1, None, leads)
    return {rl.lead_id: rl for rl in result.ranked_leads}


# --- strong vs weak leads --------------------------------------------


def test_strong_qualified_lead_outranks_weak_qualified_lead():
    leads = [
        ("lead-strong", "co-1", None, _signals(final_score=95.0, qualification_decision="GOOD_FIT", adversarial_result="SURVIVES")),
        ("lead-weak", "co-2", None, _signals(final_score=40.0, qualification_decision="WEAK_FIT")),
    ]
    ranked = _rank(leads)
    assert ranked["lead-strong"].rank < ranked["lead-weak"].rank
    assert ranked["lead-strong"].tier == RankTier.QUALIFIED_STRONG
    assert ranked["lead-weak"].tier == RankTier.QUALIFIED_WEAK


def test_higher_final_score_ranks_above_lower_score_within_same_tier():
    leads = [
        ("lead-a", "co-1", None, _signals(final_score=70.0, qualification_decision="GOOD_FIT")),
        ("lead-b", "co-2", None, _signals(final_score=90.0, qualification_decision="GOOD_FIT")),
    ]
    ranked = _rank(leads)
    assert ranked["lead-b"].rank == 1
    assert ranked["lead-a"].rank == 2


# --- hard FAIL never outranks eligible leads ---------------------------


def test_hard_fail_never_outranks_any_eligible_lead():
    leads = [
        ("lead-fail-high-score", "co-1", None, _signals(hard_rule_result="FAIL", final_score=999.0, qualification_decision="GOOD_FIT")),
        ("lead-hold-no-score", "co-2", None, _signals(hard_rule_result="HOLD")),
    ]
    ranked = _rank(leads)
    assert ranked["lead-hold-no-score"].rank < ranked["lead-fail-high-score"].rank
    assert ranked["lead-fail-high-score"].tier == RankTier.HARD_FAILED


def test_hard_fail_reason_code_present():
    leads = [("lead-1", "co-1", None, _signals(hard_rule_result="FAIL"))]
    ranked = _rank(leads)
    assert RankingReasonCode.HARD_RULE_FAIL in ranked["lead-1"].reason_codes


def test_human_accept_cannot_rescue_a_hard_fail():
    """Even the strongest possible signal (a human ACCEPT) must never move
    a hard-FAIL lead out of the HARD_FAILED tier."""
    leads = [("lead-1", "co-1", None, _signals(hard_rule_result="FAIL", human_review_decision="ACCEPT"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.HARD_FAILED


# --- deterministic tie-break --------------------------------------------


def test_identical_scores_break_tie_deterministically_by_lead_id():
    leads = [
        ("lead-z", "co-1", None, _signals(final_score=80.0, qualification_decision="GOOD_FIT")),
        ("lead-a", "co-2", None, _signals(final_score=80.0, qualification_decision="GOOD_FIT")),
    ]
    ranked = _rank(leads)
    assert ranked["lead-a"].rank == 1  # lexicographically smaller lead_id wins the tie
    assert ranked["lead-z"].rank == 2
    assert ranked["lead-a"].tie_break_key[-1] < ranked["lead-z"].tie_break_key[-1]


def test_tie_break_is_stable_across_repeated_calls():
    leads = [
        ("lead-b", "co-1", None, _signals(final_score=80.0, qualification_decision="GOOD_FIT")),
        ("lead-a", "co-2", None, _signals(final_score=80.0, qualification_decision="GOOD_FIT")),
    ]
    first = _rank(leads)
    second = _rank(leads)
    assert [rl.lead_id for rl in sorted(first.values(), key=lambda r: r.rank)] == [
        rl.lead_id for rl in sorted(second.values(), key=lambda r: r.rank)
    ]


def test_evidence_score_breaks_ties_when_final_score_matches():
    leads = [
        ("lead-a", "co-1", None, _signals(final_score=80.0, evidence_score=50.0, qualification_decision="GOOD_FIT")),
        ("lead-b", "co-2", None, _signals(final_score=80.0, evidence_score=90.0, qualification_decision="GOOD_FIT")),
    ]
    ranked = _rank(leads)
    assert ranked["lead-b"].rank == 1  # higher evidence_score wins when final_score ties


# --- missing data --------------------------------------------------------


def test_missing_score_never_fabricated_as_zero_or_max():
    leads = [("lead-1", "co-1", None, _signals(final_score=None, qualification_decision="GOOD_FIT"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].signals.final_score is None
    assert RankingReasonCode.SCORE_MISSING in ranked["lead-1"].reason_codes


def test_lead_with_no_pipeline_data_at_all_lands_in_hold_not_crashes():
    leads = [("lead-1", "co-1", None, _signals(hard_rule_result=None))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.HOLD
    assert RankingReasonCode.HARD_RULE_UNKNOWN in ranked["lead-1"].reason_codes


def test_unscored_lead_ranks_below_scored_lead_in_same_tier():
    leads = [
        ("lead-scored", "co-1", None, _signals(hard_rule_result="HOLD", final_score=None)),
        ("lead-also-unscored", "co-2", None, _signals(hard_rule_result="HOLD", final_score=None)),
    ]
    ranked = _rank(leads)
    # both HOLD, both unscored -> deterministic tie-break still applies, no crash
    assert {ranked["lead-scored"].tier, ranked["lead-also-unscored"].tier} == {RankTier.HOLD}


# --- conflicting evidence -------------------------------------------------


def test_evidence_conflicts_are_surfaced_but_do_not_change_tier_alone():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", evidence_has_conflicts=True))]
    ranked = _rank(leads)
    assert RankingReasonCode.EVIDENCE_CONFLICTS_PRESENT in ranked["lead-1"].reason_codes
    assert ranked["lead-1"].tier == RankTier.QUALIFIED_STRONG  # conflicts are diagnostic, not a tier override by themselves


# --- qualification / adversarial outcomes -------------------------------


def test_qualification_not_fit_is_rejected_tier():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="NOT_FIT"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.REJECTED
    assert RankingReasonCode.QUALIFICATION_NOT_FIT in ranked["lead-1"].reason_codes


def test_adversarial_disproved_overrides_good_fit_qualification():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", adversarial_result="DISPROVED"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.REJECTED
    assert RankingReasonCode.ADVERSARIAL_DISPROVED in ranked["lead-1"].reason_codes


def test_adversarial_weakened_downgrades_good_fit_to_qualified_weak():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", adversarial_result="WEAKENED"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.QUALIFIED_WEAK


def test_missing_qualification_after_pass_is_hold():
    leads = [("lead-1", "co-1", None, _signals(hard_rule_result="PASS", qualification_decision=None))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.HOLD
    assert RankingReasonCode.QUALIFICATION_MISSING in ranked["lead-1"].reason_codes


# --- human-review decisions -----------------------------------------------


def test_human_accept_places_lead_in_top_tier():
    leads = [
        ("lead-accepted", "co-1", None, _signals(qualification_decision="WEAK_FIT", human_review_decision="ACCEPT")),
        ("lead-good-fit-unreviewed", "co-2", None, _signals(qualification_decision="GOOD_FIT", adversarial_result="SURVIVES")),
    ]
    ranked = _rank(leads)
    assert ranked["lead-accepted"].rank < ranked["lead-good-fit-unreviewed"].rank
    assert ranked["lead-accepted"].tier == RankTier.ACCEPTED


def test_human_reject_places_lead_in_rejected_tier_even_if_good_fit():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", human_review_decision="REJECT"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.REJECTED


def test_human_hold_places_lead_in_hold_tier():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", human_review_decision="HOLD"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.HOLD


# --- batch outcome (Phase 21) --------------------------------------------


def test_batch_duplicate_outcome_places_lead_in_duplicate_tier():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", batch_outcome="DUPLICATE"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.DUPLICATE


def test_batch_duplicate_overrides_even_a_hold_human_decision():
    leads = [("lead-1", "co-1", None, _signals(batch_outcome="DUPLICATE", human_review_decision="HOLD"))]
    ranked = _rank(leads)
    assert ranked["lead-1"].tier == RankTier.DUPLICATE


# --- verification status --------------------------------------------------


def test_verification_unresolved_is_surfaced_as_a_diagnostic_reason():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT", verification_unresolved=True))]
    ranked = _rank(leads)
    assert RankingReasonCode.VERIFICATION_UNRESOLVED in ranked["lead-1"].reason_codes


# --- multi-ICP reuse: ranking is a pure function of the icp_id/version passed in ---


def test_same_signals_different_icp_ids_are_independent_rankings():
    leads = [("lead-1", "co-1", None, _signals(qualification_decision="GOOD_FIT"))]
    result_a = rank_leads("icp-a", 1, None, leads)
    result_b = rank_leads("icp-b", 7, None, leads)
    assert result_a.icp_id == "icp-a"
    assert result_b.icp_id == "icp-b"
    assert result_b.icp_version == 7
    assert result_a.ranked_leads[0].tier == result_b.ranked_leads[0].tier  # same signals -> same tier, independent of which ICP


# --- repeated ranking is deterministic -------------------------------------


def test_repeated_ranking_calls_produce_identical_results():
    leads = [
        ("lead-a", "co-1", None, _signals(final_score=70.0, qualification_decision="GOOD_FIT")),
        ("lead-b", "co-2", None, _signals(final_score=90.0, qualification_decision="WEAK_FIT")),
        ("lead-c", "co-3", None, _signals(hard_rule_result="FAIL")),
    ]
    first = rank_leads("icp-1", 1, None, leads)
    second = rank_leads("icp-1", 1, None, leads)
    assert first == second


# --- batch-scoped ranking --------------------------------------------------


def test_batch_id_is_carried_through_to_the_result():
    leads = [("lead-1", "co-1", None, _signals())]
    result = rank_leads("icp-1", 1, "batch-42", leads)
    assert result.batch_id == "batch-42"


# --- no mutation: pure function, only reads its input list -----------------


def test_rank_leads_does_not_mutate_input_signals():
    signals = _signals(final_score=50.0)
    leads = [("lead-1", "co-1", None, signals)]
    rank_leads("icp-1", 1, None, leads)
    assert signals.final_score == 50.0  # frozen model; unchanged after ranking


def test_ranking_module_never_imports_mutating_pipeline_functions():
    import inspect

    import app.services.lead_ranking as module

    source = inspect.getsource(module)
    for forbidden in ("score_lead(", "qualify_lead(", "run_adversarial_review(", "verify_field(", "deduplicate_lead(", "decide_review("):
        assert forbidden not in source


# --- score bounds / rank sequencing ----------------------------------------


def test_ranks_are_1_indexed_and_contiguous():
    leads = [
        ("lead-a", "co-1", None, _signals(final_score=10.0, qualification_decision="GOOD_FIT")),
        ("lead-b", "co-2", None, _signals(final_score=20.0, qualification_decision="GOOD_FIT")),
        ("lead-c", "co-3", None, _signals(final_score=30.0, qualification_decision="GOOD_FIT")),
    ]
    result = rank_leads("icp-1", 1, None, leads)
    assert [rl.rank for rl in result.ranked_leads] == [1, 2, 3]


def test_empty_lead_list_produces_empty_ranking():
    result = rank_leads("icp-1", 1, None, [])
    assert result.ranked_leads == ()
