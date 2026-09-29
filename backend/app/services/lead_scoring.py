"""Phase 15 — Adaptive Lead Scoring.

Six independently-inspectable components, computed by reusing every prior
phase's existing engine unchanged:

  * hard_icp_result / eligibility — Phase 12's validate_against_icp(),
    called here exactly as anywhere else. This module never re-implements
    a single hard rule.
  * icp_score — a diagnostic "how much of the ICP's configured hard rule
    surface actually passed" percentage, derived only from Phase 12's
    already-computed rule_results. It is informational, never a gate: it
    is computed and shown even when hard_icp_result is FAIL or HOLD, but
    a good icp_score can never override that result — see score_lead().
  * commercial_score — how well the company's Phase 13 business-model
    classification and Phase 14 commercial signals match *this specific
    ICP's* stated soft preferences. A preference the ICP doesn't state is
    never scored; a signal the company has that the ICP doesn't ask about
    never affects this number either way.
  * evidence_score — general evidence quality/coverage, independent of
    any ICP preference: critical-field coverage, genuine multi-provider
    corroboration (never duplicate-record corroboration from one
    provider), unresolved conflicts, and whether identity/business-model/
    commercial-signal evidence exists at all.
  * freshness_score — how recent the newest evidence is, against a
    caller-supplied, explicitly configurable decay curve. None (not a
    fabricated number) when there is no evidence to date at all.
  * identity_confidence — reused directly from Phase 7/10's own
    resolution history; never a second identity-resolution system.

final_score is a weighted average of the five component scores (freshness
excluded and its weight redistributed if it is None) — but is only ever a
number when hard_icp_result is PASS. It is None for FAIL and HOLD alike,
so nothing downstream can read "scored 82" as license to accept a HOLD or
rescue a FAIL. Manager feedback is never read here — there is no
parameter for it.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from app.schemas.business_model import BusinessModelClassificationResult, ClassificationStatus
from app.schemas.canonical_icp import CanonicalICP, CanonicalSoftPreferences
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.hard_rule_result import OverallResult, RuleStatus
from app.schemas.scoring import (
    DEFAULT_FRESHNESS_CONFIG,
    DEFAULT_SCORING_WEIGHTS,
    FreshnessConfig,
    LeadScoreResult,
    ResolutionSignal,
    ScoringWeights,
)
from app.services.evidence_engine import critical_fields_for, group_by_field, summarize_entity
from app.services.hard_icp_validation import validate_against_icp

_NEUTRAL_SCORE = 50.0


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


# --- ICP fit score --------------------------------------------------------


def _compute_icp_score(rule_results) -> float:
    """The share of the ICP's *configured* hard rules (i.e. not
    NOT_APPLICABLE) that PASS. Purely a readout of Phase 12's own
    rule_results — never a second evaluation of any rule."""
    configured = [r for r in rule_results if r.status != RuleStatus.NOT_APPLICABLE]
    if not configured:
        return 100.0  # nothing was configured, so nothing was left unsatisfied
    passed = sum(1 for r in configured if r.status == RuleStatus.PASS)
    return _clamp(100.0 * passed / len(configured))


# --- commercial fit score -------------------------------------------------


def _normalize_preference(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return cleaned.upper()


def _preference_terms(soft_preferences: CanonicalSoftPreferences) -> list[str]:
    return [
        *soft_preferences.business_models,
        *soft_preferences.commercial_signals,
        *soft_preferences.growth_signals,
        *soft_preferences.marketing_signals,
    ]


def _compute_commercial_score(
    soft_preferences: CanonicalSoftPreferences,
    business_model: BusinessModelClassificationResult | None,
    signals: list[CommercialSignalResult],
) -> float:
    """Scores only the preferences this specific ICP actually states — a
    preference is never rewarded or penalized unless the ICP names it, and
    evidence the ICP never asked about never moves this number. A
    preference with no corresponding entry anywhere in our structured
    business-model/signal vocabulary is excluded from scoring entirely
    (a vocabulary gap is not evidence about the company); one that *is*
    in our vocabulary but unsupported by evidence counts as a scored zero.
    """
    signals_by_type = {s.signal_type: s for s in signals}
    known_models = {business_model.primary_model.value, *(m.value for m in business_model.secondary_models)} if business_model else set()

    terms = [_normalize_preference(t) for t in _preference_terms(soft_preferences)]
    terms = [t for t in terms if t]
    if not terms:
        return _NEUTRAL_SCORE  # ICP states no commercial preference at all -> no opinion, not a penalty

    scored: list[float] = []
    for term in terms:
        if business_model is not None and term in {m.value for m in _all_business_models()}:
            if business_model.status == ClassificationStatus.CLASSIFIED and term in known_models:
                scored.append(100.0)
            elif business_model.status == ClassificationStatus.CLASSIFIED:
                scored.append(0.0)  # classified as something else -> this preference isn't met
            elif business_model.status == ClassificationStatus.CONFLICTING_EVIDENCE:
                scored.append(20.0)
            else:
                scored.append(0.0)
            continue

        signal = signals_by_type.get(term)
        if signal is not None:
            if signal.status == EvidenceStatus.SUPPORTED:
                scored.append(100.0)
            elif signal.status == EvidenceStatus.INSUFFICIENT:
                scored.append(55.0)
            elif signal.status == EvidenceStatus.CONFLICT:
                scored.append(20.0)
            continue

        if term in _known_signal_types():
            scored.append(0.0)  # a real, known signal the ICP wants, but none was ever observed
        # else: no structural counterpart for this preference at all -> excluded, not scored

    if not scored:
        return _NEUTRAL_SCORE
    return _clamp(sum(scored) / len(scored))


def _all_business_models():
    from app.schemas.business_model import BusinessModel

    return list(BusinessModel)


def _known_signal_types() -> frozenset[str]:
    from app.services.commercial_signal_extractor import SIGNAL_DEFINITIONS

    return frozenset(SIGNAL_DEFINITIONS.keys())


# --- evidence quality score ------------------------------------------------


def _compute_evidence_score(
    company_id: str,
    company_evidence: list[EvidenceRecord],
    person_id: str | None,
    person_evidence: list[EvidenceRecord],
    business_model: BusinessModelClassificationResult | None,
    signals: list[CommercialSignalResult],
) -> float:
    company_summary = summarize_entity(EntityType.COMPANY, company_id, company_evidence)
    coverage_scores = [company_summary.completeness * 100.0]

    all_fields = list(company_summary.fields)
    if person_id is not None:
        person_summary = summarize_entity(EntityType.PERSON, person_id, person_evidence)
        coverage_scores.append(person_summary.completeness * 100.0)
        all_fields += list(person_summary.fields)

    coverage_score = sum(coverage_scores) / len(coverage_scores)

    fields_with_evidence = [f for f in all_fields if f.records]
    if fields_with_evidence:
        conflict_free_score = 100.0 * sum(1 for f in fields_with_evidence if f.status != EvidenceStatus.CONFLICT) / len(
            fields_with_evidence
        )
        # Genuine independent corroboration only — a field with two
        # records from the *same* provider is not two independent sources.
        corroborated = sum(
            1 for f in fields_with_evidence if len({r.source_provider_id for r in f.records if r.source_provider_id}) >= 2
        )
        corroboration_score = 100.0 * corroborated / len(fields_with_evidence)
    else:
        conflict_free_score = 100.0
        corroboration_score = 0.0

    grouped = group_by_field(company_evidence)
    identity_present = bool(grouped.get("company_identity") or grouped.get("domain"))
    if person_id is not None:
        person_grouped = group_by_field(person_evidence)
        identity_present = identity_present or bool(person_grouped.get("person_identity"))
    identity_score = 100.0 if identity_present else 0.0

    business_model_score = 100.0 if business_model is not None and business_model.status == ClassificationStatus.CLASSIFIED else (
        30.0 if business_model is not None else 0.0
    )
    signal_score = 100.0 if signals else 0.0

    components = [coverage_score, conflict_free_score, corroboration_score, identity_score, business_model_score, signal_score]
    return _clamp(sum(components) / len(components))


# --- freshness score --------------------------------------------------


def _as_naive_utc(value: datetime) -> datetime:
    """Evidence timestamps may come back timezone-naive after a SQLite
    round-trip while `now` is caller-supplied and typically aware — both
    are treated as UTC and compared as naive so neither source needs to
    guess the other's tzinfo convention."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _compute_freshness_score(
    evidence_records: list[EvidenceRecord],
    now: datetime,
    config: FreshnessConfig,
) -> float | None:
    if not evidence_records:
        return None  # nothing to date at all -> honestly unknown, never fabricated
    most_recent = max(_as_naive_utc(r.retrieved_at) for r in evidence_records)
    age_days = max(0.0, (_as_naive_utc(now) - most_recent).total_seconds() / 86400.0)
    if age_days <= config.full_credit_within_days:
        return 100.0
    if age_days >= config.zero_credit_after_days:
        return 0.0
    span = config.zero_credit_after_days - config.full_credit_within_days
    return _clamp(100.0 * (1 - (age_days - config.full_credit_within_days) / span))


# --- identity confidence -----------------------------------------------


def _score_resolution_signals(signals: list[ResolutionSignal]) -> float | None:
    if not signals:
        return None
    if any(s.has_conflict for s in signals):
        return 40.0
    if any(s.confidence == "HIGH" for s in signals):
        return 100.0
    if any(s.status == "NEW" for s in signals):
        return 75.0
    return _NEUTRAL_SCORE


def _compute_identity_confidence(
    company_resolutions: list[ResolutionSignal],
    person_resolutions: list[ResolutionSignal] | None,
) -> float:
    company_score = _score_resolution_signals(company_resolutions)
    person_score = _score_resolution_signals(person_resolutions) if person_resolutions is not None else None
    scores = [s for s in (company_score, person_score) if s is not None]
    if not scores:
        return _NEUTRAL_SCORE
    return _clamp(sum(scores) / len(scores))


# --- final weighted score -----------------------------------------------


def _weighted_final(
    icp_score: float,
    commercial_score: float,
    evidence_score: float,
    freshness_score: float | None,
    identity_confidence: float,
    weights: ScoringWeights,
) -> float:
    components = [
        (weights.icp_weight, icp_score),
        (weights.commercial_weight, commercial_score),
        (weights.evidence_weight, evidence_score),
        (weights.identity_weight, identity_confidence),
    ]
    if freshness_score is not None:
        components.append((weights.freshness_weight, freshness_score))

    total_weight = sum(w for w, _ in components)
    if total_weight <= 0:
        return _NEUTRAL_SCORE
    return _clamp(sum(w * s for w, s in components) / total_weight)


# --- orchestrator -----------------------------------------------------


def score_lead(
    icp: CanonicalICP,
    company_id: str,
    company_evidence: list[EvidenceRecord],
    company_resolutions: list[ResolutionSignal],
    business_model: BusinessModelClassificationResult | None,
    commercial_signals: list[CommercialSignalResult],
    person_id: str | None,
    person_evidence: list[EvidenceRecord],
    person_resolutions: list[ResolutionSignal] | None,
    now: datetime,
    weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
    freshness_config: FreshnessConfig = DEFAULT_FRESHNESS_CONFIG,
) -> LeadScoreResult:
    """Pure and deterministic: identical inputs (including `now`, which
    must be supplied explicitly rather than read from the wall clock, so
    freshness scoring stays reproducible in tests and audits) always
    produce an identical result.
    """
    validation = validate_against_icp(icp, company_id, company_evidence, person_id, person_evidence)
    evaluation = validation.evaluation

    icp_score = _compute_icp_score(evaluation.rule_results)
    commercial_score = _compute_commercial_score(icp.soft_preferences, business_model, commercial_signals)
    evidence_score = _compute_evidence_score(
        company_id, company_evidence, person_id, person_evidence, business_model, commercial_signals
    )
    freshness_score = _compute_freshness_score(company_evidence + person_evidence, now, freshness_config)
    identity_confidence = _compute_identity_confidence(company_resolutions, person_resolutions)

    eligible = evaluation.overall_result == OverallResult.PASS
    final_score = (
        _weighted_final(icp_score, commercial_score, evidence_score, freshness_score, identity_confidence, weights)
        if eligible
        else None
    )

    evidence_ids = tuple(dict.fromkeys(ids for rule_ids in validation.evidence_ids.values() for ids in rule_ids))

    if eligible:
        explanation = f"Hard ICP PASS; final score {final_score:.1f} from icp={icp_score:.1f}, commercial={commercial_score:.1f}, evidence={evidence_score:.1f}, freshness={freshness_score}, identity={identity_confidence:.1f}."
    else:
        explanation = f"Hard ICP {evaluation.overall_result.value} — scoring withheld ({evaluation.explanation})."

    return LeadScoreResult(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        company_id=company_id,
        person_id=person_id,
        hard_icp_result=evaluation.overall_result,
        eligible_for_scoring=eligible,
        icp_score=icp_score,
        commercial_score=commercial_score,
        evidence_score=evidence_score,
        freshness_score=freshness_score,
        identity_confidence=identity_confidence,
        final_score=final_score,
        weights=weights,
        evidence_ids=evidence_ids,
        reason_codes=evaluation.reason_codes,
        explanation=explanation,
    )
