from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.business_model import BusinessModel, BusinessModelClassificationResult, ClassificationStatus
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus, SourceType
from app.schemas.hard_rule_result import OverallResult
from app.schemas.scoring import FreshnessConfig, ResolutionSignal, ScoringWeights
from app.services.lead_scoring import score_lead

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _icp(
    industries=("Skincare",),
    countries=("US",),
    min_employees=10,
    max_employees=200,
    allowed_titles=(),
    company_types=(),
    exclusions=(),
    business_models=(),
    commercial_signals=(),
    growth_signals=(),
    marketing_signals=(),
    icp_id="icp-1",
    version=1,
) -> CanonicalICP:
    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=CanonicalHardRules(
            industries=industries,
            geography=CanonicalGeography(countries=tuple(GeographyEntry(raw=c, code=c, label=c) for c in countries)),
            employee_range=EmployeeRange(min=min_employees, max=max_employees),
            allowed_titles=allowed_titles,
            company_types=company_types,
            exclusions=exclusions,
        ),
        soft_preferences=CanonicalSoftPreferences(
            business_models=business_models,
            commercial_signals=commercial_signals,
            growth_signals=growth_signals,
            marketing_signals=marketing_signals,
        ),
    )


def _evidence(entity_type, entity_id, field, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        value=value,
        source_provider_id="provider-a",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _company_evidence(company_id="company-1", **kwargs):
    fields = {
        "company_identity": "Acme Skincare Inc",
        "domain": "acme-skincare.invalid",
        "industry": "Skincare",
        "employee_count": 50,
        "country": "US",
        "company_type": "D2C",
    }
    fields.update(kwargs)
    records = []
    for field, value in fields.items():
        if value is None:
            continue
        records.append(_evidence(EntityType.COMPANY, company_id, field, value, source_provider_id="provider-a"))
        records.append(_evidence(EntityType.COMPANY, company_id, field, value, source_provider_id="provider-b"))
    return records


def _passing_score(icp=None, **kwargs):
    icp = icp or _icp()
    defaults = dict(
        company_id="company-1",
        company_evidence=_company_evidence(),
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None,
        commercial_signals=[],
        person_id=None,
        person_evidence=[],
        person_resolutions=None,
        now=NOW,
    )
    defaults.update(kwargs)
    return score_lead(icp=icp, **defaults)


# --- hard gate absoluteness ---------------------------------------------


def test_hard_fail_blocks_eligibility_and_final_score():
    icp = _icp(min_employees=1000)  # company has 50 employees -> FAIL
    result = _passing_score(icp=icp)
    assert result.hard_icp_result == OverallResult.FAIL
    assert result.eligible_for_scoring is False
    assert result.final_score is None


def test_hard_hold_never_becomes_accept():
    icp = _icp(allowed_titles=("CMO",))
    result = score_lead(
        icp=icp,
        company_id="company-1",
        company_evidence=_company_evidence(),
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None,
        commercial_signals=[],
        person_id="person-1",
        person_evidence=[
            _evidence(EntityType.PERSON, "person-1", "company_association", "company-1", confidence=ConfidenceLevel.HIGH),
            _evidence(EntityType.PERSON, "person-1", "current_title", "CMO"),  # single, unconfirmed -> INSUFFICIENT -> HOLD
        ],
        person_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        now=NOW,
    )
    assert result.hard_icp_result == OverallResult.HOLD
    assert result.eligible_for_scoring is False
    assert result.final_score is None


def test_hard_pass_reaches_scoring():
    result = _passing_score()
    assert result.hard_icp_result == OverallResult.PASS
    assert result.eligible_for_scoring is True
    assert result.final_score is not None


def test_a_good_commercial_score_never_rescues_a_hard_fail():
    icp = _icp(min_employees=1000, business_models=("DTC",))
    result = score_lead(
        icp=icp,
        company_id="company-1",
        company_evidence=_company_evidence(),
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=BusinessModelClassificationResult(
            company_id="company-1",
            primary_model=BusinessModel.DTC,
            status=ClassificationStatus.CLASSIFIED,
            confidence=ConfidenceLevel.HIGH,
            explanation="clearly DTC",
        ),
        commercial_signals=[],
        person_id=None,
        person_evidence=[],
        person_resolutions=None,
        now=NOW,
    )
    assert result.commercial_score == 100.0  # component visible...
    assert result.final_score is None  # ...but never rescues the FAIL


# --- multi-ICP isolation --------------------------------------------------


def test_different_icps_produce_different_scores():
    icp_a = _icp(business_models=("DTC",))
    icp_b = _icp(business_models=("B2B",))
    business_model = BusinessModelClassificationResult(
        company_id="company-1",
        primary_model=BusinessModel.DTC,
        status=ClassificationStatus.CLASSIFIED,
        confidence=ConfidenceLevel.HIGH,
        explanation="clearly DTC",
    )
    result_a = _passing_score(icp=icp_a, business_model=business_model)
    result_b = _passing_score(icp=icp_b, business_model=business_model)
    assert result_a.commercial_score != result_b.commercial_score
    assert result_a.commercial_score == 100.0
    assert result_b.commercial_score == 0.0


# --- commercial score: only ICP-stated preferences matter -----------------


def test_commercial_preferences_affect_commercial_score():
    icp_with_pref = _icp(commercial_signals=("SUBSCRIPTION",))
    icp_without_pref = _icp()
    signals = [
        CommercialSignalResult(
            company_id="company-1", signal_type="SUBSCRIPTION", status=EvidenceStatus.SUPPORTED,
            confidence=ConfidenceLevel.MEDIUM, explanation="subscription mentioned twice",
        )
    ]
    with_pref = _passing_score(icp=icp_with_pref, commercial_signals=signals)
    without_pref = _passing_score(icp=icp_without_pref, commercial_signals=signals)
    assert with_pref.commercial_score == 100.0
    assert without_pref.commercial_score == 50.0  # ICP states no preference at all -> neutral


def test_unrelated_soft_preferences_do_not_affect_score():
    icp = _icp(commercial_signals=("FUNDING",))  # ICP cares about FUNDING only
    signals = [
        CommercialSignalResult(
            company_id="company-1", signal_type="SUBSCRIPTION", status=EvidenceStatus.SUPPORTED,
            confidence=ConfidenceLevel.MEDIUM, explanation="unrelated signal present",
        )
    ]
    result = _passing_score(icp=icp, commercial_signals=signals)
    # FUNDING preference has no matching signal observed -> scored 0, not
    # rescued by the unrelated SUBSCRIPTION signal being present.
    assert result.commercial_score == 0.0


def test_business_model_evidence_only_matters_when_icp_relevant():
    icp = _icp()  # no business-model preference at all
    business_model = BusinessModelClassificationResult(
        company_id="company-1", primary_model=BusinessModel.B2B, status=ClassificationStatus.CLASSIFIED,
        confidence=ConfidenceLevel.HIGH, explanation="clearly B2B",
    )
    result = _passing_score(icp=icp, business_model=business_model)
    assert result.commercial_score == 50.0  # neutral: ICP never asked


def test_missing_signal_is_not_treated_as_positive():
    icp = _icp(commercial_signals=("META_ADVERTISING",))
    result = _passing_score(icp=icp, commercial_signals=[])
    assert result.commercial_score == 0.0


def test_conflicting_signal_evidence_is_penalized_but_not_zeroed():
    icp = _icp(commercial_signals=("SUBSCRIPTION",))
    signals = [
        CommercialSignalResult(
            company_id="company-1", signal_type="SUBSCRIPTION", status=EvidenceStatus.CONFLICT,
            confidence=ConfidenceLevel.UNKNOWN, explanation="conflict",
        )
    ]
    result = _passing_score(icp=icp, commercial_signals=signals)
    assert result.commercial_score == 20.0


def test_unknown_vocabulary_preference_is_excluded_not_scored_as_zero():
    icp = _icp(commercial_signals=("NOT_A_REAL_SIGNAL",))
    result = _passing_score(icp=icp, commercial_signals=[])
    assert result.commercial_score == 50.0  # excluded entirely -> falls back to neutral


# --- evidence quality -----------------------------------------------------


def test_evidence_score_independent_of_other_components():
    icp = _icp(min_employees=1000)  # forces FAIL
    result = _passing_score(icp=icp)
    assert result.evidence_score > 0  # still computed even though ineligible


def test_revenue_range_evidence_measurably_improves_evidence_score():
    """Phase 7P: revenue_range is now a critical company field (see
    evidence_engine.py's COMPANY_CRITICAL_FIELDS) — a company with
    corroborated revenue_range evidence must score a strictly higher
    evidence_score (coverage) than an otherwise-identical company with
    none, since revenue_range now counts toward completeness like every
    other critical field."""
    icp = _icp()
    without_revenue = _company_evidence()
    with_revenue = _company_evidence() + [
        _evidence(EntityType.COMPANY, "company-1", "revenue_range", "1M-10M", source_provider_id="provider-a"),
        _evidence(EntityType.COMPANY, "company-1", "revenue_range", "1M-10M", source_provider_id="provider-b"),
    ]

    result_without = score_lead(
        icp=icp, company_id="company-1", company_evidence=without_revenue,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    result_with = score_lead(
        icp=icp, company_id="company-1", company_evidence=with_revenue,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert result_with.evidence_score > result_without.evidence_score


def test_duplicate_evidence_from_one_provider_is_not_double_counted_as_corroboration():
    single_provider = [
        _evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme Inc", source_provider_id="provider-a"),
        _evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme Inc", source_provider_id="provider-a"),
    ]
    two_providers = [
        _evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme Inc", source_provider_id="provider-a"),
        _evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme Inc", source_provider_id="provider-b"),
    ]
    icp = _icp()
    score_single = score_lead(
        icp=icp, company_id="company-1", company_evidence=single_provider,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    score_multi = score_lead(
        icp=icp, company_id="company-1", company_evidence=two_providers,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert score_multi.evidence_score > score_single.evidence_score


def test_conflicting_evidence_lowers_evidence_score():
    consistent = _company_evidence()
    conflicting = _company_evidence() + [
        _evidence(EntityType.COMPANY, "company-1", "industry", "Not Skincare At All", source_provider_id="provider-c")
    ]
    icp = _icp()
    consistent_result = score_lead(
        icp=icp, company_id="company-1", company_evidence=consistent,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    conflicting_result = score_lead(
        icp=icp, company_id="company-1", company_evidence=conflicting,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert conflicting_result.evidence_score < consistent_result.evidence_score


# --- freshness -------------------------------------------------------------


def test_freshness_none_when_no_evidence_at_all():
    icp = _icp(min_employees=1000)
    result = score_lead(
        icp=icp, company_id="company-1", company_evidence=[],
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert result.freshness_score is None


def test_freshness_decays_with_age():
    fresh = [_evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme", retrieved_at=NOW)]
    stale = [_evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme", retrieved_at=NOW - timedelta(days=365))]
    icp = _icp(min_employees=1000)
    fresh_result = score_lead(
        icp=icp, company_id="company-1", company_evidence=fresh,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    stale_result = score_lead(
        icp=icp, company_id="company-1", company_evidence=stale,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert fresh_result.freshness_score == 100.0
    assert stale_result.freshness_score == 0.0


def test_freshness_is_configurable():
    evidence = [_evidence(EntityType.COMPANY, "company-1", "company_identity", "Acme", retrieved_at=NOW - timedelta(days=10))]
    icp = _icp(min_employees=1000)
    strict_config = FreshnessConfig(full_credit_within_days=1, zero_credit_after_days=5)
    result = score_lead(
        icp=icp, company_id="company-1", company_evidence=evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW, freshness_config=strict_config,
    )
    assert result.freshness_score == 0.0  # 10 days is beyond a 5-day zero-credit cutoff


def test_missing_freshness_weight_is_redistributed_not_penalized():
    icp = _icp()
    no_evidence_score = score_lead(
        icp=icp, company_id="company-1", company_evidence=[],
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    # employee_count/industry/etc are unset with no evidence -> HOLD, not eligible.
    # Use a permissive ICP with no hard rules configured so PASS is reached
    # even with zero evidence, to isolate the freshness redistribution.
    permissive_icp = CanonicalICP(
        icp_id="icp-permissive", version=1,
        hard_rules=CanonicalHardRules(),
        soft_preferences=CanonicalSoftPreferences(),
    )
    result = score_lead(
        icp=permissive_icp, company_id="company-1", company_evidence=[],
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert result.eligible_for_scoring is True
    assert result.freshness_score is None
    assert result.final_score is not None  # weight redistributed, not treated as zero


# --- identity confidence ----------------------------------------------------


def test_identity_confidence_independent_component():
    icp = _icp(min_employees=1000)  # ineligible, but identity still computed
    result = score_lead(
        icp=icp, company_id="company-1", company_evidence=_company_evidence(),
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert result.identity_confidence == 100.0


def test_identity_conflict_reduces_confidence():
    icp = _icp()
    result = score_lead(
        icp=icp, company_id="company-1", company_evidence=_company_evidence(),
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH", has_conflict=True)],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert result.identity_confidence == 40.0


def test_no_resolution_history_falls_back_to_neutral_identity():
    icp = _icp()
    result = score_lead(
        icp=icp, company_id="company-1", company_evidence=_company_evidence(),
        company_resolutions=[],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert result.identity_confidence == 50.0


# --- weighting --------------------------------------------------------------


def test_configurable_weights_change_final_score():
    icp = _icp(commercial_signals=("FUNDING",))
    default_result = _passing_score(icp=icp, commercial_signals=[])
    heavy_commercial = ScoringWeights(icp_weight=0.05, commercial_weight=0.85, evidence_weight=0.05, freshness_weight=0.03, identity_weight=0.02)
    reweighted_result = _passing_score(icp=icp, commercial_signals=[], weights=heavy_commercial)
    assert default_result.final_score != reweighted_result.final_score


def test_invalid_weights_raise_value_error():
    with pytest.raises(ValueError):
        ScoringWeights(icp_weight=0.5, commercial_weight=0.5, evidence_weight=0.5, freshness_weight=0.0, identity_weight=0.0)


def test_invalid_freshness_config_raises_value_error():
    with pytest.raises(ValueError):
        FreshnessConfig(full_credit_within_days=100, zero_credit_after_days=50)


# --- determinism and bounds -------------------------------------------------


def test_scoring_is_deterministic():
    icp = _icp()
    evidence = _company_evidence()  # fixed evidence (fixed ids) shared across both calls
    resolutions = [ResolutionSignal(status="MATCH", confidence="HIGH")]
    first = score_lead(
        icp=icp, company_id="company-1", company_evidence=evidence, company_resolutions=resolutions,
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    second = score_lead(
        icp=icp, company_id="company-1", company_evidence=evidence, company_resolutions=resolutions,
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    assert first == second


def test_all_scores_are_within_bounds():
    icp = _icp()
    result = _passing_score(icp=icp)
    for value in (result.icp_score, result.commercial_score, result.evidence_score, result.identity_confidence):
        assert 0.0 <= value <= 100.0
    if result.freshness_score is not None:
        assert 0.0 <= result.freshness_score <= 100.0
    if result.final_score is not None:
        assert 0.0 <= result.final_score <= 100.0


# --- no manager feedback / LLM / provider involvement -----------------------


def test_score_lead_signature_has_no_manager_feedback_or_llm_parameter():
    import inspect

    params = set(inspect.signature(score_lead).parameters.keys())
    assert "manager_feedback" not in params
    assert "feedback" not in params
    assert "llm" not in params


def test_no_provider_or_llm_imports_in_module():
    import inspect

    import app.services.lead_scoring as module

    source = inspect.getsource(module)
    for forbidden in ("ProviderAdapter", "openai", "anthropic", "requests.post"):
        assert forbidden not in source


# --- no hard-rule weakening --------------------------------------------------


def test_scoring_never_reevaluates_hard_rules_itself():
    import inspect

    import app.services.lead_scoring as module

    source = inspect.getsource(module)
    assert "evaluate_hard_rules" not in source  # only validate_against_icp (Phase 12) is called
    assert "validate_against_icp" in source
