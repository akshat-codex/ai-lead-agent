"""Phase 16 — compact LLM context builder.

Turns real Phase 2 (ICP), Phase 11 (evidence), Phase 12 (hard-rule
validation), Phase 13 (business model), Phase 14 (commercial signals), and
Phase 15 (scores) results into the compact QualificationContext the LLM
actually sees. This module never re-derives any of those results — it only
selects and compresses fields that already exist, so the "single source of
truth" for every fact stays exactly where it always was.

Token budget discipline: only evidence backing a hard-rule field, or
otherwise material to business-model/commercial-signal classification, is
included — full evidence dumps and raw source URLs are deliberately
omitted. See _select_evidence for exactly what qualifies.
"""
from __future__ import annotations

import json

from app.schemas.business_model import BusinessModelClassificationResult
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.hard_rule_result import HardRuleEvaluation
from app.schemas.llm_qualification import EvidenceBrief, QualificationContext, RuleResultBrief
from app.schemas.scoring import LeadScoreResult
from app.services.evidence_engine import summarize_entity

_MAX_EVIDENCE_ITEMS = 40  # a hard ceiling so a pathological evidence volume can never blow up the prompt

# Must match app/services/evidence_import.py::INDUSTRY_MATCH_PROVENANCE_KEY
# exactly — same JSON key Phase 11's hard_icp_validation.py bridge already
# reads from the same evidence_text field, reused here rather than a
# second, competing provenance mechanism.
_INDUSTRY_MATCH_PROVENANCE_KEY = "industry_match"

# Phase 13B — must match
# app/services/evidence_import.py::KEYWORD_MATCH_PROVENANCE_KEY exactly.
_KEYWORD_MATCH_PROVENANCE_KEY = "keyword_match"


def _discovery_match_type(company_evidence: list[EvidenceRecord]) -> str:
    """Phase 12 — classifies a company's discovery provenance from its own
    already-collected industry evidence, without any new provider call or
    DB query: "structured" when at least one industry evidence record
    carries the Phase 11 industry_match_branch tag (a real, live-verified
    linkedin_category/naics_category exact match); "keyword_fallback" when
    industry evidence exists but none of it carries that tag (Explorium's
    website_keywords substring-fallback tier, or a non-Explorium source);
    "unknown" when there is no industry evidence at all to judge from.

    Deliberately reads ALL industry records for this company (not just the
    first/SUPPORTED one _bridged_industry_terms uses) — a company with
    even one structured sighting among several is treated as "structured"
    here, since the point of this classification is "is there a trustworthy
    structured signal available," not "did the hard rule itself resolve
    via one" (those are different, related questions Phase 11 and Phase 12
    each need answered for their own purpose)."""
    industry_records = [r for r in company_evidence if r.field == "industry"]
    if not industry_records:
        return "unknown"

    for record in industry_records:
        if not record.evidence_text:
            continue
        try:
            parsed = json.loads(record.evidence_text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get(_INDUSTRY_MATCH_PROVENANCE_KEY), dict):
            return "structured"
    return "keyword_fallback"


def _discovery_keyword_terms(company_evidence: list[EvidenceRecord]) -> tuple[str, ...]:
    """Phase 13B — reads back the exact website_keywords term(s) tagged by
    app/providers/explorium.py's keyword branch (via
    app/services/evidence_import.py::_industry_match_provenance's
    keyword_match_terms handling), for surfacing to the LLM verification
    prompt (see QualificationContext.discovery_keyword_terms's own
    comment). Returns an empty tuple whenever no industry evidence record
    carries the tag — including every "structured" and "unknown"
    discovery_match_type, and every context built before Phase 13B."""
    industry_records = [r for r in company_evidence if r.field == "industry"]
    for record in industry_records:
        if not record.evidence_text:
            continue
        try:
            parsed = json.loads(record.evidence_text)
        except (ValueError, TypeError):
            continue
        match_info = parsed.get(_KEYWORD_MATCH_PROVENANCE_KEY) if isinstance(parsed, dict) else None
        if isinstance(match_info, dict):
            terms = match_info.get("terms")
            if isinstance(terms, list):
                return tuple(str(t) for t in terms)
    return ()


def _discovery_keyword_term_sources(company_evidence: list[EvidenceRecord]) -> tuple[str, ...]:
    """Phase 13D — parallel to _discovery_keyword_terms, positionally
    aligned with it: for each term in discovery_keyword_terms, whether it
    came from an unmatched ICP industry term or a company_type term (see
    app/providers/explorium.py's own keyword_match_term_sources comment
    for why this distinction matters — a company_type match like "D2C"
    makes a different claim than an industry match like "Entertainment").
    Returns an empty tuple whenever the source breakdown was never
    supplied (every pre-Phase-13D evidence record) — this is a strict
    addition, never required for _discovery_keyword_terms to keep
    working exactly as it already does."""
    industry_records = [r for r in company_evidence if r.field == "industry"]
    for record in industry_records:
        if not record.evidence_text:
            continue
        try:
            parsed = json.loads(record.evidence_text)
        except (ValueError, TypeError):
            continue
        match_info = parsed.get(_KEYWORD_MATCH_PROVENANCE_KEY) if isinstance(parsed, dict) else None
        if isinstance(match_info, dict):
            sources = match_info.get("term_sources")
            if isinstance(sources, list):
                return tuple(str(s) for s in sources)
    return ()


def _discovery_structured_match_scope(company_evidence: list[EvidenceRecord]) -> str:
    """Phase 15 (Phase 14 audit finding) — reads back
    app/providers/explorium.py's industry_match_scope tag: "single_category"
    when exactly one taxonomy value fed the structured branch that matched
    this candidate (Explorium's own total_results is then safely
    attributable and also surfaced, see
    _discovery_structured_match_total_results below); "multi_category"
    when more than one did (Explorium's own OR-combination makes
    total_results non-attributable to any single category — the CONFIRMED
    COMMON case in real-world ICPs, per the Phase 14 audit); "unknown" for
    every keyword-fallback candidate, every context built before Phase 15,
    and any structured match whose evidence_text doesn't carry the tag for
    any other reason.

    DELIBERATELY NOT a "broad" vs "strong" classification — see
    app/services/llm_qualification.py's Phase 15 gate comment (unchanged
    by this phase) for why: there is currently no reliable, deterministic
    way to say a "multi_category" match is worse than a "single_category"
    one; this only records what IS and ISN'T knowable about how the match
    was produced. Phase 12's gate condition is UNCHANGED by this function's
    existence — it is observability only."""
    industry_records = [r for r in company_evidence if r.field == "industry"]
    for record in industry_records:
        if not record.evidence_text:
            continue
        try:
            parsed = json.loads(record.evidence_text)
        except (ValueError, TypeError):
            continue
        match_info = parsed.get(_INDUSTRY_MATCH_PROVENANCE_KEY) if isinstance(parsed, dict) else None
        if isinstance(match_info, dict):
            scope = match_info.get("match_scope")
            if isinstance(scope, str):
                return scope
    return "unknown"


def _discovery_structured_match_total_results(company_evidence: list[EvidenceRecord]) -> int | None:
    """Phase 15 — the raw Explorium total_results count, ONLY ever present
    when _discovery_structured_match_scope returns "single_category" (see
    app/providers/explorium.py's own attribution guard — this function
    never re-derives or guesses the value, only reads back what that
    module already decided was safe to surface). None for every other
    case, including every "multi_category" and "keyword_fallback" match."""
    industry_records = [r for r in company_evidence if r.field == "industry"]
    for record in industry_records:
        if not record.evidence_text:
            continue
        try:
            parsed = json.loads(record.evidence_text)
        except (ValueError, TypeError):
            continue
        match_info = parsed.get(_INDUSTRY_MATCH_PROVENANCE_KEY) if isinstance(parsed, dict) else None
        if isinstance(match_info, dict):
            total_results = match_info.get("branch_total_results")
            if isinstance(total_results, int):
                return total_results
    return None


def _employee_range_label(icp: CanonicalICP) -> str:
    r = icp.hard_rules.employee_range
    if r.min is None and r.max is None:
        return "not constrained"
    return f"{r.min if r.min is not None else '0'}-{r.max if r.max is not None else 'unbounded'}"


def _soft_preference_terms(icp: CanonicalICP) -> tuple[str, ...]:
    """Phase 4 (AI/UX + live-safety audit) fix — root cause: this function
    read business_models/commercial_signals/growth_signals/marketing_signals
    but never sp.custom_preferences, so any soft preference that reached
    the ICP only via app/services/filter_projection.py's own "recognized-
    but-not-yet-field-backed" fallback (funding, hiring, technology,
    revenue, website — see that module's own comment) was silently inert:
    correctly captured from the user's NL description, correctly stored on
    the canonical ICP, but never actually seen by the LLM qualification
    prompt or app/services/discovery_strategy.py's own term expansion. A
    user typing "recently raised Series A funding" would have that intent
    vanish at this exact point, with no error and no signal it happened.

    custom_preferences entries are CanonicalCustomRule (label + description
    pairs), a different shape from the plain string tuples above — rendered
    as "label: description" so the LLM sees BOTH the filter's own label
    (e.g. "funding") and its actual value (e.g. "operator eq: value
    'Recently raised'") rather than losing one half."""
    sp = icp.soft_preferences
    custom_terms = tuple(f"{c.label}: {c.description}" for c in sp.custom_preferences)
    return tuple(
        dict.fromkeys((*sp.business_models, *sp.commercial_signals, *sp.growth_signals, *sp.marketing_signals, *custom_terms))
    )


def _select_evidence(
    company_evidence: list[EvidenceRecord],
    person_evidence: list[EvidenceRecord],
) -> tuple[EvidenceBrief, ...]:
    """Selects the evidence worth sending: one entry per distinct
    (entity, field, value) — corroborating duplicates of the exact same
    value are collapsed to their first occurrence rather than sent
    repeatedly, keeping the prompt compact without hiding a genuinely
    distinct fact or a genuine conflict (differing values for one field
    are NOT collapsed — see the seen-key below)."""
    briefs: list[EvidenceBrief] = []
    seen: set[tuple[str, str, str]] = set()
    for record in (*company_evidence, *person_evidence):
        value_str = str(record.value)
        key = (record.entity_id, record.field, value_str)
        if key in seen:
            continue
        seen.add(key)
        briefs.append(
            EvidenceBrief(id=record.id, field=record.field, value=value_str[:300], confidence=record.confidence.value)
        )
        if len(briefs) >= _MAX_EVIDENCE_ITEMS:
            break
    return tuple(briefs)


def _conflicting_and_missing_fields(
    entity_type: EntityType, entity_id: str, records: list[EvidenceRecord]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    summary = summarize_entity(entity_type, entity_id, records)
    conflicting = tuple(f.field for f in summary.fields if f.status == EvidenceStatus.CONFLICT)
    missing = tuple(f.field for f in summary.fields if f.status == EvidenceStatus.UNKNOWN)
    return conflicting, missing


def build_qualification_context(
    icp: CanonicalICP,
    company_id: str,
    company_evidence: list[EvidenceRecord],
    hard_rule_evaluation: HardRuleEvaluation,
    business_model: BusinessModelClassificationResult | None,
    commercial_signals: list[CommercialSignalResult],
    score: LeadScoreResult,
    person_id: str | None = None,
    person_evidence: list[EvidenceRecord] | None = None,
) -> QualificationContext:
    person_evidence = person_evidence or []

    company_conflicts, company_missing = _conflicting_and_missing_fields(EntityType.COMPANY, company_id, company_evidence)
    if person_id is not None:
        person_conflicts, person_missing = _conflicting_and_missing_fields(EntityType.PERSON, person_id, person_evidence)
    else:
        person_conflicts, person_missing = (), ()

    business_model_summary = None
    if business_model is not None:
        models = ", ".join(m.value for m in (business_model.primary_model, *business_model.secondary_models))
        business_model_summary = f"{models} ({business_model.status.value}, confidence={business_model.confidence.value})"

    signal_summaries = tuple(
        f"{s.signal_type}: {s.status.value}" for s in commercial_signals
    )

    return QualificationContext(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        icp_industries=icp.hard_rules.industries,
        icp_geography=tuple(c.label for c in icp.hard_rules.geography.countries) + icp.hard_rules.geography.unrecognized,
        icp_employee_range=_employee_range_label(icp),
        icp_allowed_titles=icp.hard_rules.allowed_titles,
        icp_company_types=icp.hard_rules.company_types,
        icp_exclusions=icp.hard_rules.exclusions,
        icp_soft_preferences=_soft_preference_terms(icp),
        company_id=company_id,
        person_id=person_id,
        hard_rule_result=hard_rule_evaluation.overall_result,
        rule_results=tuple(
            RuleResultBrief(rule=r.rule, status=r.status.value, reason_code=r.reason_code.value if r.reason_code else None)
            for r in hard_rule_evaluation.rule_results
        ),
        reason_codes=hard_rule_evaluation.reason_codes,
        discovery_match_type=_discovery_match_type(company_evidence),
        discovery_keyword_terms=_discovery_keyword_terms(company_evidence),
        discovery_keyword_term_sources=_discovery_keyword_term_sources(company_evidence),
        discovery_structured_match_scope=_discovery_structured_match_scope(company_evidence),
        discovery_structured_match_total_results=_discovery_structured_match_total_results(company_evidence),
        business_model_summary=business_model_summary,
        commercial_signal_summary=signal_summaries,
        icp_score=score.icp_score,
        commercial_score=score.commercial_score,
        evidence_score=score.evidence_score,
        freshness_score=score.freshness_score,
        identity_confidence=score.identity_confidence,
        final_score=score.final_score,
        evidence=_select_evidence(company_evidence, person_evidence),
        conflicting_fields=tuple(dict.fromkeys((*company_conflicts, *person_conflicts))),
        missing_critical_fields=tuple(dict.fromkeys((*company_missing, *person_missing))),
    )
