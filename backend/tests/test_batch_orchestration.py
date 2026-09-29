import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.batch import BatchItemModel, BatchModel
from app.schemas.batch import BatchItemOutcome, BatchItemStage
from app.schemas.hard_rule_result import OverallResult
from app.schemas.lead_deduplication import LeadDeduplicationDecision
from app.schemas.llm_qualification import QualificationDecision
from app.services.batch_orchestration import (
    _classify_outcome,
    _should_run_another_discovery_round,
    _stage_index,
    _at_least,
)


@pytest.fixture()
def db_session():
    """An isolated in-memory SQLite session — this file tests pure/near-pure
    functions and deliberately has no HTTP/registry dependency, unlike
    test_batch_api.py; _should_run_another_discovery_round only needs a
    real Session to count BatchItemModel rows."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()


def _batch(target_count=5, discovery_error_code=None, discovery_pool_exhausted=False, discovery_rounds_run=0) -> BatchModel:
    return BatchModel(
        id="batch-1", icp_id="icp-1", icp_version=1,
        requested_target_count=target_count, discovery_limit=20, people_limit_per_company=5,
        status="RUNNING", discovery_error_code=discovery_error_code, discovery_pool_exhausted=discovery_pool_exhausted,
        discovery_rounds_run=discovery_rounds_run,
    )


def _add_item(db_session, outcome, item_id):
    db_session.add(BatchItemModel(
        id=item_id, batch_id="batch-1", icp_id="icp-1", icp_version=1,
        source_candidate_id=f"cand-{item_id}", stage=BatchItemStage.DONE.value, outcome=outcome,
    ))


class _FakeValidation:
    def __init__(self, overall_result):
        self.overall_result = overall_result


class _FakeDedup:
    def __init__(self, decision):
        self.decision = decision


def _item(qualification_decision=None, stage=BatchItemStage.DISCOVERED.value):
    return BatchItemModel(
        id="item-1", batch_id="batch-1", icp_id="icp-1", icp_version=1,
        source_candidate_id="cand-1", stage=stage, qualification_decision=qualification_decision,
    )


# --- stage ordering (used to make resume safe) --------------------------


def test_stage_index_is_monotonic():
    assert _stage_index(BatchItemStage.DISCOVERED.value) < _stage_index(BatchItemStage.COMPANY_RESOLVED.value)
    assert _stage_index(BatchItemStage.COMPANY_RESOLVED.value) < _stage_index(BatchItemStage.ENRICHED.value)
    assert _stage_index(BatchItemStage.SCORED.value) < _stage_index(BatchItemStage.DONE.value)
    # Phase 32: ENRICHED now sits after SCORED/COMPANY_QUALITY_SCORED,
    # alongside PEOPLE_DISCOVERED/PERSON_RESOLVED — discovery/validation
    # never depends on enrichment (see BatchItemStage's own docstring).
    assert _stage_index(BatchItemStage.SCORED.value) < _stage_index(BatchItemStage.ENRICHED.value)
    assert _stage_index(BatchItemStage.COMPANY_QUALITY_SCORED.value) < _stage_index(BatchItemStage.ENRICHED.value)
    assert _stage_index(BatchItemStage.ENRICHED.value) < _stage_index(BatchItemStage.QUALIFIED.value)


def test_at_least_correctly_gates_resume_skipping():
    # Phase 32: ENRICHED now sits AFTER SCORED (moved alongside
    # PEOPLE_DISCOVERED/PERSON_RESOLVED — see BatchItemStage's own
    # docstring), so a SCORED item has NOT yet reached ENRICHED.
    item = _item(stage=BatchItemStage.SCORED.value)
    assert not _at_least(item, BatchItemStage.ENRICHED)
    assert _at_least(item, BatchItemStage.SCORED)
    assert not _at_least(item, BatchItemStage.QUALIFIED)

    enriched_item = _item(stage=BatchItemStage.ENRICHED.value)
    assert _at_least(enriched_item, BatchItemStage.SCORED)  # SCORED happens before ENRICHED now
    assert _at_least(enriched_item, BatchItemStage.COMPANY_QUALITY_SCORED)
    assert not _at_least(enriched_item, BatchItemStage.PERSON_RESOLVED)


# --- outcome classification: hard-fail protection -----------------------


def test_hard_fail_is_rejected_never_accepted():
    item = _item(qualification_decision=QualificationDecision.GOOD_FIT.value)  # even a "good" qualification
    _classify_outcome(item, _FakeValidation(OverallResult.FAIL.value), _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value))
    assert item.outcome == BatchItemOutcome.REJECTED.value


def test_hard_hold_is_held_never_accepted():
    item = _item(qualification_decision=QualificationDecision.GOOD_FIT.value)
    _classify_outcome(item, _FakeValidation(OverallResult.HOLD.value), _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value))
    assert item.outcome == BatchItemOutcome.HELD.value


def test_pass_with_good_fit_is_accepted():
    item = _item(qualification_decision=QualificationDecision.GOOD_FIT.value)
    _classify_outcome(item, _FakeValidation(OverallResult.PASS.value), _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value))
    assert item.outcome == BatchItemOutcome.ACCEPTED.value


def test_pass_with_weak_fit_is_accepted():
    item = _item(qualification_decision=QualificationDecision.WEAK_FIT.value)
    _classify_outcome(item, _FakeValidation(OverallResult.PASS.value), _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value))
    assert item.outcome == BatchItemOutcome.ACCEPTED.value


def test_pass_with_not_fit_is_rejected():
    item = _item(qualification_decision=QualificationDecision.NOT_FIT.value)
    _classify_outcome(item, _FakeValidation(OverallResult.PASS.value), _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value))
    assert item.outcome == BatchItemOutcome.REJECTED.value


def test_pass_with_no_qualification_decision_is_held_not_guessed():
    item = _item(qualification_decision=None)
    _classify_outcome(item, _FakeValidation(OverallResult.PASS.value), _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value))
    assert item.outcome == BatchItemOutcome.HELD.value


def test_matched_existing_lead_is_duplicate_regardless_of_hard_rule_result():
    item = _item(qualification_decision=QualificationDecision.GOOD_FIT.value)
    _classify_outcome(item, _FakeValidation(OverallResult.PASS.value), _FakeDedup(LeadDeduplicationDecision.MATCHED_EXISTING_LEAD.value))
    assert item.outcome == BatchItemOutcome.DUPLICATE.value


def test_duplicate_classification_takes_priority_even_on_hard_fail():
    """A duplicate candidate is reported as DUPLICATE, not REJECTED, even
    if this occurrence's hard validation happened to fail — the batch's
    job is to report that this is repeat work, not to re-litigate a lead
    that was already accounted for."""
    item = _item()
    _classify_outcome(item, _FakeValidation(OverallResult.FAIL.value), _FakeDedup(LeadDeduplicationDecision.MATCHED_EXISTING_LEAD.value))
    assert item.outcome == BatchItemOutcome.DUPLICATE.value


# --- determinism ---------------------------------------------------------


def test_classification_is_deterministic():
    item_a = _item(qualification_decision=QualificationDecision.GOOD_FIT.value)
    item_b = _item(qualification_decision=QualificationDecision.GOOD_FIT.value)
    validation = _FakeValidation(OverallResult.PASS.value)
    dedup = _FakeDedup(LeadDeduplicationDecision.NEW_LEAD.value)
    _classify_outcome(item_a, validation, dedup)
    _classify_outcome(item_b, validation, dedup)
    assert item_a.outcome == item_b.outcome


# --- no second pipeline implementation -----------------------------------


def test_orchestration_module_never_reimplements_pipeline_logic():
    import inspect

    import app.services.batch_orchestration as module

    source = inspect.getsource(module)
    forbidden = [
        "evaluate_hard_rules(", "resolve_candidate(", "run_company_enrichment(", "run_people_discovery(",
        "collect_company_evidence(", "collect_person_evidence(", "classify_business_model(",
        "extract_commercial_signals(", "score_lead(", "qualify_lead(", "run_adversarial_review(",
        "verify_field(", "deduplicate_lead(", "decide_review(",
    ]
    for forbidden_call in forbidden:
        assert forbidden_call not in source, f"batch orchestration must call the API layer, not {forbidden_call}"


# --- target-count-aware stopping logic ------------------------------------
# "Give me 50/100/200 leads" means QUALIFIED (ACCEPTED) leads, never raw
# discovered/held/rejected candidates — these tests lock in that only
# ACCEPTED outcomes count toward the target, per the product requirement
# that a target must never be satisfied by padding with weak matches.


def test_should_run_another_discovery_round_true_below_target(db_session):
    batch = _batch(target_count=5)
    _add_item(db_session, BatchItemOutcome.ACCEPTED.value, "1")
    _add_item(db_session, BatchItemOutcome.ACCEPTED.value, "2")
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is True


def test_should_run_another_discovery_round_stops_once_target_accepted_count_reached(db_session):
    batch = _batch(target_count=2)
    _add_item(db_session, BatchItemOutcome.ACCEPTED.value, "1")
    _add_item(db_session, BatchItemOutcome.ACCEPTED.value, "2")
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is False


def test_should_run_another_discovery_round_never_counts_held_or_rejected_toward_target(db_session):
    batch = _batch(target_count=5)
    _add_item(db_session, BatchItemOutcome.HELD.value, "1")
    _add_item(db_session, BatchItemOutcome.HELD.value, "2")
    _add_item(db_session, BatchItemOutcome.REJECTED.value, "3")
    _add_item(db_session, BatchItemOutcome.DUPLICATE.value, "4")
    _add_item(db_session, BatchItemOutcome.FAILED.value, "5")
    db_session.flush()
    # 5 items exist (== target_count), but zero are ACCEPTED — must still
    # want another round, since the target is genuinely unmet.
    assert _should_run_another_discovery_round(db_session, batch) is True


def test_should_run_another_discovery_round_false_when_pool_exhausted(db_session):
    batch = _batch(target_count=5, discovery_pool_exhausted=True)
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is False


def test_should_run_another_discovery_round_false_on_unresolved_provider_error(db_session):
    batch = _batch(target_count=5, discovery_error_code="EXPLORIUM_CREDITS_EXHAUSTED")
    db_session.flush()
    # A real, non-retryable provider error must never be silently retried
    # in an automatic loop — the caller (frontend) must see and handle it.
    assert _should_run_another_discovery_round(db_session, batch) is False


# --- Phase 25: hard round-count ceiling (never loop indefinitely on 0 ACCEPTED) --


def test_target_count_one_with_zero_accepted_keeps_wanting_rounds_below_the_cap(db_session):
    """The exact live Phase 24 scenario: requested_target_count=1, nothing
    ACCEPTED yet, well under the round cap — must still want another
    round (this is what makes the feature useful at all; only the CAP,
    not the target-count logic itself, should ever stop it)."""
    batch = _batch(target_count=1, discovery_rounds_run=2)
    _add_item(db_session, BatchItemOutcome.HELD.value, "1")
    _add_item(db_session, BatchItemOutcome.REJECTED.value, "2")
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is True


def test_discovery_stops_at_max_rounds_even_with_target_unmet(db_session, monkeypatch):
    """The core Phase 25 fix: once discovery_rounds_run reaches the
    configured ceiling, the batch must stop requesting more rounds no
    matter how far below target_count it still is — this is what
    prevents the Phase 24 live scenario (6 rounds, 100 companies, full
    credit exhaustion, target_count=1 never satisfied) from recurring."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(
        orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=3)
    )

    batch = _batch(target_count=1, discovery_rounds_run=3)  # already at the cap
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is False


def test_round_limit_reached_sets_a_distinct_honest_error_code(db_session, monkeypatch):
    """The terminal state must be an honest, distinct code — never
    silently reported as a provider failure (EXPLORIUM_CREDITS_EXHAUSTED/
    EXPLORIUM_AUTH_FAILED), since this can trip even with plenty of
    remaining provider credits, purely because target_count was never
    satisfied within the round budget."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings
    from app.services.batch_orchestration import DISCOVERY_ROUND_LIMIT_REACHED

    monkeypatch.setattr(
        orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=2)
    )

    batch = _batch(target_count=5, discovery_rounds_run=2)
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is False
    assert batch.discovery_error_code == DISCOVERY_ROUND_LIMIT_REACHED
    assert batch.discovery_error_code not in {"EXPLORIUM_CREDITS_EXHAUSTED", "EXPLORIUM_AUTH_FAILED"}
    assert batch.discovery_error_message  # a real, non-empty explanation, never silent


def test_round_limit_reached_is_a_clean_terminal_state_never_retried(db_session, monkeypatch):
    """Once the round-limit error is set, subsequent calls must behave
    exactly like any other real provider error — never silently retried
    — mirroring test_should_run_another_discovery_round_false_on_unresolved_provider_error."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings
    from app.services.batch_orchestration import DISCOVERY_ROUND_LIMIT_REACHED

    monkeypatch.setattr(
        orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=1)
    )

    batch = _batch(target_count=5, discovery_rounds_run=1)
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is False
    assert batch.discovery_error_code == DISCOVERY_ROUND_LIMIT_REACHED

    # A second call, with the error code now set, must short-circuit on
    # the existing-error check and never re-derive or overwrite anything.
    assert _should_run_another_discovery_round(db_session, batch) is False
    assert batch.discovery_error_code == DISCOVERY_ROUND_LIMIT_REACHED


def test_discovery_rounds_run_none_is_treated_as_zero_not_a_crash(db_session, monkeypatch):
    """An unflushed, freshly-constructed BatchModel can have
    discovery_rounds_run=None at the Python object level (the column's
    default=0 only applies on INSERT) — the round-limit check must treat
    that as 0, never raise a TypeError comparing None to an int."""
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(
        orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=3)
    )

    batch = BatchModel(
        id="batch-none-rounds", icp_id="icp-1", icp_version=1,
        requested_target_count=5, discovery_limit=20, people_limit_per_company=5,
        status="RUNNING",
    )
    assert batch.discovery_rounds_run is None  # sanity: this is the exact pre-flush state being guarded against
    assert _should_run_another_discovery_round(db_session, batch) is True


def test_round_cap_is_configurable_via_settings(db_session, monkeypatch):
    import app.services.batch_orchestration as orchestration_module
    from app.core.config import Settings

    monkeypatch.setattr(
        orchestration_module, "get_settings", lambda: Settings(max_discovery_rounds_per_batch=10)
    )
    batch = _batch(target_count=1, discovery_rounds_run=5)  # would have tripped the old hardcoded default of 5
    _add_item(db_session, BatchItemOutcome.HELD.value, "1")
    db_session.flush()
    assert _should_run_another_discovery_round(db_session, batch) is True
