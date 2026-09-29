"""Phase 30 — company-quality scoring.

The smallest safe layer between deterministic hard-rule qualification
(Phase 11/12) + Phase 15 scoring + selective Phase 12 semantic
verification (Phase 18) and Unipile/person discovery (Phase 7B).

WHY THIS EXISTS: app/services/batch_orchestration.py used to call Unipile
(via start_people_discovery_run) for every resolved company BEFORE hard
validation or scoring ever ran — including companies that would later hard
-FAIL. This module is the deterministic gate that now runs first, so a
person-discovery call is only ever spent on a company that has already
cleared (or is at least not hard-rejected by) the existing, unchanged hard
rules.

THIS IS NOT A SECOND QUALIFICATION SYSTEM. It never calls an LLM, never
re-evaluates a hard rule, and never re-derives a score component — every
CompanyQualitySignal is a direct, cited read of a value Phase 11/12/15/18
already computed (via the exact same QualificationContext Phase 16's own
qualify_lead() and Phase 17's adversarial review already consume — see
app/services/qualification_context.py). If a phase never produced a
signal (no verification ran, no score exists yet), that signal is simply
absent from CompanyQualityResult.signals rather than guessed or defaulted
to a neutral value — see _signals_for_context below for exactly which
signals are conditionally included and why.

SCORING, NOT RE-GATING: hard_rule_result is read verbatim from the
QualificationContext and is NEVER overridden by this module. A FAIL always
yields label=REJECT and score=None (mirroring Phase 15's own
"final_score is None unless PASS" contract in app/schemas/scoring.py) — no
combination of strong soft signals can move a hard FAIL out of REJECT. A
HOLD is always at most REVIEW, regardless of score, since a HOLD candidate
is, by hard-rule definition, still missing something the deterministic
rules require — this module treats that as informative for triage, never
as something a soft signal can silently resolve.

DETERMINISTIC AND EXPLAINABLE: given the same QualificationContext (plus
the same optional verification/qualification inputs), score_company_quality
always returns byte-identical output — no randomness, no wall-clock
dependency, no provider call. Every signal's `explanation` names the exact
underlying fact it was read from, so a REVIEW or STRONG label is always
traceable back to real, already-persisted evidence — never an opaque
number.
"""
from __future__ import annotations

from app.schemas.company_quality import CompanyQualityLabel, CompanyQualityResult, CompanyQualitySignal
from app.schemas.hard_rule_result import OverallResult, RuleStatus
from app.schemas.llm_qualification import LLMQualificationResult, QualificationContext, QualificationExecutionStatus

_NEUTRAL_UNKNOWN_VALUE = 50.0  # an explicit "no evidence either way" midpoint — never a guess at true quality


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _icp_fit_signal(context: QualificationContext) -> CompanyQualitySignal:
    """Direct readout of Phase 15's own icp_score (share of configured
    hard rules that PASS) — the single most authoritative signal available,
    since it summarizes the SAME deterministic rules the hard gate itself
    already ran, not a soft re-interpretation of them.

    P3 fix — weight raised 0.30 -> 0.40 (moved from discovery_provenance,
    see that signal's own comment): a controlled comparison (two
    candidates with byte-identical hard-rule fit, differing ONLY in
    discovery_match_type) showed a real, reproducible 9.0-point
    company_quality_score gap purely from HOW the company was found —
    see tests/test_company_quality.py::
    test_equal_fit_candidates_are_no_longer_9_points_apart_purely_on_provenance
    for the exact before/after numbers. Since company_quality_score feeds
    ranking's own tie-break key (app/services/lead_ranking.py, unchanged),
    that gap could push a genuinely equal-fit company below a weaker one
    within the same tier. icp_fit is the one signal that most directly
    answers "does this company match the ICP" — raising its share is the
    most direct way to make fit the dominant factor without removing the
    provenance signal itself."""
    configured = [r for r in context.rule_results if r.status != RuleStatus.NOT_APPLICABLE.value]
    passed = sum(1 for r in configured if r.status == RuleStatus.PASS.value)
    return CompanyQualitySignal(
        name="icp_fit",
        value=_clamp(context.icp_score),
        weight=0.40,
        explanation=(
            f"{passed}/{len(configured)} configured hard rules PASS (icp_score={context.icp_score:.1f})"
            if configured
            else "No hard rules were configured for this ICP."
        ),
    )


def _discovery_provenance_signal(context: QualificationContext) -> CompanyQualitySignal:
    """Structured-vs-keyword discovery signal (Phase 11/13B). A real,
    live-verified linkedin_category/naics_category taxonomy match is
    scored higher than a website_keywords substring fallback, which is
    scored higher than having no industry evidence to judge from at all —
    this ordering is the same trust ordering Phase 12's own selective-
    verification gate already encodes (STRUCTURED_MATCH_TRUSTED skips the
    LLM precisely because a structured match is the most reliable
    discovery signal available), just expressed as a score component here
    instead of a call/no-call decision.

    P3 fix — weight lowered 0.20 -> 0.10 (moved to _icp_fit_signal, see
    that function's own comment for the measured before/after numbers).
    This is a REBALANCE, never a removal: a keyword-fallback candidate
    still scores meaningfully lower than a structured one on this signal
    (45.0 vs 90.0, unchanged) — provenance still counts, it just no
    longer competes with icp_fit for being the single largest factor in
    company_quality_score."""
    if context.discovery_match_type == "structured":
        value = 90.0
        detail = "structured Explorium taxonomy match (linkedin_category/naics_category)"
        if context.discovery_structured_match_scope == "single_category":
            detail += " — single resolved category"
        elif context.discovery_structured_match_scope == "multi_category":
            detail += " — multiple resolved categories combined (Phase 15: not further attributable)"
    elif context.discovery_match_type == "keyword_fallback":
        value = 45.0
        terms = ", ".join(context.discovery_keyword_terms) or "unspecified term(s)"
        detail = f"keyword (website_keywords) fallback match on: {terms}"
    else:
        value = _NEUTRAL_UNKNOWN_VALUE
        detail = "no industry evidence available to judge discovery provenance"
    return CompanyQualitySignal(name="discovery_provenance", value=_clamp(value), weight=0.10, explanation=detail)


def _evidence_strength_signal(context: QualificationContext) -> CompanyQualitySignal:
    """Phase 15's own evidence_score (critical-field coverage, genuine
    multi-provider corroboration, unresolved conflicts) plus a direct
    penalty for THIS context's own missing_critical_fields/
    conflicting_fields — never re-computed, only read and, where relevant,
    explained with the specific field names rather than a bare number."""
    penalty_detail = ""
    if context.missing_critical_fields:
        penalty_detail += f"; missing: {', '.join(context.missing_critical_fields)}"
    if context.conflicting_fields:
        penalty_detail += f"; conflicting: {', '.join(context.conflicting_fields)}"
    return CompanyQualitySignal(
        name="evidence_strength",
        value=_clamp(context.evidence_score),
        weight=0.20,
        explanation=f"evidence_score={context.evidence_score:.1f}{penalty_detail}" if penalty_detail else f"evidence_score={context.evidence_score:.1f}",
    )


def _employee_range_confidence_signal(context: QualificationContext) -> CompanyQualitySignal | None:
    """Only included when the ICP actually configures an employee_range
    rule (a NOT_APPLICABLE rule carries no fit information to score) — a
    company is never rewarded or penalized on a constraint the ICP itself
    never asked about. PASS/FAIL/HOLD read verbatim from the rule Phase
    11's own trust bridge already evaluated; nothing here re-derives
    employee-count trust."""
    employee_rule = next((r for r in context.rule_results if r.rule == "employee_range"), None)
    if employee_rule is None or employee_rule.status == RuleStatus.NOT_APPLICABLE.value:
        return None
    if employee_rule.status == RuleStatus.PASS.value:
        value, detail = 100.0, "employee_range rule PASS"
    elif employee_rule.status == RuleStatus.HOLD.value:
        value, detail = _NEUTRAL_UNKNOWN_VALUE, f"employee_range rule HOLD ({employee_rule.reason_code or 'no reason code'})"
    else:
        value, detail = 0.0, f"employee_range rule FAIL ({employee_rule.reason_code or 'no reason code'})"
    return CompanyQualitySignal(name="employee_range_confidence", value=value, weight=0.10, explanation=detail)


def _geography_fit_signal(context: QualificationContext) -> CompanyQualitySignal | None:
    """Same NOT_APPLICABLE-skip contract as employee-range above, for the
    geography rule."""
    geo_rule = next((r for r in context.rule_results if r.rule == "geography"), None)
    if geo_rule is None or geo_rule.status == RuleStatus.NOT_APPLICABLE.value:
        return None
    if geo_rule.status == RuleStatus.PASS.value:
        value, detail = 100.0, "geography rule PASS"
    elif geo_rule.status == RuleStatus.HOLD.value:
        value, detail = _NEUTRAL_UNKNOWN_VALUE, f"geography rule HOLD ({geo_rule.reason_code or 'no reason code'})"
    else:
        value, detail = 0.0, f"geography rule FAIL ({geo_rule.reason_code or 'no reason code'})"
    return CompanyQualitySignal(name="geographic_fit", value=value, weight=0.10, explanation=detail)


def _company_type_confidence_signal(context: QualificationContext) -> CompanyQualitySignal | None:
    """Only included when the ICP configures company_types AND evidence
    exists to judge the rule from (a configured-but-NOT_APPLICABLE rule —
    e.g. no company_type evidence collected at all — still carries no fit
    information, exactly like employee-range/geography above)."""
    company_type_rule = next((r for r in context.rule_results if r.rule == "company_type"), None)
    if company_type_rule is None or company_type_rule.status == RuleStatus.NOT_APPLICABLE.value:
        return None
    if company_type_rule.status == RuleStatus.PASS.value:
        value, detail = 100.0, "company_type rule PASS"
    elif company_type_rule.status == RuleStatus.HOLD.value:
        value, detail = _NEUTRAL_UNKNOWN_VALUE, f"company_type rule HOLD ({company_type_rule.reason_code or 'no reason code'})"
    else:
        value, detail = 0.0, f"company_type rule FAIL ({company_type_rule.reason_code or 'no reason code'})"
    return CompanyQualitySignal(name="company_type_confidence", value=value, weight=0.05, explanation=detail)


def _semantic_verification_signal(
    context: QualificationContext,
    qualification: LLMQualificationResult | None,
) -> CompanyQualitySignal | None:
    """Only included when Phase 12's selective verification actually ran a
    live LLM call for this candidate (status == SUCCESS — never
    STRUCTURED_MATCH_TRUSTED, which by definition skipped the LLM and thus
    has nothing semantic to report here; the discovery_provenance signal
    above already credits that trust). Reads the qualification decision
    verbatim — GOOD_FIT/WEAK_FIT/NOT_FIT/HOLD map directly to a value, and
    the qualification's own confidence (when the provider returned one) is
    blended in as a tie-break within that mapping rather than trusted
    alone, since a low-confidence GOOD_FIT is a genuinely different signal
    from a high-confidence one."""
    if qualification is None:
        return None
    status = qualification.status.value if hasattr(qualification.status, "value") else qualification.status
    if status != QualificationExecutionStatus.SUCCESS.value:
        return None
    decision = qualification.decision.value if hasattr(qualification.decision, "value") else qualification.decision
    confidence = qualification.confidence if qualification.confidence is not None else _NEUTRAL_UNKNOWN_VALUE
    if decision == "GOOD_FIT":
        value = _clamp(60.0 + 0.4 * confidence)
    elif decision == "WEAK_FIT":
        value = _clamp(30.0 + 0.3 * confidence)
    elif decision == "HOLD":
        value = _NEUTRAL_UNKNOWN_VALUE
    else:  # NOT_FIT
        value = _clamp(20.0 - 0.2 * confidence)
    return CompanyQualitySignal(
        name="semantic_verification",
        value=value,
        weight=0.15,
        explanation=f"LLM qualification decision={decision}, confidence={confidence:.1f}",
    )


def _evidence_completeness_signal(context: QualificationContext) -> CompanyQualitySignal:
    """How many of the compact evidence briefs actually sent to the LLM
    context exist at all — a coarse but honest "is there anything here to
    judge from" signal, independent of whether any individual field is
    critical. Always included (never conditionally skipped), since
    "how much evidence exists" is always a knowable fact regardless of ICP
    configuration."""
    count = len(context.evidence)
    # Saturates at 10 distinct evidence items — chosen because
    # _MAX_EVIDENCE_ITEMS in qualification_context.py caps the context at
    # 40, and 10 distinct (entity, field, value) facts is already a company
    # with real multi-field coverage; this is a floor/ceiling for the
    # score curve, not a claim about what "enough" evidence means for any
    # specific ICP.
    value = _clamp(100.0 * min(count, 10) / 10)
    return CompanyQualitySignal(
        name="evidence_completeness", value=value, weight=0.10, explanation=f"{count} distinct evidence item(s) available"
    )


def _signals_for_context(
    context: QualificationContext,
    qualification: LLMQualificationResult | None,
) -> tuple[CompanyQualitySignal, ...]:
    candidates = (
        _icp_fit_signal(context),
        _discovery_provenance_signal(context),
        _evidence_strength_signal(context),
        _employee_range_confidence_signal(context),
        _geography_fit_signal(context),
        _company_type_confidence_signal(context),
        _semantic_verification_signal(context, qualification),
        _evidence_completeness_signal(context),
    )
    return tuple(s for s in candidates if s is not None)


def _weighted_score(signals: tuple[CompanyQualitySignal, ...]) -> tuple[float, tuple[CompanyQualitySignal, ...]]:
    """Re-normalizes weights over only the signals actually present (a
    company missing a company_type rule is never penalized for a signal
    that was never applicable to it) and returns both the final score and
    the signals with their weight field updated to the value ACTUALLY
    applied — so CompanyQualityResult.signals is always self-consistent
    with the score it explains, never a static configuration that doesn't
    match what was computed."""
    total_weight = sum(s.weight for s in signals)
    if total_weight <= 0:
        return _NEUTRAL_UNKNOWN_VALUE, signals
    normalized = tuple(s.model_copy(update={"weight": s.weight / total_weight}) for s in signals)
    score = sum(s.value * s.weight for s in normalized)
    return _clamp(score), normalized


def _label_for(hard_rule_result: str, score: float | None) -> CompanyQualityLabel:
    if hard_rule_result == OverallResult.FAIL.value:
        return CompanyQualityLabel.REJECT
    if hard_rule_result == OverallResult.HOLD.value:
        return CompanyQualityLabel.REVIEW
    # PASS: STRONG only above a real, fixed threshold — otherwise REVIEW,
    # never REJECT (a hard PASS can never be downgraded to REJECT by a
    # soft score; see this module's own docstring).
    assert score is not None  # guaranteed by score_company_quality's own contract for a non-FAIL result
    return CompanyQualityLabel.STRONG if score >= 70.0 else CompanyQualityLabel.REVIEW


def score_company_quality(
    context: QualificationContext,
    qualification: LLMQualificationResult | None = None,
) -> CompanyQualityResult:
    """Pure and deterministic: identical inputs always produce identical
    output, no side effects, no provider call. `qualification` is optional
    because, in the reordered batch pipeline (see
    app/services/batch_orchestration.py), this gate now runs BEFORE Phase
    16's own LLM qualification — most calls will have qualification=None,
    and the semantic_verification signal is simply absent from the result
    in that case rather than guessed. Passing a completed qualification
    (e.g. for a re-score after the fact, or via the standalone
    /api/v1/company-quality endpoint) enriches the result with that signal
    but never changes any OTHER signal's value.
    """
    hard_rule_result = context.hard_rule_result.value if hasattr(context.hard_rule_result, "value") else context.hard_rule_result

    if hard_rule_result == OverallResult.FAIL.value:
        # Mirrors Phase 15's own "no score for FAIL" contract exactly —
        # signals are still computed and returned (useful for a human
        # reviewing WHY it failed), but score/label are never anything but
        # None/REJECT for a hard FAIL, unconditionally.
        signals = _signals_for_context(context, qualification)
        return CompanyQualityResult(
            icp_id=context.icp_id,
            icp_version=context.icp_version,
            company_id=context.company_id,
            hard_rule_result=hard_rule_result,
            label=CompanyQualityLabel.REJECT,
            score=None,
            signals=signals,
            explanation="Hard-rule result is FAIL — no company-quality score is computed; REJECT is authoritative and cannot be overridden by any soft signal.",
        )

    signals = _signals_for_context(context, qualification)
    score, normalized_signals = _weighted_score(signals)
    label = _label_for(hard_rule_result, score)

    reason = ", ".join(f"{s.name}={s.value:.0f}(w={s.weight:.2f})" for s in normalized_signals)
    explanation = f"Hard-rule result is {hard_rule_result}; weighted quality score={score:.1f} -> {label.value}. Signals: {reason}."

    return CompanyQualityResult(
        icp_id=context.icp_id,
        icp_version=context.icp_version,
        company_id=context.company_id,
        hard_rule_result=hard_rule_result,
        label=label,
        score=score,
        signals=normalized_signals,
        explanation=explanation,
    )
