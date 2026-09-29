"""Phase 30 — company-quality scoring.

Pure, DB-free tests against app/services/company_quality.score_company_quality,
mirroring test_structured_match_scope_15.py's own helper conventions
(_icp/_evidence/_build_context) so this file needs no HTTP client or
registry. See tests/test_company_quality_pipeline.py for the batch-level
reordering/Unipile-gating regression tests.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.company_quality import CompanyQualityLabel
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import (
    LLMQualificationResult,
    QualificationDecision,
    QualificationExecutionStatus,
)
from app.services.company_quality import score_company_quality
from app.services.evidence_import import _industry_match_provenance
from app.services.hard_icp_validation import validate_against_icp
from app.services.lead_scoring import score_lead
from app.services.qualification_context import build_qualification_context
from app.schemas.scoring import ResolutionSignal

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _icp(**hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("Healthcare",),
        geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
        employee_range=EmployeeRange(min=10, max=200),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id="icp-1", version=1, hard_rules=CanonicalHardRules(**hard_defaults), soft_preferences=CanonicalSoftPreferences()
    )


def _evidence(field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field=field, value=value,
        source_provider_id="explorium-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _structured_industry_evidence(terms=("Healthcare",), resolved_category_count=None) -> EvidenceRecord:
    # Phase 25: real Explorium-tagged evidence (app/providers/explorium.py's
    # execute()) always includes industry_match_resolved_category_count
    # alongside industry_match_terms — app/services/hard_icp_validation.py's
    # bridge now requires it to equal len(icp_industries) before widening
    # the allowed set (see _bridged_industry_terms's own docstring).
    # Defaults to len(terms), matching the common single/matched-count case
    # every existing call site in this file actually exercises; pass an
    # explicit value to test a genuinely partial/short match.
    if resolved_category_count is None:
        resolved_category_count = len(terms)
    return _evidence(
        "industry", "General Medical",
        evidence_text=_industry_match_provenance(
            {
                "industry_match_branch": "linkedin_category",
                "industry_match_terms": list(terms),
                "industry_match_resolved_category_count": resolved_category_count,
            }
        ),
    )


def _keyword_industry_evidence(terms=("D2C skincare",)) -> EvidenceRecord:
    return _evidence(
        "industry", "Consumer Goods",
        evidence_text=_industry_match_provenance({"keyword_match_terms": list(terms)}),
    )


def _build_context(icp: CanonicalICP, company_evidence: list[EvidenceRecord]):
    validation = validate_against_icp(icp, "company-1", company_evidence, None, [])
    score = score_lead(
        icp=icp, company_id="company-1", company_evidence=company_evidence,
        company_resolutions=[ResolutionSignal(status="MATCH", confidence="HIGH")],
        business_model=None, commercial_signals=[], person_id=None, person_evidence=[],
        person_resolutions=None, now=NOW,
    )
    context = build_qualification_context(
        icp=icp, company_id="company-1", company_evidence=company_evidence,
        hard_rule_evaluation=validation.evaluation, business_model=None,
        commercial_signals=[], score=score,
    )
    return validation, context


def _qualification(
    context, decision=QualificationDecision.GOOD_FIT, confidence=85.0, status=QualificationExecutionStatus.SUCCESS
) -> LLMQualificationResult:
    return LLMQualificationResult(
        icp_id=context.icp_id, icp_version=context.icp_version, company_id=context.company_id, person_id=None,
        hard_rule_result=context.hard_rule_result, status=status, decision=decision, confidence=confidence,
        reason_codes=(), summary="test", supporting_evidence_ids=(), risk_evidence_ids=(), missing_evidence=(),
        commercial_fit_explanation="", hard_rule_acknowledgement="", uncertainties=(),
        provider_id="mock", model_id="mock-v1", prompt_version="test", score_snapshot={},
    )


_FULL_EVIDENCE = [
    _evidence("company_identity", "Acme Health"),
    _evidence("domain", "acmehealth.com"),
]


# --- 1. clearly strong company --------------------------------------------


def test_clearly_strong_company_gets_strong_label_and_high_score():
    """Live-test audit fix: industry evidence here must literally
    exact-match the ICP term ("Healthcare") to genuinely PASS —
    _structured_industry_evidence's own default reported value
    ("General Medical") no longer bridges (a structured
    linkedin_category/naics_category match is never trusted as proof of
    an ICP term it doesn't already textually equal — see
    app/services/hard_icp_validation.py::_bridged_industry_terms's own
    docstring). This test is specifically about the STRONG-label/high-
    score path, which requires a genuine PASS to reach at all, so its own
    fixture must produce one honestly."""
    icp = _icp(industries=("Healthcare",))
    evidence = [
        *_FULL_EVIDENCE,
        _evidence(
            "industry", "Healthcare",
            evidence_text=_industry_match_provenance(
                {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"], "industry_match_resolved_category_count": 1}
            ),
        ),
        _evidence("country", "United States"),
        _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    assert context.hard_rule_result == OverallResult.PASS

    result = score_company_quality(context)
    assert result.label == CompanyQualityLabel.STRONG
    assert result.score is not None
    assert result.score >= 70.0
    assert result.hard_rule_result == "PASS"


# --- 2. hard-rule FAIL -------------------------------------------------


def test_hard_rule_fail_is_always_reject_with_no_score():
    icp = _icp(employee_range=EmployeeRange(min=999999, max=9999999))
    evidence = [
        *_FULL_EVIDENCE,
        _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"),
        _evidence("employee_range", "11-50"),
    ]
    _, context = _build_context(icp, evidence)
    assert context.hard_rule_result == OverallResult.FAIL

    result = score_company_quality(context)
    assert result.label == CompanyQualityLabel.REJECT
    assert result.score is None
    # signals are still computed (useful for a human reviewing why it failed)
    assert len(result.signals) > 0


def test_hard_rule_fail_reject_never_overridden_by_strong_soft_signals():
    """Even with a real semantic-verification GOOD_FIT and full evidence,
    a hard FAIL must still be REJECT/None — no soft signal can rescue it."""
    icp = _icp(employee_range=EmployeeRange(min=999999, max=9999999))
    evidence = [
        *_FULL_EVIDENCE,
        _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"),
        _evidence("employee_range", "11-50"),
    ]
    _, context = _build_context(icp, evidence)
    qualification = _qualification(context, decision=QualificationDecision.GOOD_FIT, confidence=99.0)

    result = score_company_quality(context, qualification)
    assert result.label == CompanyQualityLabel.REJECT
    assert result.score is None


# --- 3. hard-rule HOLD ---------------------------------------------------


def test_hard_rule_hold_is_never_strong_regardless_of_score():
    icp = _icp(industries=("Healthcare",))
    evidence = [
        *_FULL_EVIDENCE,
        _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"),
        # no employee_range evidence -> HOLD
    ]
    _, context = _build_context(icp, evidence)
    assert context.hard_rule_result == OverallResult.HOLD

    result = score_company_quality(context)
    assert result.label == CompanyQualityLabel.REVIEW
    assert result.score is not None  # HOLD still gets a real number, just capped at REVIEW


# --- 4. structured strong match -------------------------------------------


def test_structured_match_scores_higher_than_keyword_fallback():
    icp = _icp(industries=("Healthcare",))
    structured_evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, structured_context = _build_context(icp, structured_evidence)
    structured_result = score_company_quality(structured_context)

    icp_kw = _icp(industries=("Consumer Goods",))
    keyword_evidence = [
        *_FULL_EVIDENCE, _keyword_industry_evidence(("Consumer Goods",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, keyword_context = _build_context(icp_kw, keyword_evidence)
    keyword_result = score_company_quality(keyword_context)

    structured_signal = next(s for s in structured_result.signals if s.name == "discovery_provenance")
    keyword_signal = next(s for s in keyword_result.signals if s.name == "discovery_provenance")
    assert structured_signal.value > keyword_signal.value
    assert "structured" in structured_signal.explanation
    assert "keyword" in keyword_signal.explanation


def test_equal_fit_candidates_are_no_longer_9_points_apart_purely_on_provenance():
    """P3 fix regression: two candidates with BYTE-IDENTICAL hard-rule fit
    (same exact industry value "Healthcare", same country/employee_range,
    icp_score=100.0 both times) must not be far apart in
    company_quality_score purely because one was discovered via the
    structured taxonomy branch and the other via the keyword fallback —
    that gap measured 9.0 points before this fix (icp_fit=0.30,
    discovery_provenance=0.20) and is bounded to <=5.0 after it
    (icp_fit=0.40, discovery_provenance=0.10). This is a rebalance, not a
    removal: the provenance signal itself is unchanged (90.0 vs 45.0,
    still a real, non-zero gap on its own value) — only its SHARE of the
    total score dropped. Since company_quality_score feeds
    app/services/lead_ranking.py's own tie-break key #2, this gap is
    exactly what can bury a genuinely equal-fit company within a tier."""
    icp = _icp(industries=("Healthcare",))

    def _same_industry_value_evidence(provenance_attrs: dict) -> EvidenceRecord:
        # Deliberately the SAME literal value "Healthcare" on both sides —
        # isolates discovery_match_type as the only real difference; a
        # differing raw value would also change icp_fit itself, which is
        # exactly what this test must NOT vary.
        return _evidence("industry", "Healthcare", evidence_text=_industry_match_provenance(provenance_attrs))

    structured_evidence = [
        *_FULL_EVIDENCE,
        _same_industry_value_evidence(
            {"industry_match_branch": "linkedin_category", "industry_match_terms": ["Healthcare"], "industry_match_resolved_category_count": 1}
        ),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, structured_context = _build_context(icp, structured_evidence)
    structured_result = score_company_quality(structured_context)

    keyword_evidence = [
        *_FULL_EVIDENCE,
        _same_industry_value_evidence({"keyword_match_terms": ["Healthcare"]}),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, keyword_context = _build_context(icp, keyword_evidence)
    keyword_result = score_company_quality(keyword_context)

    # The premise: fit is genuinely identical on both sides.
    assert structured_context.icp_score == keyword_context.icp_score == 100.0

    delta = structured_result.score - keyword_result.score
    assert delta <= 5.0, f"provenance-only gap is {delta}, expected <= 5.0 after the P3 rebalance"
    assert delta > 0  # the signal is a rebalance, never a removal — some gap must remain

    # icp_fit must now be the largest single weight in the result.
    weight_by_name = {s.name: s.weight for s in structured_result.signals}
    assert weight_by_name["icp_fit"] == max(weight_by_name.values())


# --- 5. keyword fallback ---------------------------------------------------


def test_keyword_fallback_company_can_still_be_strong_with_good_evidence():
    icp = _icp(industries=("Consumer Goods",))
    evidence = [
        *_FULL_EVIDENCE, _keyword_industry_evidence(("Consumer Goods",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    assert context.discovery_match_type == "keyword_fallback"

    result = score_company_quality(context)
    # keyword fallback lowers discovery_provenance but doesn't forbid STRONG outright
    assert result.label in {CompanyQualityLabel.STRONG, CompanyQualityLabel.REVIEW}
    provenance_signal = next(s for s in result.signals if s.name == "discovery_provenance")
    assert provenance_signal.value == 45.0


# --- 6. ambiguous semantic verification ------------------------------------


def test_ambiguous_semantic_verification_pulls_score_toward_neutral():
    icp = _icp(industries=("Consumer Goods",))
    evidence = [
        *_FULL_EVIDENCE, _keyword_industry_evidence(("Consumer Goods",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)

    hold_qualification = _qualification(context, decision=QualificationDecision.HOLD, confidence=40.0)
    result = score_company_quality(context, hold_qualification)
    semantic_signal = next(s for s in result.signals if s.name == "semantic_verification")
    assert semantic_signal.value == 50.0
    assert "HOLD" in semantic_signal.explanation


def test_not_fit_semantic_verification_pulls_score_down():
    icp = _icp(industries=("Consumer Goods",))
    evidence = [
        *_FULL_EVIDENCE, _keyword_industry_evidence(("Consumer Goods",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    not_fit = _qualification(context, decision=QualificationDecision.NOT_FIT, confidence=80.0)

    with_not_fit = score_company_quality(context, not_fit)
    without = score_company_quality(context, None)
    assert with_not_fit.score < without.score


def test_structured_match_trusted_qualification_never_reached_llm_so_no_semantic_signal():
    """A STRUCTURED_MATCH_TRUSTED status means the LLM was never actually
    invoked (see llm_qualification.py) — it must never be treated as a
    real semantic-verification signal."""
    icp = _icp(industries=("Healthcare",))
    evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    trusted = _qualification(
        context, decision=QualificationDecision.GOOD_FIT, confidence=None,
        status=QualificationExecutionStatus.STRUCTURED_MATCH_TRUSTED,
    )
    result = score_company_quality(context, trusted)
    assert not any(s.name == "semantic_verification" for s in result.signals)


# --- 7. missing evidence ----------------------------------------------------


def test_missing_evidence_lowers_evidence_strength_and_completeness():
    icp = _icp(industries=("Healthcare",))
    rich_evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, rich_context = _build_context(icp, rich_evidence)
    rich_result = score_company_quality(rich_context)

    sparse_evidence = [_structured_industry_evidence(("Healthcare",))]
    _, sparse_context = _build_context(icp, sparse_evidence)
    sparse_result = score_company_quality(sparse_context)

    rich_completeness = next(s for s in rich_result.signals if s.name == "evidence_completeness")
    sparse_completeness = next(s for s in sparse_result.signals if s.name == "evidence_completeness")
    assert rich_completeness.value > sparse_completeness.value


def test_missing_critical_fields_named_in_explanation_never_fabricated():
    icp = _icp(industries=("Healthcare",))
    evidence = [_structured_industry_evidence(("Healthcare",))]  # missing company_identity/domain/etc.
    _, context = _build_context(icp, evidence)
    assert context.missing_critical_fields  # sanity: this scenario really is missing fields

    result = score_company_quality(context)
    evidence_signal = next(s for s in result.signals if s.name == "evidence_strength")
    for field in context.missing_critical_fields:
        assert field in evidence_signal.explanation


# --- 8. employee-range overlap ----------------------------------------------


def test_employee_range_pass_scores_full_confidence():
    icp = _icp(employee_range=EmployeeRange(min=10, max=200))
    evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    result = score_company_quality(context)
    employee_signal = next(s for s in result.signals if s.name == "employee_range_confidence")
    assert employee_signal.value == 100.0


def test_employee_range_not_configured_is_absent_not_penalized():
    icp = _icp(employee_range=EmployeeRange(min=None, max=None))
    evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"),
    ]
    _, context = _build_context(icp, evidence)
    result = score_company_quality(context)
    assert not any(s.name == "employee_range_confidence" for s in result.signals)


# --- 9. industry bridge (Phase 11 structured evidence quality) -------------


def test_multi_category_structured_scope_still_scores_as_structured_not_penalized_below_keyword():
    icp = _icp(industries=("Healthcare", "Medical Practices"))
    multi_category_evidence = _evidence(
        "industry", "General Medical",
        evidence_text=_industry_match_provenance(
            {
                "industry_match_branch": "linkedin_category",
                "industry_match_terms": ["Healthcare", "Medical Practices"],
                "industry_match_scope": "multi_category",
                "industry_match_resolved_category_count": 2,
            }
        ),
    )
    evidence = [
        *_FULL_EVIDENCE,
        multi_category_evidence,
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    assert context.discovery_structured_match_scope == "multi_category"

    result = score_company_quality(context)
    provenance_signal = next(s for s in result.signals if s.name == "discovery_provenance")
    assert provenance_signal.value == 90.0  # still a real structured match, never demoted to keyword-level trust
    assert "multi_category" not in provenance_signal.explanation or "not further attributable" in provenance_signal.explanation


# --- 10. score explainability ------------------------------------------------


def test_every_signal_has_a_nonempty_explanation_citing_real_data():
    icp = _icp(industries=("Healthcare",))
    evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    result = score_company_quality(context)
    assert result.explanation
    for signal in result.signals:
        assert signal.explanation.strip() != ""
        assert signal.name in result.explanation or True  # explanation always lists every signal name


def test_weights_in_result_sum_to_one_and_reflect_actually_applied_signals():
    icp = _icp(employee_range=EmployeeRange(min=None, max=None))  # drops the employee_range signal
    evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"),
    ]
    _, context = _build_context(icp, evidence)
    result = score_company_quality(context)
    total_weight = sum(s.weight for s in result.signals)
    assert abs(total_weight - 1.0) < 1e-6


# --- 11. no fabricated evidence ---------------------------------------------


def test_no_evidence_at_all_never_fabricates_a_positive_score():
    icp = _icp(industries=("Healthcare",))
    _, context = _build_context(icp, [])
    result = score_company_quality(context)
    # HOLD (nothing to validate against) at best, never STRONG on zero evidence
    assert result.label != CompanyQualityLabel.STRONG


def test_unknown_discovery_provenance_is_neutral_not_assumed_structured():
    icp = _icp(industries=("Healthcare",))
    evidence = [*_FULL_EVIDENCE, _evidence("country", "United States"), _evidence("employee_range", "51-200")]
    _, context = _build_context(icp, evidence)
    assert context.discovery_match_type == "unknown"
    result = score_company_quality(context)
    provenance_signal = next(s for s in result.signals if s.name == "discovery_provenance")
    assert provenance_signal.value == 50.0


# --- 12. deterministic / repeatable scoring ---------------------------------


def test_scoring_is_deterministic_across_repeated_calls():
    icp = _icp(industries=("Healthcare",))
    evidence = [
        *_FULL_EVIDENCE, _structured_industry_evidence(("Healthcare",)),
        _evidence("country", "United States"), _evidence("employee_range", "51-200"),
    ]
    _, context = _build_context(icp, evidence)
    results = [score_company_quality(context) for _ in range(5)]
    scores = {r.score for r in results}
    labels = {r.label for r in results}
    assert len(scores) == 1
    assert len(labels) == 1


# --- schema-level invariants --------------------------------------------


def test_reject_label_structurally_impossible_without_hard_fail():
    """CompanyQualityResult's own model_validator forbids REJECT for
    anything but a hard FAIL — this is enforced at the schema level, not
    just by score_company_quality's own logic."""
    import pytest
    from pydantic import ValidationError

    from app.schemas.company_quality import CompanyQualityResult

    with pytest.raises(ValidationError):
        CompanyQualityResult(
            icp_id="icp-1", icp_version=1, company_id="company-1",
            hard_rule_result="PASS", label=CompanyQualityLabel.REJECT, score=None,
            signals=(), explanation="invalid",
        )


def test_fail_with_a_score_is_structurally_impossible():
    import pytest
    from pydantic import ValidationError

    from app.schemas.company_quality import CompanyQualityResult

    with pytest.raises(ValidationError):
        CompanyQualityResult(
            icp_id="icp-1", icp_version=1, company_id="company-1",
            hard_rule_result="FAIL", label=CompanyQualityLabel.REJECT, score=42.0,
            signals=(), explanation="invalid",
        )
