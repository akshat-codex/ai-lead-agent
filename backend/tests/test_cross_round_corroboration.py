"""P2 fix — cross-round compound-ICP corroboration regression tests.

Root cause: app/services/batch_orchestration.py::_already_seen_provider_external_ids
correctly avoided a redundant full pipeline pass for a repeat discovery
candidate (same real company, same (provider_id, external_id)), but it also
silently discarded that repeat sighting's own discovery attributes
entirely — including a compound-ICP's second branch tag (e.g. "SaaS" found
via a keyword branch in round 2, for a company already discovered via the
"Healthcare" structured branch in round 1). Since
app/services/evidence_import.py::collect_company_evidence only ever reads
evidence from DiscoveryCandidateModel rows that actually exist, and
app/services/hard_icp_validation.py::_cross_branch_corroborated_terms
(Phase 37/38) was specifically built to union such cross-round provenance,
a real corroborating company could reach round 2, have its second branch's
proof thrown away before that mechanism ever saw it, while round 1's item
was already HELD before round 2 even ran — permanently stuck.

These tests exercise the new orchestration-level functions directly
(near-pure, DB-backed, no HTTP/registry — mirrors test_batch_orchestration.py's
own fixture style) rather than a full HTTP round-trip.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.batch import BatchItemModel, BatchModel
from app.models.company import CompanyResolutionModel
from app.models.discovery import DiscoveryCandidateModel
from app.schemas.batch import BatchItemOutcome, BatchItemStage
from app.schemas.candidate_company import CandidateCompany
from app.schemas.hard_rule_result import OverallResult
from app.services.batch_orchestration import (
    _carries_new_cross_branch_provenance,
    _persist_repeat_sightings_with_new_provenance,
    _reopen_held_items_for_companies,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _batch() -> BatchModel:
    return BatchModel(
        id="batch-1", icp_id="icp-1", icp_version=1,
        requested_target_count=5, discovery_limit=20, people_limit_per_company=5, status="RUNNING",
    )


def _candidate(external_id="ext-1", attributes=None, provider_id="explorium-company-discovery-v1", candidate_id=None) -> CandidateCompany:
    return CandidateCompany(
        id=candidate_id or str(uuid4()), icp_id="icp-1", icp_version=1, provider_id=provider_id,
        external_id=external_id, name="Acme Health SaaS Inc", domain="acme-health-saas.invalid",
        attributes=attributes or {}, discovered_at=NOW,
    )


def _seed_round1_candidate_row(db_session, run_id="run-1", external_id="ext-1", attributes=None) -> DiscoveryCandidateModel:
    """Mirrors real production shape exactly: _run_one_discovery_round
    (and the initial _seed_items_from_discovery) always creates a
    BatchItemModel alongside every genuinely-new DiscoveryCandidateModel
    row — _persist_repeat_sightings_with_new_provenance's own
    existing_rows query joins through BatchItemModel to correctly scope
    "already seen" to THIS batch, so a fixture representing round 1's
    real, already-processed sighting must include that row too."""
    row = DiscoveryCandidateModel(
        id=str(uuid4()), run_id=run_id, icp_id="icp-1", icp_version=1,
        provider_id="explorium-company-discovery-v1", external_id=external_id, name="Acme Health SaaS Inc",
        domain="acme-health-saas.invalid", attributes=attributes or {}, discovered_at=NOW,
    )
    db_session.add(row)
    db_session.add(BatchItemModel(
        id=str(uuid4()), batch_id="batch-1", icp_id="icp-1", icp_version=1,
        source_candidate_id=row.id, stage=BatchItemStage.DONE.value,
    ))
    db_session.flush()
    return row


def _resolve_to_company(db_session, candidate_row, company_id="company-1") -> None:
    db_session.add(CompanyResolutionModel(
        id=str(uuid4()), candidate_id=candidate_row.id, discovery_run_id=candidate_row.run_id,
        status="MATCH", canonical_company_id=company_id, reason_code="EXACT_DOMAIN_MATCH", explanation="test",
    ))
    db_session.flush()


def _held_item(db_session, company_id="company-1", item_id="item-1") -> BatchItemModel:
    item = BatchItemModel(
        id=item_id, batch_id="batch-1", icp_id="icp-1", icp_version=1, source_candidate_id="cand-round1",
        company_id=company_id, stage=BatchItemStage.DONE.value, outcome=BatchItemOutcome.HELD.value,
        hard_rule_result=OverallResult.HOLD.value, qualification_decision=None,
    )
    db_session.add(item)
    db_session.flush()
    return item


# --- _carries_new_cross_branch_provenance --------------------------------


def test_new_branch_tag_is_detected_as_new_provenance():
    existing = {"industry_match_terms": ["Healthcare"]}
    incoming = {"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]}
    assert _carries_new_cross_branch_provenance(existing, incoming) is True


def test_same_branch_repeat_is_not_new_provenance():
    """A pure re-pagination into the same company via the SAME branch (no
    new term) must stay a cheap no-op, exactly as before this fix."""
    existing = {"industry_match_terms": ["Healthcare"]}
    incoming = {"industry_match_terms": ["Healthcare"]}
    assert _carries_new_cross_branch_provenance(existing, incoming) is False


def test_empty_incoming_attributes_is_not_new_provenance():
    assert _carries_new_cross_branch_provenance({"industry_match_terms": ["Healthcare"]}, {}) is False


def test_new_company_type_branch_tag_is_detected():
    existing = {"industry_match_terms": ["Healthcare"]}
    incoming = {"company_type_match_terms": ["D2C"]}
    assert _carries_new_cross_branch_provenance(existing, incoming) is True


# --- _persist_repeat_sightings_with_new_provenance -----------------------


def test_repeat_with_new_provenance_persists_a_new_candidate_row_and_returns_company_id(db_session):
    round1 = _seed_round1_candidate_row(
        db_session, attributes={"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]}
    )
    _resolve_to_company(db_session, round1, "company-1")
    batch = _batch()

    repeat = _candidate(attributes={"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]})
    affected = _persist_repeat_sightings_with_new_provenance(db_session, batch, [repeat])

    assert affected == {"company-1"}
    rows = db_session.query(DiscoveryCandidateModel).filter(DiscoveryCandidateModel.external_id == "ext-1").all()
    assert len(rows) == 2  # round 1's original row, plus the new round-2 row
    new_row = next(r for r in rows if r.id == repeat.id)
    assert new_row.attributes.get("keyword_match_terms") == ["SaaS"]
    # The new row is its own genuinely new sighting — its own
    # discovered_at (this round's real wall-clock time, distinct from
    # round 1's), which is exactly what keeps app/api/evidence.py::
    # _is_duplicate's natural key from treating this as an
    # already-imported duplicate. (SQLite drops tzinfo on round-trip, so
    # compare naively rather than requiring exact tzinfo equality here.)
    assert new_row.discovered_at.replace(tzinfo=None) == repeat.discovered_at.replace(tzinfo=None)


def test_repeat_with_no_new_provenance_persists_nothing(db_session):
    round1 = _seed_round1_candidate_row(
        db_session, attributes={"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]}
    )
    _resolve_to_company(db_session, round1, "company-1")
    batch = _batch()

    repeat = _candidate(attributes={"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"]})
    affected = _persist_repeat_sightings_with_new_provenance(db_session, batch, [repeat])

    assert affected == set()
    rows = db_session.query(DiscoveryCandidateModel).filter(DiscoveryCandidateModel.external_id == "ext-1").all()
    assert len(rows) == 1  # nothing new persisted


def test_repeat_for_unresolved_candidate_is_skipped_safely(db_session):
    """A repeat candidate whose round-1 row was never resolved to a
    canonical company (still UNRESOLVED, or resolution genuinely missing)
    must never crash and must never be counted as affecting a company."""
    _seed_round1_candidate_row(db_session, attributes={"industry_match_terms": ["Healthcare"]})
    # Deliberately no CompanyResolutionModel row created.
    batch = _batch()
    repeat = _candidate(attributes={"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]})
    affected = _persist_repeat_sightings_with_new_provenance(db_session, batch, [repeat])
    assert affected == set()


def test_third_round_sighting_checks_against_combined_prior_provenance(db_session):
    """A third rediscovery must be checked against the UNION of everything
    already persisted across all prior rounds, not just the single most
    recent row — otherwise round 3 restating round 1's own term would be
    wrongly treated as new."""
    round1 = _seed_round1_candidate_row(
        db_session, external_id="ext-1", attributes={"industry_match_terms": ["Healthcare"]}
    )
    _resolve_to_company(db_session, round1, "company-1")
    batch = _batch()
    round2 = _candidate(attributes={"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]})
    _persist_repeat_sightings_with_new_provenance(db_session, batch, [round2])

    # Round 3 restates round 1's Healthcare term only — nothing new.
    round3 = _candidate(attributes={"industry_match_terms": ["Healthcare"]})
    affected = _persist_repeat_sightings_with_new_provenance(db_session, batch, [round3])
    assert affected == set()


# --- _reopen_held_items_for_companies ------------------------------------


def test_held_item_is_reopened_for_an_affected_company(db_session):
    item = _held_item(db_session, company_id="company-1")
    reopened = _reopen_held_items_for_companies(db_session, _batch(), {"company-1"})

    assert len(reopened) == 1
    assert reopened[0].id == item.id
    db_session.refresh(item)
    assert item.outcome is None
    assert item.hard_rule_result is None
    assert item.qualification_decision is None
    assert item.stage == BatchItemStage.COMPANY_RESOLVED.value
    assert item.company_id == "company-1"  # never re-resolved, preserved as-is


def test_accepted_item_is_never_reopened(db_session):
    """ACCEPTED/REJECTED/DUPLICATE are terminal for the automated pipeline
    (docs/lead-decision-policy.md) — only HOLD gets a defined path back."""
    item = BatchItemModel(
        id="item-1", batch_id="batch-1", icp_id="icp-1", icp_version=1, source_candidate_id="cand-round1",
        company_id="company-1", stage=BatchItemStage.DONE.value, outcome=BatchItemOutcome.ACCEPTED.value,
    )
    db_session.add(item)
    db_session.flush()

    reopened = _reopen_held_items_for_companies(db_session, _batch(), {"company-1"})
    assert reopened == []
    db_session.refresh(item)
    assert item.outcome == BatchItemOutcome.ACCEPTED.value
    assert item.stage == BatchItemStage.DONE.value


def test_rejected_item_is_never_reopened(db_session):
    item = BatchItemModel(
        id="item-1", batch_id="batch-1", icp_id="icp-1", icp_version=1, source_candidate_id="cand-round1",
        company_id="company-1", stage=BatchItemStage.DONE.value, outcome=BatchItemOutcome.REJECTED.value,
    )
    db_session.add(item)
    db_session.flush()

    reopened = _reopen_held_items_for_companies(db_session, _batch(), {"company-1"})
    assert reopened == []
    db_session.refresh(item)
    assert item.outcome == BatchItemOutcome.REJECTED.value


def test_no_affected_companies_reopens_nothing(db_session):
    _held_item(db_session, company_id="company-1")
    reopened = _reopen_held_items_for_companies(db_session, _batch(), set())
    assert reopened == []


def test_held_item_for_a_different_company_is_untouched(db_session):
    item = _held_item(db_session, company_id="company-2")
    reopened = _reopen_held_items_for_companies(db_session, _batch(), {"company-1"})
    assert reopened == []
    db_session.refresh(item)
    assert item.outcome == BatchItemOutcome.HELD.value


def test_third_round_restating_the_second_rounds_own_term_is_not_new(db_session):
    """Sharper version of the test above: round 2's own contribution
    (persisted WITHOUT a BatchItemModel, by design — see
    _persist_repeat_sightings_with_new_provenance's own docstring) must
    still be visible to round 3's comparison, not just round 1's. This is
    what actually exercises the join-scoping fix in existing_rows'
    query — a query that only found round 1's row (because only rows
    WITH a BatchItemModel satisfied the join) would wrongly treat round
    3's restatement of round 2's own term as new."""
    round1 = _seed_round1_candidate_row(
        db_session, external_id="ext-1", attributes={"industry_match_terms": ["Healthcare"]}
    )
    _resolve_to_company(db_session, round1, "company-1")
    batch = _batch()
    round2 = _candidate(attributes={"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]})
    _persist_repeat_sightings_with_new_provenance(db_session, batch, [round2])

    # Round 3 restates ONLY round 2's own term (SaaS) — nothing new,
    # since round 2 already established it.
    round3 = _candidate(attributes={"keyword_match_terms": ["SaaS"], "keyword_match_term_sources": ["industry"]})
    affected = _persist_repeat_sightings_with_new_provenance(db_session, batch, [round3])
    assert affected == set()
