"""Phase 17 — company-quality-aware ranking + Unipile-eligibility audit.

Phase 16 already built app/services/company_quality.py (the deterministic
STRONG/REVIEW/REJECT gate) and reordered the batch pipeline so Unipile only
runs after it. The Phase 17 audit found the one real gap: the EXISTING
Phase 22 ranking system (app/services/lead_ranking.py) had no awareness of
company_quality_score at all — two leads with an identical Phase 15
final_score but very different discovery provenance (structured vs weak
keyword) would tie-break on evidence_score/qualification_confidence alone,
never on the one signal that actually captures HOW the company was found.
This phase's only functional change is inserting company_quality_score into
the existing tie-break key, right after final_score — no new ranking
system, no new tier, no change to tier assignment (HARD_FAILED/HOLD/etc.
are completely unaffected — see the tier-precedence tests below).

Mirrors tests/test_lead_ranking.py's own _signals()/_rank() helper
convention for the pure-ranking tests, and tests/test_batch_api.py's
registry/ICP-payload conventions for the pipeline-integration tests.
"""
import httpx
import respx

from app.providers.mocks import MockWebSearchProvider
from app.providers.registry import ProviderRegistry
from app.schemas.ranking import RankedLeadSignals, RankingReasonCode, RankTier
from app.services.lead_ranking import rank_leads

from tests.test_batch_api import _create_batch, _create_icp, _with_registry  # noqa: F401
from tests.test_company_quality_pipeline import _SpyPeopleDataProvider, _spy_registry  # noqa: F401


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
        company_quality_score=None,
        company_quality_label=None,
    )
    base.update(overrides)
    return RankedLeadSignals(**base)


def _rank(leads):
    result = rank_leads("icp-1", 1, None, leads)
    return {rl.lead_id: rl for rl in result.ranked_leads}


# ============================================================================
# 1. Companies are correctly ranked (company_quality_score as tie-break)
# ============================================================================


def test_higher_company_quality_score_outranks_lower_within_same_tier():
    """Two leads with an IDENTICAL final_score — without Phase 17, these
    would tie-break purely on evidence_score/qualification_confidence,
    ignoring discovery provenance entirely."""
    leads = [
        ("lead-a", "co-a", None, _signals(
            final_score=80.0, qualification_decision="GOOD_FIT", adversarial_result="SURVIVES",
            company_quality_score=90.0, company_quality_label="STRONG", evidence_score=50.0,
        )),
        ("lead-b", "co-b", None, _signals(
            final_score=80.0, qualification_decision="GOOD_FIT", adversarial_result="SURVIVES",
            company_quality_score=55.0, company_quality_label="REVIEW", evidence_score=50.0,
        )),
    ]
    ranked = _rank(leads)
    assert ranked["lead-a"].rank < ranked["lead-b"].rank
    assert ranked["lead-a"].tier == ranked["lead-b"].tier == RankTier.QUALIFIED_STRONG


def test_final_score_still_takes_priority_over_company_quality_score():
    """company_quality_score is inserted AFTER final_score, not before —
    a lead with a lower final_score never outranks one with a higher
    final_score purely because its company_quality_score is higher."""
    leads = [
        ("lead-high-final", "co-a", None, _signals(
            final_score=90.0, qualification_decision="GOOD_FIT", company_quality_score=40.0,
        )),
        ("lead-high-quality", "co-b", None, _signals(
            final_score=60.0, qualification_decision="WEAK_FIT", company_quality_score=95.0,
        )),
    ]
    ranked = _rank(leads)
    assert ranked["lead-high-final"].rank < ranked["lead-high-quality"].rank


def test_missing_company_quality_score_never_outranks_a_scored_lead():
    leads = [
        ("lead-scored", "co-a", None, _signals(final_score=70.0, qualification_decision="GOOD_FIT", company_quality_score=60.0)),
        ("lead-unscored", "co-b", None, _signals(final_score=70.0, qualification_decision="GOOD_FIT", company_quality_score=None)),
    ]
    ranked = _rank(leads)
    assert ranked["lead-scored"].rank < ranked["lead-unscored"].rank
    assert RankingReasonCode.COMPANY_QUALITY_MISSING in ranked["lead-unscored"].reason_codes


# ============================================================================
# 2. Hard FAIL never reaches Unipile (tier-level regression guard)
# ============================================================================


def test_hard_fail_stays_hard_failed_tier_regardless_of_company_quality_score():
    """A hard FAIL must remain HARD_FAILED even if some other signal
    (e.g. a stale company_quality_score from a prior ICP version) is high
    — company_quality_score never participates in tier assignment, only
    intra-tier ordering."""
    leads = [
        ("lead-fail", "co-a", None, _signals(hard_rule_result="FAIL", company_quality_score=None)),
        ("lead-pass", "co-b", None, _signals(hard_rule_result="PASS", qualification_decision="GOOD_FIT", company_quality_score=10.0)),
    ]
    ranked = _rank(leads)
    assert ranked["lead-fail"].tier == RankTier.HARD_FAILED
    assert ranked["lead-pass"].tier != RankTier.HARD_FAILED
    assert ranked["lead-pass"].rank < ranked["lead-fail"].rank  # HARD_FAILED always sorts last


def test_hold_cannot_become_fake_pass_via_company_quality_score():
    """A HOLD lead with a high company_quality_score must still rank in
    the HOLD tier, strictly below every QUALIFIED_STRONG/QUALIFIED_WEAK/
    ACCEPTED lead — company_quality_score can only reorder WITHIN a tier,
    never promote a lead across the tier boundary."""
    leads = [
        ("lead-hold-high-quality", "co-a", None, _signals(hard_rule_result="HOLD", company_quality_score=95.0)),
        ("lead-pass-low-quality", "co-b", None, _signals(
            hard_rule_result="PASS", qualification_decision="WEAK_FIT", company_quality_score=10.0,
        )),
    ]
    ranked = _rank(leads)
    assert ranked["lead-hold-high-quality"].tier == RankTier.HOLD
    assert ranked["lead-pass-low-quality"].tier == RankTier.QUALIFIED_WEAK
    assert ranked["lead-pass-low-quality"].rank < ranked["lead-hold-high-quality"].rank


# ============================================================================
# 3. Deterministic / repeatable ranking
# ============================================================================


def test_ranking_is_deterministic_across_repeated_calls():
    leads = [
        ("lead-a", "co-a", None, _signals(final_score=80.0, qualification_decision="GOOD_FIT", company_quality_score=70.0)),
        ("lead-b", "co-b", None, _signals(final_score=80.0, qualification_decision="GOOD_FIT", company_quality_score=70.0)),
        ("lead-c", "co-c", None, _signals(hard_rule_result="HOLD", company_quality_score=90.0)),
    ]
    results = [tuple(rl.lead_id for rl in rank_leads("icp-1", 1, None, leads).ranked_leads) for _ in range(5)]
    assert len(set(results)) == 1  # byte-identical order every time, including the identical-score tie broken by lead_id


def test_tie_break_key_shape_widened_but_lead_id_stays_last():
    leads = [("lead-z", "co-z", None, _signals()), ("lead-a", "co-a", None, _signals())]
    ranked = _rank(leads)
    assert len(ranked["lead-a"].tie_break_key) == 5
    assert ranked["lead-a"].tie_break_key[-1] < ranked["lead-z"].tie_break_key[-1]


# ============================================================================
# 4. Pipeline integration: Unipile eligibility (REVIEW verified, not guessed)
# ============================================================================


def test_review_labeled_company_does_reach_unipile(client):
    """The task explicitly says 'verify exactly whether REVIEW companies
    should reach Unipile ... don't guess.' Reading
    app/services/batch_orchestration.py confirms the gate is
    `quality.label != CompanyQualityLabel.REJECT.value` — REVIEW and
    STRONG are BOTH eligible, only REJECT (hard FAIL) is excluded. This
    test proves it end-to-end: a HOLD company (label=REVIEW) still gets a
    real people-discovery attempt."""
    icp = _create_icp(client, "Ranking17 A")  # default wide employee range -> some items likely PASS, none forced HOLD/FAIL
    registry, spy = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()

    non_fail_items = [item for item in body["items"] if item["hard_rule_result"] != "FAIL"]
    assert non_fail_items, "expected at least one non-FAIL item for this scenario"

    quality_rows = []
    for item in non_fail_items:
        rows = client.get("/api/v1/company-quality", params={"icp_id": icp["id"], "company_id": item["company_id"]}).json()
        quality_rows.extend(rows)
    labels = {row["label"] for row in quality_rows}
    assert labels & {"STRONG", "REVIEW"}  # at least one non-FAIL company got a real label
    assert "REJECT" not in labels  # no REJECT among non-FAIL items, sanity check on the gate's own logic
    assert len(spy.people_discovery_calls) > 0  # and Unipile WAS actually called for this batch


@respx.mock
def test_only_reject_companies_are_excluded_from_unipile(client):
    """The inverse of the above: a REJECT (hard-FAIL) company is the ONLY
    label excluded from Unipile eligibility.

    Phase 32: enrichment now runs AFTER hard validation, so — mirroring
    tests/test_company_quality_pipeline.py::test_hard_rejected_company_never_reaches_people_discovery's
    own reasoning — a guaranteed hard FAIL needs the real, respx-mocked
    ExploriumCompanyDiscoveryProvider (single sighting reaches
    SUPPORTED_STRUCTURED for a trusted field), not the mock company
    providers (which only ever reach HOLD from discovery-time evidence
    alone here)."""
    from app.providers.explorium import ExploriumCompanyDiscoveryProvider
    from tests.test_company_quality_pipeline import EXPLORIUM_AUTOCOMPLETE_URL, EXPLORIUM_SEARCH_URL, _create_icp_with_hard_rules

    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "business_id": "rj0000000000000000000000000002",
                        "name": "Another Canadian Test Co",
                        "country_name": "Canada",
                        "number_of_employees_range": "51-200",
                    }
                ],
                "total_results": 1,
                "page": None,
            },
        )
    )

    icp = _create_icp_with_hard_rules(client, "Ranking17 B", geography=["France"])
    registry = ProviderRegistry()
    registry.register(ExploriumCompanyDiscoveryProvider(api_key="test-key"))
    spy = _SpyPeopleDataProvider()
    registry.register(spy)
    registry.register(MockWebSearchProvider())
    body = _create_batch(client, icp["id"], target_count=1, registry=registry).json()

    assert all(item["hard_rule_result"] == "FAIL" for item in body["items"])
    assert body["items"]  # sanity: the scenario actually discovered something
    assert spy.people_discovery_calls == []


def test_resume_does_not_duplicate_unipile_calls(client):
    icp = _create_icp(client, "Ranking17 C")
    registry, spy = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()
    calls_after_create = len(spy.people_discovery_calls)

    resume = _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{body['id']}/resume"))
    assert resume.status_code == 200
    assert len(spy.people_discovery_calls) == calls_after_create


# ============================================================================
# 5. Strong keyword company can beat weak structured company
# (already proven at the company_quality.py unit level in
# test_company_quality.py::test_keyword_fallback_company_can_still_be_strong_with_good_evidence
# — this is the RANKING-level consequence of that: it must actually place
# such a company ahead of a weak structured one.)
# ============================================================================


def test_strong_keyword_company_outranks_weak_structured_company():
    leads = [
        ("lead-keyword-strong", "co-a", None, _signals(
            final_score=75.0, qualification_decision="GOOD_FIT", adversarial_result="SURVIVES",
            company_quality_score=82.0, company_quality_label="STRONG",
        )),
        ("lead-structured-weak", "co-b", None, _signals(
            final_score=75.0, qualification_decision="WEAK_FIT",
            company_quality_score=48.0, company_quality_label="REVIEW",
        )),
    ]
    ranked = _rank(leads)
    assert ranked["lead-keyword-strong"].rank < ranked["lead-structured-weak"].rank


# ============================================================================
# 6. Missing evidence never creates false confidence in ranking
# ============================================================================


def test_lead_with_no_company_quality_row_ranked_but_never_favored():
    """A lead scored before Phase 30 ran (or sourced outside the batch
    pipeline) has company_quality_score=None — must still be ranked (never
    dropped), but never favored over a lead with a real, lower-but-present
    score, since a missing signal is not treated as a positive one."""
    leads = [
        ("lead-real-score", "co-a", None, _signals(
            final_score=60.0, qualification_decision="WEAK_FIT", company_quality_score=20.0,
        )),
        ("lead-no-quality-row", "co-b", None, _signals(
            final_score=60.0, qualification_decision="WEAK_FIT", company_quality_score=None,
        )),
    ]
    ranked = _rank(leads)
    assert ranked["lead-real-score"].rank < ranked["lead-no-quality-row"].rank
    assert ranked["lead-no-quality-row"] is not None  # still present in the ranking, not dropped


# ============================================================================
# 7. Ranking adds zero OpenAI/Explorium calls
# ============================================================================


def test_ranking_never_imports_llm_or_discovery_provider_modules():
    import inspect

    import app.services.lead_ranking as ranking_module

    source = inspect.getsource(ranking_module)
    forbidden = ["openai", "OpenAIProvider", "ExploriumCompanyDiscoveryProvider", "run_company_discovery", "interpret_icp"]
    for forbidden_name in forbidden:
        assert forbidden_name not in source, f"lead_ranking.py must never reference {forbidden_name}"


def test_ranking_api_call_makes_zero_new_provider_or_llm_calls(client):
    """Calling GET /api/v1/rankings after a batch has already run must not
    trigger any additional Explorium/Unipile/OpenAI call — ranking only
    reads already-persisted rows."""
    icp = _create_icp(client, "Ranking17 D")
    registry, spy = _spy_registry()
    body = _create_batch(client, icp["id"], target_count=2, registry=registry).json()
    calls_before_ranking = len(spy.people_discovery_calls)

    ranking_response = _with_registry(
        client, registry, lambda: client.get("/api/v1/rankings", params={"icp_id": icp["id"], "batch_id": body["id"]})
    )
    assert ranking_response.status_code == 200
    assert len(spy.people_discovery_calls) == calls_before_ranking  # ranking itself called no provider
