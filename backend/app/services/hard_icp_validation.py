"""Phase 12 — Evidence-Backed Hard ICP Validation.

Connects real canonical company/person data (Phases 7/10) and Phase 11
evidence to the UNCHANGED Phase 3 Hard ICP Rule Engine
(app/services/hard_rule_engine.py). This module never duplicates or
redesigns Phase 3's rule logic — evaluate_hard_rules() is called here
exactly as it is anywhere else.

Phase 12's only job is building the evidence-gated Candidate that engine
expects: a hard-rule field is populated from an evidence value ONLY when
that field's Phase 11 status is SUPPORTED. CONFLICT, INSUFFICIENT, and
UNKNOWN all leave the field unset — which Phase 3 already turns into HOLD
entirely on its own; no new HOLD logic is added here, and no value is ever
guessed to fill a gap.

Soft preferences are never read anywhere in this module (CanonicalICP's
soft_preferences is never even accessed), matching the same guarantee
Phase 3 itself makes.

Live-test audit fix (2026-09-03): _bridged_industry_terms (Phase 11) used
to widen the industry allowed-set passed to evaluate_hard_rules using a
candidate's own structurally-resolved taxonomy match — retired after
investigation confirmed that trust was never provable with real Explorium
data (a resolved linkedin_category/naics_category VALUE and the
candidate's reported naics_description TEXT live in unrelated
identifier/label spaces with no shared lookup this codebase has or should
invent — see that function's own docstring for the full story). Phase 3
itself remains completely unmodified either way — it always did, and
still does, only its own exact-match check; only what reached it as
`candidate.industry` ever changed. An unverifiable structured match now
suppresses `industry` to unknown (see
_industry_has_unprovable_compound_match) so Phase 3 HOLDs on it, rather
than being (wrongly) widened into a false PASS.

Person handling deliberately does not treat a single Phase 9 discovery
sighting as a verified title: with today's providers a lone sighting
always carries ConfidenceLevel.UNKNOWN, and Phase 11's status rule already
requires either a stated MEDIUM/HIGH confidence or two independent,
agreeing sources to reach SUPPORTED — so an unconfirmed title naturally
stays INSUFFICIENT and HOLDs, with no special-casing required here. The one
thing this module does add: a person's title is only read at all once
their "company_association" evidence confirms *this* company — otherwise
the title is treated as unknown for this specific validation, exactly per
the task's "current title/company association cannot be sufficiently
supported -> HOLD" rule.
"""
from __future__ import annotations

import json

from app.schemas.candidate import Candidate
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus, FieldEvidenceSummary
from app.schemas.hard_icp_validation import HardIcpValidationResult
from app.services.evidence_engine import summarize_entity
from app.services.hard_rule_engine import evaluate_hard_rules
from app.services.icp_normalization import clean_text

_ELIGIBLE_STATUSES = (EvidenceStatus.SUPPORTED, EvidenceStatus.SUPPORTED_STRUCTURED)

# Must match app/services/evidence_import.py::INDUSTRY_MATCH_PROVENANCE_KEY
# exactly — this module reads back the same JSON key that module writes.
_INDUSTRY_MATCH_PROVENANCE_KEY = "industry_match"

# Phase 37 — must match app/services/evidence_import.py::
# KEYWORD_MATCH_PROVENANCE_KEY exactly. Used only by
# _cross_branch_corroborated_terms below; never by _bridged_industry_terms
# or _industry_has_unprovable_compound_match, which remain scoped to
# structured-only provenance exactly as before this phase.
_KEYWORD_MATCH_PROVENANCE_KEY = "keyword_match"


def _effective_required_industry_terms(
    icp_industries: tuple[str, ...], icp_combination_terms: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Phase 39 — compound-ICP recall fix.

    app/services/discovery_strategy.py's AI term-expansion deliberately
    proposes COMBINATION phrases for a compound ICP (e.g. "Healthcare" +
    "SaaS" -> also propose "Healthcare Software", "Clinical Software" —
    see that module's own _SYSTEM_INSTRUCTIONS) and reports them
    separately, as strategy.combination_industry_terms — never mixed into
    strategy.industry_terms (independent synonyms of ONE term). Phase 39's
    merge_strategy_into_hard_rules merges combination terms into the SAME
    flat icp.hard_rules.industries tuple the user's own terms live in
    (Explorium still tries them through the ordinary structured/naics/
    keyword cascade, completely unchanged) but ALSO separately records
    them into CanonicalHardRules.industry_combination_terms, so the
    distinction is NOT lost the way it silently was before Phase 39 (when
    an earlier attempt at this fix tried to reconstruct "is this a
    combination phrase" from text shape alone — e.g. checking whether one
    term textually contains another — and was confirmed, by live-testing
    against the prompt's own real example outputs like "Clinical
    Software," to miss the common case entirely, since a real combination
    phrase need not literally repeat either original word).

    Every bridging/corroboration check in this module (_bridged_industry_terms,
    _industry_has_unprovable_compound_match, _cross_branch_corroborated_terms)
    counts len(icp_industries) as the number of INDEPENDENT terms a
    candidate must structurally prove. Left unfixed, a company whose
    evidence structurally, exactly resolves the AI's own "Healthcare
    Software"/"Clinical Software" phrase (the single best possible
    discovery signal for this ICP) can never satisfy
    resolved_count == len(icp_industries), since the merged tuple also
    separately counts "Healthcare" and "SaaS" as requirements — confirmed
    live: this HOLDs today instead of PASSing.

    This function returns the reduced "effective" required term set: every
    term in `icp_combination_terms` is dropped from the count (a genuine,
    AI-declared compound-intersection phrase already covers the intent
    those terms exist to express), leaving only the terms the ICP itself —
    user-typed, or AI-proposed as an independent synonym — still requires
    proof for. A term list with no combination terms recorded (the common
    case: independent/alternative industries, a single-term ICP, or any
    pre-Phase-39 caller that never populated
    CanonicalHardRules.industry_combination_terms at all) is returned
    completely unchanged — this is a strict, additive extension."""
    if not icp_combination_terms:
        return icp_industries
    combination_keys = {clean_text(t).lower() for t in icp_combination_terms}
    return tuple(t for t in icp_industries if clean_text(t).lower() not in combination_keys)


def _supported_value_and_evidence(
    field_summaries: dict[str, FieldEvidenceSummary],
    field: str,
):
    """SUPPORTED_STRUCTURED is included alongside SUPPORTED — it means a
    single, honestly-UNKNOWN-confidence record from a provider this
    codebase has explicitly named as a trusted structured-data source for
    this field (see evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS/
    _TRUSTED_STRUCTURED_FIELDS). An arbitrary, untrusted single-source
    sighting still never reaches either status and still HOLDs, exactly as
    before — nothing here lowers the bar for evidence in general, only for
    the specific fields/providers that allowlist names."""
    summary = field_summaries.get(field)
    if summary is None or summary.status not in _ELIGIBLE_STATUSES:
        return None, ()
    return summary.records[0].value, tuple(record.id for record in summary.records)


def _bridged_industry_terms(
    field_summaries: dict[str, FieldEvidenceSummary],
    icp_industries: tuple[str, ...],
    icp_combination_terms: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """RETIRED, always returns () — kept as a stable no-op function (never
    deleted outright) because app/services/hard_icp_validation.py's own
    call site and several sibling functions' docstrings still refer to it
    by name for historical context; removing the symbol entirely would be
    a bigger, unrelated diff than this fix calls for.

    Live-test audit finding (2026-09-03), corrected after a deeper
    investigation (see the investigation report accompanying this fix):
    this function used to widen the industry hard rule's allowed-values
    set with a candidate's own reported "industry" evidence value
    whenever that value came from a structured Explorium taxonomy branch
    (linkedin_category or naics_category) — e.g. trusting that ICP term
    "Healthcare" resolving via linkedin_category proves a candidate whose
    reported industry is "Optical Goods Stores" (a NAICS description)
    satisfies "Healthcare".

    Investigation confirmed this trust was NEVER actually provable with
    real Explorium data, for ANY resolved value, not merely the
    Optical-Goods-Stores case that first exposed it:
      - Explorium's /businesses response only ever reports a company's
        NAICS-taxonomy description (naics_description -> "industry" —
        see app/providers/explorium.py's own _BUSINESS_ATTRIBUTE_MAP). It
        never echoes back a company's own linkedin_category value at all.
      - A linkedin_category-resolved branch's matched value (e.g. the
        slug "healthcare") and a naics_category-resolved branch's matched
        value (a NUMERIC NAICS code, e.g. "511210") both live in
        ID/label spaces that share NO identifier with the returned
        naics_description TEXT — there is no code-to-description lookup
        captured anywhere in this codebase, and building one would mean
        hardcoding NAICS/LinkedIn taxonomy knowledge, which this fix must
        not do (see app/services/hard_rule_engine.py's own "never
        fuzzy/semantic matching, never invent a synonym" discipline,
        unchanged and still the law here).
      - Confirmed by the module's OWN prior "good" examples: "Healthcare"
        -> "General Medical and Surgical Hospitals" and "SaaS" ->
        "Software Publishers" only ever LOOKED correct because whoever
        wrote those test fixtures happened to know, from real-world
        knowledge, that those pairings are true — no code in this
        codebase ever verified it. The exact same trust would have
        equally accepted "Healthcare" -> "Optical Goods Stores" (which it
        did, live) — there was never a real distinguishing signal between
        the two cases, only luck in which examples were chosen to write
        tests against.

    Given docs/evidence-policy.md's own contract ("A field backed only by
    INFERRED and/or LOW-confidence evidence is not verified... it cannot
    satisfy a hard rule check") and this quality contract's explicit
    "never fabricate or guess" principle, the only honest behavior when a
    relationship cannot be proven is to withhold it — never widen the
    allowed set on an unverifiable value. See
    _industry_is_unconfirmed_structured_match (below) for the generic,
    HOLD-not-guess replacement: a structured match that doesn't already
    exact-match an ICP term now suppresses to unknown (HOLD), for ANY
    ICP — single-term or compound — rather than being silently trusted
    here. A literal exact-text match (e.g. candidate industry ==
    "Healthcare") never needed this function at all —
    app/services/hard_rule_engine.py's own plain exact-match check
    already PASSes that case on its own merits, unaffected by this
    retirement."""
    return ()


def _industry_has_unprovable_compound_match(
    field_summaries: dict[str, FieldEvidenceSummary],
    icp_industries: tuple[str, ...],
    icp_combination_terms: tuple[str, ...] = (),
) -> bool:
    """Generalized live-test audit fix (2026-09-03, corrected after a
    deeper investigation — name kept for continuity with existing call
    sites/tests referring to it, even though the trigger condition below
    is no longer specifically about compound ICPs).

    _bridged_industry_terms is now permanently retired (see its own
    docstring for the full investigation): NO structured
    linkedin_category/naics_category match can be verified against a
    candidate's reported "industry" (NAICS description) value with real
    Explorium data, for ANY ICP — one industry term or several. Without
    SOME suppression, a candidate whose real, literal industry value
    doesn't string-match the ICP's own term(s) still reaches
    evaluate_hard_rules() with that literal value, which correctly
    exact-match-FAILs it — but FAIL means "actively disproven," and an
    unverifiable NAICS/LinkedIn taxonomy crossing is not proof of
    disproof either (the company could be a genuine ICP match; we simply
    cannot confirm it from this one resolved string, per this module's
    own "never bridge on missing/ambiguous provenance" discipline,
    unchanged). The honest outcome is HOLD, not a confident-sounding FAIL
    this codebase cannot actually back up, and never a guessed PASS.

    Returns True whenever a candidate's OWN industry evidence:
      1. is SUPPORTED/SUPPORTED_STRUCTURED (i.e. would otherwise be read
         as a fact at all — untrusted/INSUFFICIENT evidence already HOLDs
         on its own, unaffected by this function);
      2. does NOT already exact-match one of the ICP's own literal terms
         (a literal match needs no suppression — evaluate_hard_rules'
         plain, unmodified exact-match check already PASSes that case
         entirely on its own merits); and
      3. carries real structured-branch provenance (industry_match_branch
         + industry_match_terms, written only by app/providers/explorium.py
         for a genuine live-verified linkedin_category/naics_category
         match) tied to at least one of the ICP's own current industry
         terms — i.e. this candidate's mismatch traces back to an
         unverifiable cross-taxonomy resolution, not to having no
         structured signal at all.

    A keyword-fallback record (no industry_match_branch tag at all, by
    construction — see app/providers/explorium.py's own
    _STRUCTURED_TAXONOMY_FIELD, which excludes the keyword branch) or a
    resolved branch untied to any of the ICP's current terms returns
    False — this function never suppresses a candidate whose mismatch has
    no structured signal behind it at all; that case's plain FAIL is
    already the honest, unrelated-to-this-fix outcome
    app/services/hard_rule_engine.py has always produced.

    icp_combination_terms is accepted for call-site/signature
    compatibility only and no longer changes this function's own
    decision — see _effective_required_industry_terms's docstring for
    why a combination phrase existed in the first place (softening
    resolved_category_count comparisons this function no longer makes at
    all, now that NO count-based proof is trusted)."""
    summary = field_summaries.get("industry")
    if summary is None or summary.status not in _ELIGIBLE_STATUSES:
        return False

    icp_terms_clean = {clean_text(t).lower() for t in icp_industries}
    for record in summary.records:
        # The candidate's OWN resolved value may already exact-match one
        # of the ICP's own literal terms — evaluate_hard_rules' plain,
        # unmodified exact-match check already PASSes that case entirely
        # on its own merits, with no bridge or suppression involved at
        # all (see test_compound_icp_with_matching_exact_value_still_passes_honestly).
        # This function must never suppress a value that would otherwise
        # legitimately PASS outright.
        if clean_text(str(record.value)).lower() in icp_terms_clean:
            continue
        if not record.evidence_text:
            continue
        try:
            parsed = json.loads(record.evidence_text)
        except (ValueError, TypeError):
            continue
        match_info = parsed.get(_INDUSTRY_MATCH_PROVENANCE_KEY) if isinstance(parsed, dict) else None
        if not isinstance(match_info, dict):
            continue
        branch_terms = match_info.get("icp_terms")
        if not isinstance(branch_terms, list):
            continue
        branch_terms_clean = {clean_text(str(t)).lower() for t in branch_terms}
        if not (branch_terms_clean & icp_terms_clean):
            continue  # stale/unrelated tag — never suppress on an unrelated branch
        # A real structured branch (linkedin_category or naics_category)
        # resolved for at least one of THIS ICP's own terms, but the
        # candidate's own reported value does not already exact-match any
        # ICP term — an unverifiable cross-taxonomy relationship (see this
        # function's own docstring). HOLD, never PASS or FAIL.
        return True
    return False


def _record_covered_icp_terms(record: EvidenceRecord, icp_terms_clean: set[str]) -> set[str]:
    """Every ICP industry term this ONE evidence record's own provenance
    tag(s) genuinely, structurally cover — the union of whatever
    industry_match.icp_terms (a real linkedin_category/naics_category
    exact match — see _bridged_industry_terms) and keyword_match.terms
    (a real website_keywords OR-list entry, filtered to ones the branch
    itself attributed to the "industry" request field, never
    "company_type" — see app/providers/explorium.py's own
    keyword_match_term_sources comment) this record's evidence_text
    actually states, intersected against the ICP's own current term list.
    Both tags can be present on the SAME record since Phase 37 (see
    app/providers/explorium.py::_merge_cross_branch_attributes and
    app/services/evidence_import.py::_industry_match_provenance's own
    updated docstrings) — that is precisely the "one candidate, evidence
    from multiple discovery branches" case this function exists to read.
    Returns an empty set for a record with no parseable/relevant
    provenance at all — never a guess."""
    if not record.evidence_text:
        return set()
    try:
        parsed = json.loads(record.evidence_text)
    except (ValueError, TypeError):
        return set()
    if not isinstance(parsed, dict):
        return set()

    covered: set[str] = set()

    # RETIRED (live-test audit finding, 2026-09-03, corrected after
    # deeper investigation — see _bridged_industry_terms's own docstring
    # for the full story): a structured linkedin_category/naics_category
    # branch match was previously trusted to mark its own icp_terms as
    # "covered" purely because the branch resolved, with no verification
    # that the candidate's own reported industry (NAICS description)
    # value actually corresponds to those terms — the exact same
    # unprovable cross-taxonomy trust _bridged_industry_terms itself
    # relied on, since confirmed unprovable with real Explorium data for
    # ANY resolved value, not only the case that first exposed it. This
    # function no longer reads industry_match_* provenance at all — only
    # keyword_match_* below, a genuinely different, still-provable signal
    # (the literal search term that found the candidate, unrelated to any
    # taxonomy-crossing trust), unaffected by this fix.
    keyword_info = parsed.get(_KEYWORD_MATCH_PROVENANCE_KEY)
    if isinstance(keyword_info, dict):
        keyword_terms = keyword_info.get("terms")
        keyword_sources = keyword_info.get("term_sources")
        if isinstance(keyword_terms, list):
            if isinstance(keyword_sources, list) and len(keyword_sources) == len(keyword_terms):
                # Only terms this branch itself attributed to the
                # "industry" request field — a keyword hit tagged
                # "company_type" (e.g. "D2C") says something about what
                # KIND of business this is, never a claim about which
                # INDUSTRY concept it satisfies, and must never be counted
                # here (see app/providers/explorium.py's own
                # keyword_match_term_sources comment for the exact same
                # distinction already enforced elsewhere).
                industry_sourced_terms = [t for t, source in zip(keyword_terms, keyword_sources) if source == "industry"]
            else:
                # No source breakdown available (a pre-Phase-13D record,
                # or a keyword-less request shape's tag) — treat every
                # term as industry-sourced, matching this codebase's
                # existing behavior everywhere else keyword_match_term_sources
                # is optionally absent.
                industry_sourced_terms = list(keyword_terms)
            # A keyword OR-list's own "total_results" (if Explorium even
            # returned one) is never attributable to any single term
            # within it — the SAME confound _bridged_industry_terms'
            # structured-branch check exists to avoid, restated here for
            # the keyword tier: Explorium's website_keywords OR-list
            # returns ONE merged result set, with NO per-term match
            # breakdown at all. A list of 2+ industry-sourced keyword
            # terms is therefore NEVER safe to treat as "this candidate
            # matched every one of these terms" — only a list of exactly
            # ONE unambiguously does (nothing else in the OR-list to
            # confound it with).
            if len(industry_sourced_terms) == 1:
                covered |= {clean_text(str(t)).lower() for t in industry_sourced_terms}

    return covered & icp_terms_clean


def _cross_branch_corroborated_terms(
    field_summaries: dict[str, FieldEvidenceSummary],
    icp_industries: tuple[str, ...],
    icp_combination_terms: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Phase 37 — cross-branch/cross-provenance industry corroboration.

    Live-test audit fix (2026-09-03): _record_covered_icp_terms (used
    below) no longer trusts a structured linkedin_category/naics_category
    branch match on its own — see that function's own docstring and
    _bridged_industry_terms's for the full investigation. This function's
    documented "Healthcare (structured) + SaaS (keyword)" corroboration
    scenario below now only ever succeeds via keyword-branch coverage
    (a genuinely different, still-provable signal); a structured-only
    branch can no longer contribute coverage for any term, so a candidate
    whose ONLY signal for a term is an unverifiable structured resolution
    correctly HOLDs via _industry_has_unprovable_compound_match instead of
    corroborating here. This function's own remaining behavior (unioning
    provable coverage across records/rounds, requiring ALL terms covered
    before returning anything) is otherwise UNCHANGED.

    _bridged_industry_terms (above) only ever bridges when ONE structured
    branch, alone, structurally resolved EVERY ICP industry term — which
    is impossible by construction whenever a compound ICP's terms
    genuinely resolve to DIFFERENT Explorium branches (the documented,
    common "Healthcare SaaS" case: "Healthcare" matches a real
    linkedin_category, "SaaS" has no exact taxonomy match anywhere and
    falls to the keyword branch — see app/providers/explorium.py's own
    "or, not and" branch-merging comment). Before this function existed,
    such a candidate could NEVER bridge/PASS no matter how genuinely
    correct it was, because no single branch ever covers a genuinely
    disjoint compound ICP alone.

    This function recognizes the SAME kind of genuine correctness through
    a DIFFERENT, still fully evidence-based signal: does this ONE
    candidate's own evidence — combined ACROSS every branch that
    contributed to it, all still real, live-verified Explorium provenance
    on a SINGLE evidence record (see app/providers/explorium.py::
    _merge_cross_branch_attributes for how one record ends up carrying
    provenance from more than one branch when Explorium itself returns
    the same real company from more than one branch in one call) —
    together cover every ICP industry term. A structured Healthcare hit
    PLUS a keyword SaaS hit, on the confirmed SAME real company, is
    genuine corroboration: two independent, real discovery signals for
    the two different halves of a compound requirement, not a guess.

    This is NEVER semantic/fuzzy matching and never invents a synonym: a
    term only ever "counts" as covered when a real Explorium branch
    (structured taxonomy match, or the literal keyword OR-list actually
    sent) reports it, exactly as _bridged_industry_terms's own structured
    check already does — this function only widens WHICH branches'
    combined provenance can satisfy that bar, never how loosely a single
    branch's own match is judged.

    Still conservative by construction:
      - Requires the industry field to already be SUPPORTED/
        SUPPORTED_STRUCTURED (i.e. still gated by the exact same trust
        rule as every other read here — Hermes's own untrusted single
        sightings never reach this function's summary lookup at all,
        since summary.status gates it below exactly like
        _bridged_industry_terms does).
      - Requires the COMBINED coverage to include EVERY stated ICP
        industry term — a candidate covering only some terms across all
        its branches still returns () here, exactly like the single-
        branch case, and _industry_has_unprovable_compound_match (already
        unmodified) still correctly softens that to HOLD, never PASS.
      - Single-term ICPs have no "compound" concept to corroborate across
        branches for, so this is a pure no-op for them (matches
        _bridged_industry_terms's own len() <= 1 guards elsewhere in this
        module's sibling functions, restated here for the same reason).

    Returns the candidate's own evidenced industry value(s) (added to the
    allowed set for THIS evaluation only, exactly like
    _bridged_industry_terms) when corroboration is complete, or () when
    it is not — never guesses, never partial-credits.

    Phase 38 — CROSS-RECORD union, not just per-record: the ORIGINAL
    (Phase 37) version of this function only ever checked whether ONE
    evidence record's OWN combined branch tags, alone, covered every ICP
    term — which only recognizes corroboration when Explorium happened to
    return the same company from multiple branches within a SINGLE API
    call (see app/providers/explorium.py::_merge_cross_branch_attributes).
    It could never recognize the equally real case of a Healthcare hit in
    discovery ROUND 1 and a separate SaaS hit in ROUND 2 (or from Hermes
    entirely), because app/services/evidence_import.py::
    collect_company_evidence ALREADY builds one SEPARATE industry
    EvidenceRecord per resolved discovery candidate — evidence from every
    round/provider that ever resolved to this canonical company was
    already being collected, just never unioned across those separate
    records. This function now reads `field_summaries["industry"].records`
    directly (documented on FieldEvidenceSummary as "the full, unfiltered
    set", regardless of the field's overall EvidenceStatus — see the
    summary.status gate REMOVED below, and why that is still safe) and
    unions _record_covered_icp_terms across ALL of them.

    Why bypassing the summary.status gate is still safe: two genuinely
    different real Explorium-reported industry label strings for the SAME
    canonical company (one per discovery round/branch — e.g. "General
    Medical and Surgical Hospitals" from a Healthcare hit, "Software
    Publishers" from a separate SaaS hit) make evidence_engine.py's
    compute_field_status report CONFLICT for the "industry" field as a
    whole (2+ distinct values), which is NOT in _ELIGIBLE_STATUSES — so
    the OLD gate would refuse to even look at these records, permanently
    blocking exactly the cross-round scenario this function exists to
    recognize. The real safety boundary was never the field's aggregate
    status; it is, and remains, each INDIVIDUAL record's own provenance
    tag, verified per-record by _record_covered_icp_terms (a record with
    no real industry_match/keyword_match tag — e.g. any bare Hermes
    sighting — contributes an empty covered set regardless of this
    function's gating, so "one weak/unknown source is sufficient by
    itself" remains impossible either way)."""
    if len(icp_industries) <= 1:
        return ()

    summary = field_summaries.get("industry")
    if summary is None:
        return ()

    icp_terms_clean = {clean_text(t).lower() for t in icp_industries}
    # Phase 39: the coverage bar is the EFFECTIVE (compound-phrase-
    # collapsed) term set — see _effective_required_industry_terms's own
    # docstring — so covering an AI-proposed combination phrase alone
    # (e.g. "Healthcare Software") satisfies the terms it textually
    # subsumes ("Healthcare") without also needing separate, independent
    # evidence for those subsumed terms. Membership lookups against a
    # record's own provenance still use the full, unreduced
    # icp_terms_clean — only the SUCCESS BAR is reduced.
    effective_required_clean = {
        clean_text(t).lower() for t in _effective_required_industry_terms(icp_industries, icp_combination_terms)
    }

    # Every ICP term covered by the UNION of every record's own,
    # independently-verified provenance — this is what recognizes
    # "Healthcare in round 1 + SaaS in round 2" (or "+ Hermes") as
    # genuine corroboration, never a single record alone unless it
    # already covers everything itself (the Phase 37 same-call case,
    # still handled identically as a special case of this same union).
    combined_covered: set[str] = set()
    contributing_record_values: list[str] = []
    for record in summary.records:
        record_covered = _record_covered_icp_terms(record, icp_terms_clean)
        if record_covered:
            combined_covered |= record_covered
            contributing_record_values.append(str(record.value))

    if not (effective_required_clean <= combined_covered):
        # Combined evidence still doesn't prove every effectively-required
        # term — never partial-credit; HOLD is decided elsewhere (see
        # _industry_has_unprovable_compound_match and this module's own
        # INDUSTRY_UNKNOWN fallback), never guessed here.
        return ()

    return tuple(dict.fromkeys(contributing_record_values))


def _cross_branch_corroboration_evidence_ids(
    field_summaries: dict[str, FieldEvidenceSummary],
    icp_industries: tuple[str, ...],
    icp_combination_terms: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """The evidence ids backing a _cross_branch_corroborated_terms match —
    every "industry" record that contributed at least one genuinely
    covered ICP term to the union, across every discovery round/provider
    that resolved to this company. Kept as a companion function (same
    "recompute alongside the value function" pattern this module already
    uses for _free_text_industry_bridge / _free_text_industry_evidence_ids)
    so a corroborated PASS always cites the REAL contributing records —
    e.g. BOTH the round-1 Healthcare-branch record's id AND the round-2
    SaaS-branch record's id — never an empty evidence_ids list and never
    a citation for a record that didn't actually contribute coverage."""
    if len(icp_industries) <= 1:
        return ()

    summary = field_summaries.get("industry")
    if summary is None:
        return ()

    icp_terms_clean = {clean_text(t).lower() for t in icp_industries}
    # Phase 39: same effective-count reduction as _cross_branch_corroborated_terms
    # above — must use the identical success bar so this function's ()
    # vs. non-() outcome always agrees with that one's.
    effective_required_clean = {
        clean_text(t).lower() for t in _effective_required_industry_terms(icp_industries, icp_combination_terms)
    }
    combined_covered: set[str] = set()
    per_record_covered: list[tuple[str, set[str]]] = []
    for record in summary.records:
        record_covered = _record_covered_icp_terms(record, icp_terms_clean)
        if record_covered:
            combined_covered |= record_covered
            per_record_covered.append((record.id, record_covered))

    if not (effective_required_clean <= combined_covered):
        return ()

    return tuple(dict.fromkeys(record_id for record_id, _ in per_record_covered))


def _free_text_industry_bridge(field_summaries: dict[str, FieldEvidenceSummary], icp_industries: tuple[str, ...]) -> str | None:
    """Phase 35 — free-text evidence -> industry bridge (Hermes usefulness
    fix).

    _bridged_industry_terms (above) only ever reads Explorium's real,
    live-verified linkedin_category/naics_category resolution — Hermes
    (app/providers/hermes.py) has no taxonomy at all, only free-text
    fields an LLM research agent read off a webpage. Left unaddressed, a
    Hermes-only candidate's industry evidence NEVER reaches SUPPORTED/
    SUPPORTED_STRUCTURED (Hermes's provider_id is deliberately absent from
    evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS — see that
    module's own docstring, unchanged by this function), so it always
    HOLDs regardless of how clearly the free text actually proves the
    ICP's intent — even for a company whose description literally reads
    "SaaS platform serving healthcare providers" against a "Healthcare
    SaaS" ICP.

    This function closes that gap WITHOUT lowering the evidence-status bar
    and WITHOUT any fuzzy/semantic matching: it reads ONLY the
    "description" evidence field's RAW TEXT VALUES DIRECTLY (regardless of
    their computed EvidenceStatus — deliberately, since a single Hermes
    sighting's status will always be INSUFFICIENT/UNKNOWN, and that status
    is not what makes this safe; literal string containment is), and
    returns a synthesized industry value ONLY when the free text contains,
    as an exact case-insensitive SUBSTRING, EVERY ONE of the ICP's own
    already-stated industry terms — the identical "every stated term
    must be covered, never guess when partial" discipline
    _industry_has_unprovable_compound_match already established for
    Explorium's structured branch, applied here to free text instead.

    Deliberately NEVER reads the "industry" field itself here — that
    field's own raw value already has its own, separate, correctly-gated
    path (_supported_value_and_evidence / _bridged_industry_terms above),
    and conflating the two would let an untrusted single "industry"
    sighting bypass its own trust gate simply by string-containing itself
    (a real regression this exact design caught and fixed: see
    tests/test_hard_icp_validation.py::
    test_untrusted_single_sighting_on_industry_still_holds_not_pass_or_fail,
    which must keep HOLDing an untrusted single "industry" sighting
    regardless of this function's existence). "description" is a
    genuinely distinct evidence field — free descriptive prose, never the
    provider's own single-word/phrase industry classification — so
    reading it does not create that same bypass.

    This is never semantic/fuzzy matching: "software provider" does NOT
    satisfy an ICP term of "SaaS" (no literal substring match), and this
    function has no synonym table and adds none — it is exactly as
    conservative as checking whether the literal words the ICP itself
    already asked for appear in the text. For a compound ICP like
    ("Healthcare", "SaaS"), only text containing BOTH "healthcare" AND
    "saas" (in any order, anywhere in the combined text) qualifies —
    exactly the "COMPLETE intersection" requirement, satisfied
    identically for ANY industry pair (Fintech+SaaS, D2C+Health, ...)
    with no per-industry code of any kind.

    Returns None (no bridge) when:
      - the ICP has no industry terms at all (nothing to prove),
      - no description evidence exists at all for this company,
      - the combined free text is missing even ONE of the ICP's terms.
    Returns the ICP's own FIRST industry term (a value already IN the
    allowed set, so evaluate_hard_rules' unmodified exact-match check
    trivially passes) when every term is proven. The candidate's real,
    original description evidence records are cited as this rule's
    evidence via _free_text_industry_evidence_ids, called separately by
    validate_against_icp — this function only decides the WIDENED VALUE,
    never which evidence ids justify it."""
    if not icp_industries:
        return None

    text_parts: list[str] = []
    summary = field_summaries.get("description")
    if summary is not None:
        for record in summary.records:
            if isinstance(record.value, str) and record.value:
                text_parts.append(record.value)

    if not text_parts:
        return None

    combined_text = clean_text(" ".join(text_parts)).lower()
    for term in icp_industries:
        term_clean = clean_text(term).lower()
        if not term_clean or term_clean not in combined_text:
            # At least one required term has zero literal support in the
            # free text — never bridge on a partial match, exactly
            # mirroring _bridged_industry_terms' condition 3.
            return None

    return icp_industries[0]


def _free_text_industry_evidence_ids(field_summaries: dict[str, FieldEvidenceSummary]) -> tuple[str, ...]:
    """The evidence ids backing a _free_text_industry_bridge match — every
    record from the "description" field that actually contributed text to
    the bridge's combined_text, regardless of EvidenceStatus (see that
    function's own docstring for why status is not the safety gate here,
    and why "industry" itself is deliberately never read by either
    function). Kept separate from _supported_value_and_evidence so a
    bridged PASS always cites the REAL records a human reviewer can
    check, never an empty evidence_ids list."""
    ids: list[str] = []
    summary = field_summaries.get("description")
    if summary is not None:
        for record in summary.records:
            if isinstance(record.value, str) and record.value:
                ids.append(record.id)
    return tuple(dict.fromkeys(ids))


def validate_against_icp(
    icp: CanonicalICP,
    company_id: str,
    company_evidence: list[EvidenceRecord],
    person_id: str | None,
    person_evidence: list[EvidenceRecord],
) -> HardIcpValidationResult:
    """Builds an evidence-gated Candidate and calls Phase 3's unchanged
    evaluate_hard_rules(). Pure and DB-free, mirroring every other
    *_engine module in this codebase — the caller (app/api/hard_icp_validation.py)
    owns persistence.
    """
    company_summary = summarize_entity(EntityType.COMPANY, company_id, company_evidence)
    company_fields = {field.field: field for field in company_summary.fields}

    company_name, company_name_ev = _supported_value_and_evidence(company_fields, "company_identity")
    domain, domain_ev = _supported_value_and_evidence(company_fields, "domain")
    industry, industry_ev = _supported_value_and_evidence(company_fields, "industry")
    # Phase 25: a genuinely PARTIAL structured match against a compound,
    # multi-term ICP (e.g. "Healthcare SaaS") is suppressed to unknown
    # here, BEFORE Candidate is built — see
    # _industry_has_unprovable_compound_match's own docstring for exactly
    # why this candidate has been neither proven nor disproven, and why
    # this mirrors the exact same "unconfirmed -> None -> Phase 3 HOLDs
    # on it itself" pattern this function already applies to person
    # titles below. app/services/hard_rule_engine.py's own HOLD/
    # INDUSTRY_UNKNOWN path is completely unmodified; only what reaches
    # it changes, for this one evaluation, for this one candidate.
    #
    # Phase 37: computed BEFORE the suppression check below — a candidate
    # whose evidence, combined ACROSS every discovery branch that
    # contributed to it, already corroborates the FULL compound ICP (see
    # _cross_branch_corroborated_terms's own docstring) must never be
    # suppressed to HOLD by _industry_has_unprovable_compound_match, which
    # only exists to soften a FAIL for a candidate that is NOT fully
    # corroborated. Corroboration is checked first, and short-circuits the
    # suppression, so a genuinely proven compound match reaches PASS via
    # the widened allowed-set below instead of being needlessly HOLD'd.
    cross_branch_terms = _cross_branch_corroborated_terms(
        company_fields, icp.hard_rules.industries, icp.hard_rules.industry_combination_terms
    )
    if (
        industry is not None
        and not cross_branch_terms
        and _industry_has_unprovable_compound_match(
            company_fields, icp.hard_rules.industries, icp.hard_rules.industry_combination_terms
        )
    ):
        industry, industry_ev = None, ()
    # Phase 38: when the industry field's OVERALL status is CONFLICT (two+
    # genuinely different real industry label strings — e.g. one per
    # discovery round/provider — for this same canonical company),
    # _supported_value_and_evidence above never populated `industry` at
    # all (CONFLICT is not in _ELIGIBLE_STATUSES). Cross-round/cross-
    # provider corroboration is exactly the scenario CONFLICT status was
    # unhelpfully blocking (see _cross_branch_corroborated_terms's own
    # docstring for why reading summary.records directly, bypassing that
    # gate, is still safe): if the combined evidence nonetheless proves
    # the FULL compound ICP, that is a genuine, evidenced fact this
    # candidate has earned — set `industry` directly here (mirroring the
    # free-text bridge's own "no structured value was ever set, use ours"
    # pattern immediately below) rather than leaving it None and HOLDing
    # on a company this evidence has actually already proven.
    if industry is None and cross_branch_terms:
        industry, industry_ev = cross_branch_terms[0], _cross_branch_corroboration_evidence_ids(
            company_fields, icp.hard_rules.industries, icp.hard_rules.industry_combination_terms
        )
    # Phase 35 — free-text industry bridge (see _free_text_industry_bridge's
    # own docstring): unlike _bridged_industry_terms below (which only ever
    # WIDENS the allowed set for an industry value that already reached
    # SUPPORTED/SUPPORTED_STRUCTURED), a Hermes-only candidate's industry
    # evidence never reaches that status at all — Hermes is deliberately
    # untrusted (see evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS),
    # so `industry` is still None here for such a candidate. Only attempt
    # the free-text bridge when the structured path found nothing at all
    # (industry is None) — a candidate with a real SUPPORTED/
    # SUPPORTED_STRUCTURED industry value already has a stronger,
    # structured basis for its result and must never have that outcome
    # second-guessed or overridden by weaker free-text evidence.
    if industry is None:
        free_text_value = _free_text_industry_bridge(company_fields, icp.hard_rules.industries)
        if free_text_value is not None:
            industry, industry_ev = free_text_value, _free_text_industry_evidence_ids(company_fields)
    geography, geography_ev = _supported_value_and_evidence(company_fields, "country")
    employee_count, employee_count_ev = _supported_value_and_evidence(company_fields, "employee_count")
    # Phase 7O: employee_range (a provider-stated bucket, e.g. "51-200")
    # is a real, separately-persisted evidence field since Phase 7N, but
    # was never read here — the employee_range hard rule could only ever
    # resolve from an exact employee_count, which most real discovery
    # providers rarely supply. Read independently of employee_count so
    # evidence_ids below can cite whichever one actually produced the
    # rule's evidence.
    employee_range_bucket, employee_range_bucket_ev = _supported_value_and_evidence(company_fields, "employee_range")
    company_type, company_type_ev = _supported_value_and_evidence(company_fields, "company_type")

    title: str | None = None
    title_ev: tuple[str, ...] = ()

    if person_id is not None:
        person_summary = summarize_entity(EntityType.PERSON, person_id, person_evidence)
        person_fields = {field.field: field for field in person_summary.fields}

        association = person_fields.get("company_association")
        association_confirms_this_company = (
            association is not None
            and association.status == EvidenceStatus.SUPPORTED
            and association.records[0].value == company_id
        )

        if association_confirms_this_company:
            title, title_ev = _supported_value_and_evidence(person_fields, "current_title")
        # If the association isn't confirmed for *this* company, the title
        # stays unknown for this validation — Phase 3 will HOLD on it via
        # its existing TITLE_UNKNOWN path, exactly as if no title evidence
        # existed at all.

    candidate = Candidate(
        company_name=company_name,
        domain=domain,
        industry=industry,
        geography=geography,
        employee_count=employee_count if isinstance(employee_count, int) else None,
        employee_range=employee_range_bucket if isinstance(employee_range_bucket, str) else None,
        title=title,
        company_type=company_type,
        custom_rule_results={},  # no evidence-backed way to establish these yet; Phase 3 HOLDs each
    )

    # Phase 11 industry search -> taxonomy bridge (see _bridged_industry_terms's
    # own docstring for the full rationale). Builds a per-call, ADDITIVE-ONLY
    # copy of the icp's industries list for THIS candidate's evaluation —
    # the original `icp` object is never mutated, and
    # app/services/hard_rule_engine.py itself is never touched: it still
    # only ever does its own unmodified exact-match check, just against a
    # set that may now also include this candidate's own evidenced,
    # provenance-confirmed industry value.
    #
    # Phase 37: cross_branch_terms (computed above, before the suppression
    # check) is unioned in here the same additive way — a candidate whose
    # combined multi-branch evidence corroborates the full compound ICP
    # gets its own evidenced value added to the allowed set, so
    # evaluate_hard_rules' unmodified exact-match check trivially passes,
    # exactly like the existing same-branch bridge case.
    bridged_terms = _bridged_industry_terms(
        company_fields, icp.hard_rules.industries, icp.hard_rules.industry_combination_terms
    )
    all_bridged_terms = tuple(dict.fromkeys((*bridged_terms, *cross_branch_terms)))
    icp_for_evaluation = icp
    if all_bridged_terms:
        widened_industries = tuple(dict.fromkeys((*icp.hard_rules.industries, *all_bridged_terms)))
        icp_for_evaluation = icp_for_evaluation.model_copy(
            update={"hard_rules": icp_for_evaluation.hard_rules.model_copy(update={"industries": widened_industries})}
        )

    # P1 fix — allowed_titles must not force HOLD on a company-only
    # evaluation (person_id is None): no person has been looked for yet
    # at this point in the pipeline (see
    # app/services/batch_orchestration.py's own "company-first ordering"
    # docstring — hard validation runs before Unipile/person discovery),
    # so an unresolved title here is not "evidence is missing," it is
    # "there is nothing to evaluate yet." NOT_APPLICABLE is the correct
    # status for that (RuleStatus.NOT_APPLICABLE's own docstring: "the ICP
    # does not constrain this rule AT ALL" for THIS evaluation — a
    # per-call view, never a mutation of the ICP's own allowed_titles,
    # which a later person-scoped call still reads in full).
    #
    # This must NEVER suppress the rule once a real person is being
    # evaluated: person_id is not None means a person candidate actually
    # exists for this validation, and an unresolved/mismatched title for
    # THAT person must still HOLD/FAIL exactly as
    # app/services/hard_rule_engine.py's own test_title_unknown_holds
    # already asserts — unchanged here, since this suppression is gated
    # strictly on person_id is None.
    if person_id is None and icp.hard_rules.allowed_titles:
        icp_for_evaluation = icp_for_evaluation.model_copy(
            update={"hard_rules": icp_for_evaluation.hard_rules.model_copy(update={"allowed_titles": ()})}
        )

    evaluation = evaluate_hard_rules(icp_for_evaluation, candidate)

    exclusion_evidence = tuple(
        dict.fromkeys(company_name_ev + domain_ev + industry_ev + geography_ev + company_type_ev + title_ev)
    )
    # Phase 7O: cite whichever evidence actually produced the
    # employee_range rule's result — employee_count when the Candidate had
    # one (the same precedence evaluate_hard_rules itself applies), else
    # employee_range_bucket_ev. Never cite an evidence id that wasn't
    # actually used to reach the outcome.
    employee_rule_evidence = employee_count_ev if candidate.employee_count is not None else employee_range_bucket_ev
    field_evidence = {
        "industry": industry_ev,
        "geography": geography_ev,
        "employee_range": employee_rule_evidence,
        "company_type": company_type_ev,
        "allowed_titles": title_ev,
        "exclusions": exclusion_evidence,
    }
    evidence_ids = {rule.rule: field_evidence.get(rule.rule, ()) for rule in evaluation.rule_results}

    return HardIcpValidationResult(
        company_id=company_id,
        person_id=person_id,
        evaluation=evaluation,
        evidence_ids=evidence_ids,
    )
