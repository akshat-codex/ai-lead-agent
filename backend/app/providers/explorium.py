"""Phase 7A (corrected) — real Explorium provider adapter for COMPANY_DISCOVERY.

Calls Explorium's real Fetch Businesses API (POST /v2/businesses, header
API_KEY) and maps only the fields Explorium actually documents returning —
name, domain, number_of_employees_range, naics_description (industry),
country_name (geography), linkedin_profile, business_id. Confirmed against
Explorium's own published API reference
(https://developers.explorium.ai/v2/businesses/fetch_businesses.md) before
writing this mapping — nothing here is invented.

Two fields are deliberately NOT mapped onto the existing precise-count/
company-identity attribute names, to avoid misrepresenting the data
Explorium actually returns:
  - number_of_employees_range is a STRING RANGE (e.g. "11-50"), not a
    precise integer. app/schemas/candidate.py's Candidate.employee_count is
    strictly int | None, and the existing codebase already documents the
    "a range is not a count" rule (see app/services/evidence_import.py's own
    comment) — so this is mapped to attributes["employee_range"] (a string),
    never coerced into a fabricated employee_count integer.
  - linkedin_profile is preserved as attributes["linkedin_id"] exactly when
    Explorium's response actually includes it for a given business — never
    filled in when absent. This satisfies the required "Company LinkedIn
    URL when actually available" product output without inventing one.

The API key is supplied at construction (read once, from Settings, by
app/providers/default_registry.py) and never read from the environment
inside execute(), never included in any ProviderRequest/ProviderResponse
field, and never written to a log line.

Pagination: live-tested against the real API (Explorium's own published
docs claim a `page` int param is also supported, but sending one is
rejected with a 422 "extra fields not permitted" — that claim is wrong for
this endpoint as of this testing). Only a bare top-level `next_cursor`
string field actually works; the response echoes the next token as
`page.next_cursor`, or sends `page: null` once a query is exhausted. See
`ProviderRequest.cursor` / `ProviderResponse.cursor`/`exhausted`.

Phase 7E — per-industry discovery branching. website_keywords alone is
low-precision: it matches any word appearing anywhere on a company's
website, so a recruiting agency whose site merely mentions "Healthcare"
surfaces as readily as a real healthcare company (confirmed live: a
website_keywords=[Healthcare] sample returned 0/10 genuinely healthcare
companies). A real, closed, autocomplete-confirmed `linkedin_category`
match is dramatically more precise (confirmed live: the same query via
linkedin_category=[healthcare] returned 10/10 genuinely healthcare-adjacent
companies). But most ICP industry terms have NO real category counterpart
at all (confirmed live: FMCG, D2C Brands, OTT Platforms, and Microdrama
Companies all return zero results from Explorium's own live autocomplete
endpoint, GET /v1/businesses/autocomplete) — inventing a broader/adjacent
category for those would be exactly the undefensible guessing this
codebase has always refused to do for industry filtering.

So each ICP industry term is independently, automatically checked against
the real autocomplete endpoint (never hardcoded per-ICP): a term that gets
a real, EXACT (case/whitespace-normalized) match joins a "structured"
branch query using linkedin_category; every other term falls back to
exactly today's website_keywords branch. Both branches, when both exist,
are separate real POST /v2/businesses calls (Explorium's own filters AND
together within one request — confirmed live — so a structured filter and
a keyword filter cannot be combined in a single call without wrongly
excluding companies that only match one side), merged and deduplicated by
business_id before returning one ProviderResponse. This keeps
provider_id singular and keeps every caller (company_discovery.py,
batch_orchestration.py, BatchModel's cursor/exhaustion bookkeeping)
completely unaware that more than one Explorium query happened — cursor
state for both branches is packed into the one opaque
ProviderResponse.cursor string, exactly as that field's contract already
allows (a provider-owned, caller-never-interprets value).

Phase 7G — two focused fixes, no architecture change:
  - _build_employee_size_filter previously only forwarded Explorium size
    buckets FULLY CONTAINED within the ICP's [min, max] range. For a real,
    arbitrary range like 15-150, no bucket qualified, so no company_size
    filter was sent at all and companies of any size passed through
    unconstrained (confirmed live: IBM/Meta/Deloitte, all 10001+
    employees, appeared in the Phase 7F Deep Tech test). Now any bucket
    that OVERLAPS [min, max] is forwarded — never silently drops the
    constraint. The existing hard ICP validation (unchanged) still
    performs the exact numeric check against real employee evidence.
  - yearly_revenue_range, a documented Explorium response field already
    present in every real response body, is now mapped into
    attributes["revenue_range"] instead of being discarded — no new API
    call, purely surfacing data already being paid for.

Phase 7I — discovery error/observability only, no discovery-behavior change:
  - Every _lookup_linkedin_category autocomplete call and the main
    _run_one_branch /businesses call now logs its wall-clock duration at
    DEBUG (module logger). The Phase 7H live test took ~94s end-to-end
    with an unrecoverable generic PROVIDER_ERROR and no way to tell
    whether the 8 sequential per-industry-term autocomplete lookups (each
    up to timeout_seconds) or the main search call was the actual cause —
    this makes that visible on the next run without guessing. Logging
    only; no call count, timeout, retry, or branching logic changed.

Phase 7N — end-to-end plumbing audit found ONE real gap: the frontend's
"Company type" hard-rule field (e.g. "D2C, PE-backed, Startup" — a real,
ICP-generic input, not domain-specific) flows correctly all the way to
CompanyDiscoveryQuery.company_types, but this adapter never read that
field at all — silently dropped, the only such gap found (employee_range,
geography, and industry were all confirmed already correct end-to-end).
Explorium has no structured "company type" taxonomy to check via
autocomplete the way industries are, so company_types terms now join the
SAME website_keywords OR-list already used for unmatched industry terms
— never a new filter type, never an invented structured mapping, and
never ICP-specific (this reads whatever company_types the CURRENT
request states, exactly like industries already do).

Phase 41 — company_type discovery precision. Phase 7N's conclusion above
("Explorium has no structured company type taxonomy to check") was never
actually TESTED for company_type-shaped terms: this module's own
confirmed-live finding of zero autocomplete results covered
INDUSTRY-shaped compound terms (FMCG, D2C Brands, OTT Platforms,
Microdrama Companies — see the Phase 7E note above), while
company_type-shaped terms like "Startup", "Nonprofit", or "Wholesale
Distributor" plausibly DO have a real linkedin_category/naics_category
counterpart and were simply never looked up. So company_types now get
the SAME structured-taxonomy-first attempt industries already get
(_plan_company_type_branches), through the SAME live autocomplete
endpoint, the SAME exact-match-only discipline, and the SAME per-instance
caches — no new taxonomy, no fuzzy matching, no invented category, and
no extra API call for a term already cached.

Two hard constraints shape the implementation:
  - A resolved company_type term gets its OWN branch
    (_STRUCTURED_COMPANY_TYPE_BRANCH / _NAICS_COMPANY_TYPE_BRANCH),
    never merged into an industry structured branch: Explorium's filters
    AND together within one request, so one call carrying both a
    resolved industry category and a resolved company-type category
    would wrongly require a candidate to satisfy both simultaneously —
    the identical reasoning that already keeps the industry
    linkedin_category and naics_category branches separate.
  - Its provenance uses a DISTINCT key set (company_type_match_branch /
    company_type_match_terms / company_type_match_resolved_category_count),
    never industry_match_*. This module maps NO Explorium response field
    into a "company_type" attribute at all (company_type is absent from
    _BUSINESS_ATTRIBUTE_MAP, unlike naics_description -> industry), and
    app/services/evidence_import.py::_industry_match_provenance writes
    nothing for such a record — so a company_type structured match is
    PURELY discovery provenance (which real signal found this candidate)
    and is structurally unable to reach hard ICP validation as industry
    evidence. The `company_type` hard rule's existing HOLD-for-Explorium
    behavior is unchanged, and this phase creates no new PASS path.

A company_type term with no exact match on either taxonomy still falls to
the website_keywords OR-list exactly as before, still tagged
keyword_match_term_sources == "company_type" (Phase 13D) — the
low-precision signal stays honestly labeled as low-precision rather than
being dressed up as structured evidence.

Phase 8B — a second structured taxonomy tier, NAICS, before falling back
to keywords. The Phase 8 live test (a real D2C/FMCG ICP) confirmed
linkedin_category has no exact match for many legitimate industry terms,
so those terms fell straight to website_keywords and produced obvious
false positives (staffing agencies, unrelated consultancies). Explorium's
own documentation (confirmed via
https://developers.explorium.ai/reference/businesses/autocomplete/
businesses_autocomplete.md — NOT independently live-verified against the
real API this session, unlike linkedin_category's autocomplete mechanism
in earlier phases) confirms naics_category is a second, real, documented
autocomplete `field` value on the exact same endpoint, exact same
{query, label, value} response shape as linkedin_category — the only
documented difference is that a NAICS suggestion's `value` is a numeric
code string (e.g. "541512") rather than a readable slug, while `label`
is still the human-readable text an ICP term is matched against exactly
the same way.

Resolution order per industry term, never guessed/fuzzy, always an exact
label match: (1) linkedin_category autocomplete, (2) if no exact match,
naics_category autocomplete, (3) if neither matches, website_keywords.
Structured matches from the two taxonomies are kept in SEPARATE branches
(never merged into one filter — Explorium's filters AND together within
one request, confirmed live in Phase 7E, so a naics_category value and a
linkedin_category value in the same call would wrongly require a company
to satisfy both simultaneously). This reuses the exact same autocomplete
call mechanism, per-instance caching, and N-branch merge/dedup/pagination
architecture Phase 7E already built — generalized from 2 branches to up
to 3, with zero changes to that merge logic itself (it was already
written generically over however many branches are active).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderReliabilityProfile,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.services.icp_normalization import clean_text

EXPLORIUM_AUTH_FAILED = "EXPLORIUM_AUTH_FAILED"
EXPLORIUM_CREDITS_EXHAUSTED = "EXPLORIUM_CREDITS_EXHAUSTED"

# The branch names packed into the opaque multi-branch cursor JSON object —
# never exposed outside this module.
_STRUCTURED_BRANCH = "structured"  # linkedin_category
_NAICS_BRANCH = "naics"  # naics_category — the Phase 8B second structured tier
_KEYWORD_BRANCH = "keyword"
# Phase 41 — SEPARATE structured branches for company_type terms. Never
# merged with _STRUCTURED_BRANCH/_NAICS_BRANCH: Explorium's filters AND
# together within one request (confirmed live in Phase 7E/8B), so a
# resolved industry linkedin_category value and a resolved company_type
# linkedin_category value in the SAME call would wrongly require a
# candidate to satisfy both simultaneously — the identical reasoning that
# already keeps the industry structured/naics branches apart from each
# other, applied one more time here.
_STRUCTURED_COMPANY_TYPE_BRANCH = "structured_company_type"  # linkedin_category, company_type terms only
_NAICS_COMPANY_TYPE_BRANCH = "naics_company_type"  # naics_category, company_type terms only

# Phase 11 (industry search -> taxonomy bridge): the Explorium taxonomy
# field name each structured branch queried against — attached to every
# record that branch returns (see execute()'s merge loop) so a downstream
# consumer (app/services/evidence_import.py, app/services/
# hard_icp_validation.py) can tell "this candidate's industry evidence
# came from a real, live-verified exact taxonomy match" apart from "this
# candidate merely happened to mention a keyword." _KEYWORD_BRANCH is
# deliberately absent from this map — a keyword-fallback record NEVER
# receives this tag, by construction, not by a downstream filter having to
# remember to exclude it.
_STRUCTURED_TAXONOMY_FIELD = {_STRUCTURED_BRANCH: "linkedin_category", _NAICS_BRANCH: "naics_category"}
# Phase 41 — the company_type analogue of the map above, deliberately a
# SEPARATE dict (never merged into _STRUCTURED_TAXONOMY_FIELD) so
# execute()'s merge loop can tell "this branch proves an INDUSTRY term"
# apart from "this branch proves a COMPANY_TYPE term" and tag each with
# its own, non-conflatable provenance key set (company_type_match_* vs
# industry_match_*) — a company_type structured match must never be
# readable as industry evidence by any downstream consumer, and vice
# versa.
_STRUCTURED_COMPANY_TYPE_TAXONOMY_FIELD = {
    _STRUCTURED_COMPANY_TYPE_BRANCH: "linkedin_category",
    _NAICS_COMPANY_TYPE_BRANCH: "naics_category",
}

# Phase 13C: a hard ceiling on the TOTAL number of terms ever sent in one
# website_keywords OR-list, mirroring app/services/discovery_strategy.py's
# own MAX_AI_INDUSTRY_TERMS/MAX_AI_COMPANY_TYPE_TERMS docstring ("a hard
# ceiling so a pathological... volume can never blow up the prompt") —
# applied here as a safety bound, not a claim that fewer keyword terms are
# inherently higher quality. Justified by a real, confirmed gap: Phase 10's
# own caps (6 AI industry terms + 4 AI company_type terms = 10) bound only
# the AI-PROPOSED contribution, never the user-typed industries/
# company_types lists, which app/services/icp_normalization.py's own
# _clean_string_list dedups but never caps in count — so today, an
# unbounded user term list plus the AI's contribution can produce an
# unbounded website_keywords OR-list with no ceiling at all. Deliberately
# NOT a "keep only specific terms, drop generic ones" heuristic — this
# codebase has no evidence-backed way to rank term specificity (a Phase 13C
# discovery-quality benchmark found false positives driven by CATEGORY
# BREADTH, not term COUNT — see that benchmark's own findings), so this
# cap truncates in the existing, already-established "user terms first"
# order (see merge_strategy_into_hard_rules's own additive-only ordering)
# rather than attempting any new semantic judgment about which terms are
# "better." A term dropped by this cap is still preserved everywhere else
# (the ICP's own industries/company_types tuples are never mutated) — only
# this one branch's OR-list is bounded.
_MAX_KEYWORD_OR_TERMS = 12


def _dedup_terms_preserve_order(terms: list[str]) -> list[str]:
    """Case/whitespace-insensitive dedup, preserving input order and the
    casing of the first occurrence — the EXACT same normalization
    discipline app/services/icp_normalization.py::_clean_string_list
    already established for the ICP's own hard-rule term lists (reusing
    clean_text().casefold(), the same comparison _lookup_category already
    uses to match a term against Explorium's own taxonomy labels), applied
    here to close a real, confirmed gap: today's plain `if term not in
    list` check only catches an EXACT literal repeat, so "Consumer Goods"
    and "consumer goods" (e.g. one user-typed, one AI-proposed) currently
    both survive as separate, redundant OR-list entries — pure waste, with
    no possible discovery benefit, since Explorium's own website_keywords
    matching cannot distinguish them either."""
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        key = clean_text(term).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(term)
    return result


def _prioritized_keyword_terms(
    terms: list[str], limit: int, term_origin: dict[str, str], combination_terms: tuple[str, ...]
) -> list[str]:
    """Phase 40 — deduped, capped, and (only when truncation actually
    happens) priority-ordered so a long term list doesn't silently drop
    exactly the terms most likely to matter.

    Deduping is unchanged (_dedup_terms_preserve_order, above). When the
    deduped list already fits within `limit`, this is a pure no-op —
    every existing single-branch/no-truncation caller/test sees byte-for-
    byte the same list in the same order as before this phase.

    Only when the deduped list EXCEEDS `limit` does selection change: terms
    are grouped into three priority tiers —
      1. user-typed terms (term_origin[term] == "user", or absent from
         term_origin entirely — the same safe default _origins_for already
         uses for "unknown" origin, since a pre-Phase-13D caller or a term
         term_origin was never told about must never be treated as lower
         priority than a term positively known to be AI-proposed)
      2. AI-proposed COMBINATION terms (Phase 39's
         icp.hard_rules.industry_combination_terms — a phrase the AI
         specifically declared names a compound ICP's full intersection,
         e.g. "Clinical Software" for a Healthcare+SaaS ICP) — the single
         most likely AI-proposed term to make a compound-ICP candidate
         actually corroborate (see app/services/hard_icp_validation.py's
         Phase 39 fix), so these are preserved ahead of a generic AI
         synonym even though both are equally "AI-proposed."
      3. every other AI-proposed term (independent synonyms)
    filled greedily in that tier order, taking as many as fit from each
    tier before moving to the next — this can never expand the cap, only
    change WHICH terms survive it. The final result is then re-sorted back
    into the ORIGINAL relative order the terms appeared in (never reordering
    what Explorium actually receives — "user terms first" positionally is
    unaffected; only which terms are present changes), so this is a
    SELECTION change only, never a request-shape change.

    `combination_terms` is always empty for a pre-Phase-40 caller (a
    default-empty CompanyDiscoveryQuery field), which makes tier 2 a
    permanent no-op for them — this function's tier-1-vs-rest split then
    reduces to exactly the prior "user terms first" truncation order,
    unchanged."""
    deduped = _dedup_terms_preserve_order(terms)
    if len(deduped) <= limit:
        return deduped

    combination_keys = {clean_text(t).casefold() for t in combination_terms}
    original_index = {term: idx for idx, term in enumerate(deduped)}

    def _tier(term: str) -> int:
        key = clean_text(term).casefold()
        origin = term_origin.get(key, "user")  # unknown origin defaults to highest priority, never penalized
        if origin != "ai":
            return 1
        if key in combination_keys:
            return 2
        return 3

    selected: list[str] = []
    for tier in (1, 2, 3):
        if len(selected) >= limit:
            break
        for term in deduped:
            if len(selected) >= limit:
                break
            if term in selected:
                continue
            if _tier(term) == tier:
                selected.append(term)
    selected.sort(key=lambda t: original_index[t])
    return selected


def _origins_for(terms: list[str], term_origin: dict[str, str]) -> list[str]:
    """Phase 13D — looks up each term's user/AI origin from the optional
    caller-supplied map (see execute()'s own term_origin comment), keyed
    the same way _dedup_terms_preserve_order normalizes (clean_text +
    casefold). A term absent from the map (e.g. term_origin was never
    supplied at all, or this specific term predates Phase 13D's tracking)
    reports "unknown" rather than guessing — this must never fabricate an
    origin it doesn't actually know."""
    return [term_origin.get(clean_text(t).casefold(), "unknown") for t in terms]


# Phase 37: attribute keys whose VALUE is a provenance term-list — when the
# same real company (same external_id) is returned by more than one branch
# in one execute() call, these are the keys _merge_cross_branch_attributes
# unions (not overwrites) across both records, so the merged record
# honestly reflects EVERY branch that actually contributed evidence for it.
# Deliberately a fixed, generic list of KEY NAMES, never any term VALUE —
# this makes no industry-specific or ICP-specific assumption of any kind.
_CROSS_BRANCH_TERM_LIST_KEYS = (
    "industry_match_terms",
    "industry_match_term_origins",
    # Live-test audit finding — must be unioned exactly like
    # industry_match_terms above: a candidate genuinely found via TWO
    # structured branches in one call (e.g. linkedin_category AND
    # naics_category each independently resolved) must keep BOTH
    # branches' resolved taxonomy values, never just the first branch
    # merged_records happened to keep.
    "industry_match_resolved_values",
    "keyword_match_terms",
    "keyword_match_term_sources",
    "keyword_match_term_origins",
    # Phase 41 — the company_type analogue of industry_match_terms/
    # industry_match_term_origins above, so a candidate found via BOTH a
    # company_type structured branch AND an industry/keyword branch keeps
    # BOTH branches' provenance on the one merged record, never losing
    # either.
    "company_type_match_terms",
    "company_type_match_term_origins",
)


def _merge_cross_branch_attributes(existing: NormalizedRecord, incoming: NormalizedRecord) -> NormalizedRecord:
    """Combines a LATER branch's record for the SAME real company (same
    external_id, already kept from an EARLIER branch this round) into the
    record already in merged_records — see execute()'s own
    merged_record_index_by_external_id comment for why this exists at all
    (before this phase, the later branch's record, and everything it
    proved, was silently discarded).

    Every _CROSS_BRANCH_TERM_LIST_KEYS-listed attribute present on EITHER
    record is unioned (existing record's list first, then any new items
    from the incoming record, deduplicated by exact value — these are
    already-cleaned ICP term strings, never raw user text needing
    normalization) rather than one overwriting the other, so a company
    found via the Healthcare structured branch AND the SaaS keyword branch
    ends up with BOTH branches' `industry_match_terms`/`keyword_match_terms`
    visible on the same record — this is the concrete, generic signal
    hard_icp_validation.py's cross-branch corroboration check (see that
    module) reads to recognize compound-ICP coverage.

    A scalar (non-list) attribute already present on `existing` is never
    overwritten by `incoming`'s value — the first branch's real, honest
    value for a field like `industry`/`country`/`employee_range` is kept
    as-is; this function only ever ADDS provenance, never fabricates or
    resolves a disagreement between two branches' scalar values (both
    describe the same real company from the same Explorium account, so
    they are expected to agree in practice; if they ever don't, keeping
    the first-seen value is the same "never guess" discipline used
    everywhere else in this codebase). A scalar attribute `existing`
    lacks but `incoming` has is added, since that's new information, not
    a disagreement to arbitrate."""
    merged_attributes = dict(existing.attributes)
    for key, incoming_value in incoming.attributes.items():
        if key in _CROSS_BRANCH_TERM_LIST_KEYS:
            existing_list = merged_attributes.get(key)
            if not isinstance(incoming_value, list):
                continue
            if not isinstance(existing_list, list):
                merged_attributes[key] = list(incoming_value)
                continue
            merged_attributes[key] = existing_list + [v for v in incoming_value if v not in existing_list]
        elif key not in merged_attributes:
            merged_attributes[key] = incoming_value
    return existing.model_copy(update={"attributes": merged_attributes})


# Sentinel marking a branch as exhausted inside the packed cursor JSON
# object, distinct from "not yet attempted" (branch name simply absent from
# the dict) and "mid-pagination" (branch name present with a real Explorium
# cursor string) — this is what lets execute() remember, across separate
# calls/rounds, that (for example) the structured branch ran out on round 2
# while the keyword branch is still producing results on round 3, without
# ever re-querying the exhausted branch again. Never a real Explorium
# cursor value (Explorium's own next_cursor tokens are opaque but always
# contain a "|" per every live-observed example, e.g.
# "0.0|24537742.0|8ca8..." — this sentinel deliberately avoids that shape,
# though the check is by exact string identity, not shape, so collision
# risk is not a concern either way).
_BRANCH_EXHAUSTED = "__exhausted__"

# Phase 40: replaces the original even-split-across-branches page-size
# budget (each branch got at most page_size / branch_count) — that scheme
# meant a compound ICP with more active branches got PROPORTIONALLY FEWER
# results per branch per round (e.g. limit=20 with 3 branches active gave
# each branch only ~7), directly undercutting Phase 2's own compound-ICP
# corroboration fix (a candidate needs a genuine hit from more than one
# branch to corroborate, and starving each branch's page size makes that
# less likely to happen in any given round, not more). Each active branch
# now requests up to this ceiling directly, with no division — this can
# make ONE round return up to branch_count times more raw candidates than
# before, but never uncontrolled: still hard-capped at Explorium's own
# documented, live-verified page_size ceiling (100) per branch per call,
# still gated by the SAME unchanged downstream dedup
# (merged_record_index_by_external_id, batch_orchestration.py's
# already-seen-external-id tracking) and hard ICP validation — more RAW
# candidates flow into an unchanged pipeline, never more false PASSes.
_EXPLORIUM_PAGE_SIZE_CEILING = 100

# Explorium response fields this adapter maps into NormalizedRecord.attributes.
# Only present when Explorium's own response actually includes them for a
# given business — never filled with None/a guess to "complete" the shape.
# Keys on the right follow this codebase's own established attribute
# vocabulary (see app/providers/mocks.py's MockCompanyDataProvider and
# app/services/{hard_icp_validation,evidence_import}.py's expected field
# names), not Explorium's own field names, so the existing pipeline reads
# them the same way it already reads mock/Apollo-supplied attributes.
_BUSINESS_ATTRIBUTE_MAP: dict[str, str] = {
    "domain": "domain",
    "country_name": "country",
    "naics_description": "industry",
    # yearly_revenue_range is a documented Explorium response field
    # (confirmed via https://developers.explorium.ai/v2/businesses/
    # fetch_businesses.md) already present in every real response body at
    # no extra API-call cost — Phase 7G maps it through instead of
    # discarding it, using the same "string range, not a fabricated
    # number" discipline already applied to employee_range below.
    "yearly_revenue_range": "revenue_range",
    # employee_range and linkedin_id are handled explicitly below (module
    # docstring explains why they can't use this simple 1:1 map).
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_employee_size_filter(min_employees: int | None, max_employees: int | None) -> list[str] | None:
    """Explorium's company_size filter takes discrete documented bucket
    strings (e.g. "11-50", "51-200"), not an arbitrary numeric range.

    Phase 7G fix: this previously only forwarded buckets FULLY CONTAINED
    within the ICP's [min, max] bounds. For a real, arbitrary range like
    15-150, no documented bucket is fully contained (11-50 fails since
    11 < 15; 51-200 fails since 200 > 150), so the function silently
    returned None and NO size filter was sent at all — companies of any
    size, including 10001+, passed straight through discovery
    unconstrained (confirmed live in the Phase 7F Deep Tech test: IBM,
    Meta, Deloitte all appeared with no employee_count evidence rejecting
    them at discovery time).

    Now any bucket that OVERLAPS [min, max] at all is forwarded — this
    intentionally admits some companies outside the exact range (e.g. a
    51-200 bucket for a 15-150 request also admits 151-200), but it is
    the only correct behavior given Explorium's fixed buckets: never
    silently drop the constraint entirely. The existing hard ICP
    validation (unchanged) performs the exact numeric check against each
    company's real employee evidence and rejects true mismatches — this
    filter's only job is to keep discovery from returning wildly
    oversized/undersized companies as candidates in the first place, not
    to be the final authority on fit."""
    if min_employees is None and max_employees is None:
        return None

    # Explorium's documented company_size buckets, each as (low, high);
    # high=None means open-ended (e.g. "10001+").
    buckets: tuple[tuple[str, int, int | None], ...] = (
        ("1-10", 1, 10),
        ("11-50", 11, 50),
        ("51-200", 51, 200),
        ("201-500", 201, 500),
        ("501-1000", 501, 1000),
        ("1001-5000", 1001, 5000),
        ("5001-10000", 5001, 10000),
        ("10001+", 10001, None),
    )

    lo = min_employees if min_employees is not None else 0
    hi = max_employees

    if hi is not None and lo > hi:
        # Invalid/empty range — never silently fall back to "no filter"
        # (which would admit every company size); there is no honest
        # bucket selection for an inverted range, so send none of them,
        # same as today's behavior for this specific invalid-input case.
        return None

    selected = [
        label
        for label, bucket_lo, bucket_hi in buckets
        # Overlap test: the bucket's range and [lo, hi] intersect.
        if (bucket_hi is None or bucket_hi >= lo) and (hi is None or bucket_lo <= hi)
    ]
    return selected or None


class ExploriumCompanyDiscoveryProvider(ProviderAdapter):
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.explorium.ai/v2",
        provider_id: str = "explorium-company-discovery-v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Explorium",
            capabilities={ProviderCapability.COMPANY_DISCOVERY},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # The real, live-verified autocomplete endpoint lives under /v1, not
        # /v2 (confirmed: GET https://api.explorium.ai/v1/businesses/autocomplete
        # returns real data; the /v2 host has no such path). Derived from the
        # same configured host as _base_url, never a second, separately
        # configured setting — there is exactly one Explorium host.
        self._autocomplete_base_url = (
            self._base_url[: -len("/v2")] + "/v1" if self._base_url.endswith("/v2") else self._base_url
        )
        # Per-instance caches: ExploriumCompanyDiscoveryProvider is a
        # module-level singleton (app/providers/default_registry.py), so
        # these persist for the process lifetime — an ICP's industry terms
        # are checked against the real autocomplete endpoint at most once
        # each per taxonomy, never repeated on every "Find More" round for
        # the same ICP. Keyed by the exact cleaned term; value is the
        # matched category value, or None if confirmed no exact match.
        # Two separate caches (Phase 8B) since a term can independently
        # miss on one taxonomy and match on the other — never conflated.
        self._category_match_cache: dict[str, str | None] = {}
        self._naics_match_cache: dict[str, str | None] = {}

    def _build_base_filters(self, query: dict[str, Any]) -> dict[str, Any]:
        """country_code/company_size only — shared by every branch. Never
        includes industry: each branch (see _plan_industry_branches) adds
        its own industry-related filter on top of a copy of this base."""
        # Explorium's actual /v2/businesses endpoint requires each filter
        # value to be an object with a "values" key — a bare array (as an
        # earlier version of this adapter sent) is rejected with a 422
        # ("Input must be a dictionary with a 'values' key"). Confirmed
        # directly against the real API, not assumed from documentation
        # alone, since the published reference examples do not show this
        # wrapper explicitly.
        filters: dict[str, Any] = {}

        geography_codes = query.get("geography_codes") or []
        if geography_codes:
            # Explorium's country_code filter expects lowercase ISO alpha-2
            # codes; this codebase's own geography codes are already ISO
            # alpha-2 (see app/services/icp_normalization.py), just
            # normalized to lowercase for Explorium's documented convention.
            filters["country_code"] = {"values": [str(code).lower() for code in geography_codes]}

        size_buckets = _build_employee_size_filter(query.get("min_employees"), query.get("max_employees"))
        if size_buckets:
            filters["company_size"] = {"values": size_buckets}

        return filters

    def _lookup_category(self, field: str, term: str, cache: dict[str, str | None]) -> str | None:
        """Live GET .../v1/businesses/autocomplete?field=<field> for one
        ICP term against one Explorium taxonomy. Generic across taxonomies
        (Phase 7E's linkedin_category, Phase 8B's naics_category) — same
        endpoint, same request shape, same {query, label, value} response
        shape, confirmed via Explorium's own documentation for both
        field values. Returns the matched category value ONLY on an exact
        (clean_text + casefold) match against the returned `label` — never
        the first/closest suggestion, never a broader or narrower
        category, since accepting anything less than an exact match would
        be exactly the undefensible guessing this codebase has always
        refused to do for industry filtering (confirmed live for
        linkedin_category: "Healthcare" -> exact "Healthcare" match;
        "Health resort" or other near-miss suggestions for a different
        query must never be silently accepted as if they were the term the
        user actually typed). This same exact-match-on-label discipline
        applies identically to naics_category, where `value` happens to be
        a numeric NAICS code rather than a slug — matching is still always
        against the human-readable `label`, never the code. Any failure
        (timeout, non-200, network error, malformed body) is treated as
        "no match" for THIS call — an autocomplete outage must degrade to
        the next tier (naics, then keyword), never abort discovery.

        Phase 13A: caching is scoped to a REAL, CONFIRMED API ANSWER only —
        either a genuine exact match, or a genuine "no exact match" (a real
        HTTP 200 whose suggestions were checked and none matched). A
        transient failure (timeout, network error, non-200, a malformed/
        non-JSON body) is NEVER cached: this codebase's autocomplete cache
        lives for the lifetime of the whole process (this provider instance
        is a module-level singleton — see app/providers/default_registry.py
        — shared across every ICP/batch, not just one), so caching a
        transient failure identically to a genuine no-match would silently
        and PERMANENTLY demote that term to the low-precision keyword
        fallback tier for every future ICP that ever uses it, until the
        process restarts — confirmed as a real, previously-undetected
        precision risk (Phase 13 audit). The very next lookup for the same
        term, after a transient failure, retries against the real API
        exactly as if nothing had been cached at all.

        Cached per-instance in the caller-supplied cache dict, keyed by
        cleaned term — a separate cache per taxonomy, since the same term
        can independently match one and miss the other."""
        cleaned = clean_text(term).casefold()
        if cleaned in cache:
            return cache[cleaned]

        matched: str | None = None
        started = time.monotonic()
        outcome = "matched"
        confirmed_answer = False  # Phase 13A: only a real, confirmed API answer is ever cached
        try:
            response = httpx.get(
                f"{self._autocomplete_base_url}/businesses/autocomplete",
                params={"field": field, "query": term},
                headers={"API_KEY": self._api_key},
                timeout=self._timeout_seconds,
            )
            if response.status_code == 200:
                for suggestion in response.json():
                    label = suggestion.get("label")
                    value = suggestion.get("value")
                    if isinstance(label, str) and isinstance(value, str) and clean_text(label).casefold() == cleaned:
                        matched = value
                        break
                if matched is None:
                    outcome = "no_exact_match"
                confirmed_answer = True  # a real 200 with a parseable body — genuine, cacheable answer either way
            else:
                outcome = f"http_{response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a non-JSON body; both cases fall through to
            # "no match found" for THIS call — never raised into execute(),
            # and (Phase 13A) never cached, since neither is a confirmed
            # answer from Explorium.
            matched = None
            outcome = f"error:{type(exc).__name__}"
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            # Phase 7I: DEBUG-level timing only, never the API key or full
            # response body — lets a slow/failing autocomplete lookup be
            # distinguished from a slow/failing main search call after the
            # fact, which the Phase 7H live test had no way to do.
            logger.debug(
                "Explorium autocomplete %s term=%r outcome=%s elapsed_ms=%.0f cached=%s",
                field,
                term,
                outcome,
                elapsed_ms,
                confirmed_answer,
            )

        if confirmed_answer:
            cache[cleaned] = matched
        return matched

    def _lookup_linkedin_category(self, term: str) -> str | None:
        return self._lookup_category("linkedin_category", term, self._category_match_cache)

    def _lookup_naics_category(self, term: str) -> str | None:
        return self._lookup_category("naics_category", term, self._naics_match_cache)

    def _plan_industry_branches(
        self, industries: tuple[str, ...]
    ) -> tuple[list[str], list[str], list[str], dict[str, str]]:
        """Splits the ICP's industry terms into (linkedin_values,
        naics_values, keyword_terms) — never ICP-specific, the same rule
        runs for whatever industries the request in hand actually states.
        Resolution order per term, exact-match-only at every tier, never
        fuzzy/guessed: (1) linkedin_category, (2) if no exact match,
        naics_category, (3) if neither matches, the term falls to
        keyword_terms unchanged — exactly preserving the pre-Phase-8B
        website_keywords behavior for anything without ANY defensible
        structured match in either taxonomy.

        Phase 37 addition: also returns `term_branch_map` — the ICP's own
        ORIGINAL term string (never the resolved taxonomy label) mapped to
        which branch name it resolved into (_STRUCTURED_BRANCH,
        _NAICS_BRANCH, or _KEYWORD_BRANCH). This is genuine, already-known
        per-term information this method always computed internally (the
        `for term in industries` loop below) but previously discarded —
        purely additive, no new resolution logic, no fuzzy matching. This
        is what lets execute() tag each branch's records with EXACTLY
        which ICP terms resolved into that specific branch, instead of the
        prior "the whole ICP industries tuple was in play" confound — see
        execute()'s own industry_match_terms comment. Needed so
        hard_icp_validation.py can recognize when a compound ICP's
        DIFFERENT terms resolved into DIFFERENT branches and later
        corroborate across them for the SAME candidate, rather than only
        ever requiring one branch to structurally cover every term (which
        is impossible by construction whenever terms genuinely resolve to
        different taxonomy fields — see hard_icp_validation.py's own
        _bridged_industry_terms docstring)."""
        started = time.monotonic()
        linkedin_values: list[str] = []
        naics_values: list[str] = []
        keyword_terms: list[str] = []
        term_branch_map: dict[str, str] = {}
        for term in industries:
            linkedin_match = self._lookup_linkedin_category(term)
            if linkedin_match is not None:
                if linkedin_match not in linkedin_values:
                    linkedin_values.append(linkedin_match)
                term_branch_map[term] = _STRUCTURED_BRANCH
                continue
            naics_match = self._lookup_naics_category(term)
            if naics_match is not None:
                if naics_match not in naics_values:
                    naics_values.append(naics_match)
                term_branch_map[term] = _NAICS_BRANCH
                continue
            keyword_terms.append(term)
            term_branch_map[term] = _KEYWORD_BRANCH
        elapsed_ms = (time.monotonic() - started) * 1000
        # Phase 7I/8B: total time for ALL of this ICP's industry-term
        # autocomplete lookups combined, across both taxonomies (individual
        # per-term/per-taxonomy timings are logged inside _lookup_category)
        # — makes it possible to see, e.g., "8 terms took 6800ms total" as
        # a single summary line rather than having to sum separate DEBUG
        # lines by hand.
        logger.debug(
            "Explorium industry branch planning terms=%d linkedin=%d naics=%d keyword=%d elapsed_ms=%.0f",
            len(industries),
            len(linkedin_values),
            len(naics_values),
            len(keyword_terms),
            elapsed_ms,
        )
        return linkedin_values, naics_values, keyword_terms, term_branch_map

    def _plan_company_type_branches(
        self, company_types: tuple[str, ...]
    ) -> tuple[list[str], list[str], list[str], dict[str, str]]:
        """Phase 41 — the company_type analogue of _plan_industry_branches.

        Before this phase, CompanyDiscoveryQuery.company_types NEVER
        attempted a structured taxonomy lookup at all — every term went
        straight to the low-precision website_keywords OR-list regardless
        of whether a real, exact linkedin_category/naics_category match
        existed for it (unlike industries, which always get the
        structured-first attempt). This was based on this module's own
        confirmed-live finding that INDUSTRY-shaped compound terms like
        "FMCG", "D2C Brands", "OTT Platforms", and "Microdrama Companies"
        return zero autocomplete results (see this module's own docstring)
        — but that finding was never actually tested for company_type-
        shaped terms like "Startup", "PE-backed", or "D2C" on their own,
        which may well have a real LinkedIn/NAICS category counterpart
        (e.g. "Startup" is a plausible real linkedin_category label) that
        was simply never tried.

        Reuses the EXACT SAME live autocomplete mechanism, exact-match-
        only discipline, and per-instance cache (_category_match_cache/
        _naics_match_cache — shared with _plan_industry_branches, since
        the same term string resolves identically regardless of which ICP
        field it came from) as the industry planner — no new fuzzy
        matching, no new taxonomy, no invented category. A company_type
        term that doesn't resolve structurally still falls to
        keyword_terms unchanged, preserving today's fallback behavior for
        every term without a real structured match."""
        started = time.monotonic()
        linkedin_values: list[str] = []
        naics_values: list[str] = []
        keyword_terms: list[str] = []
        term_branch_map: dict[str, str] = {}
        for term in company_types:
            linkedin_match = self._lookup_linkedin_category(term)
            if linkedin_match is not None:
                if linkedin_match not in linkedin_values:
                    linkedin_values.append(linkedin_match)
                term_branch_map[term] = _STRUCTURED_COMPANY_TYPE_BRANCH
                continue
            naics_match = self._lookup_naics_category(term)
            if naics_match is not None:
                if naics_match not in naics_values:
                    naics_values.append(naics_match)
                term_branch_map[term] = _NAICS_COMPANY_TYPE_BRANCH
                continue
            keyword_terms.append(term)
            term_branch_map[term] = _KEYWORD_BRANCH
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.debug(
            "Explorium company_type branch planning terms=%d linkedin=%d naics=%d keyword=%d elapsed_ms=%.0f",
            len(company_types),
            len(linkedin_values),
            len(naics_values),
            len(keyword_terms),
            elapsed_ms,
        )
        return linkedin_values, naics_values, keyword_terms, term_branch_map

    def _error_response(self, request: ProviderRequest, code: str, message: str) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=False,
            error=ProviderError(code=code, message=message, retryable=False),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=False,
            ),
        )

    def _run_one_branch(
        self, request: ProviderRequest, filters: dict[str, Any], cursor: str | None, page_size: int
    ) -> ProviderResponse:
        """One real POST /v2/businesses call for one branch's filter dict.
        Returns a failed ProviderResponse (success=False) on a genuine
        auth/credits failure — the exact same handling regardless of which
        branch hit it, since both branches share one Explorium account/key
        and there is nothing branch-specific about an auth or credits
        failure; execute() returns that failure immediately rather than
        trying the remaining branches. A network/5xx failure is raised
        instead, converted by ProviderAdapter.run() into a clean
        PROVIDER_ERROR exactly as before this method existed — the
        difference from before is only that execute() now may make 1 or 2
        of these calls instead of always 1."""
        body: dict[str, Any] = {"mode": "full", "page_size": page_size}
        if filters:
            body["filters"] = filters
        if cursor:
            # Confirmed via live testing against the real API: Explorium's
            # continuation token is a bare top-level "next_cursor" string
            # field, never nested under "page" — sending a top-level "page"
            # field of any shape (despite being documented) is rejected with
            # 422 "extra fields not permitted".
            body["next_cursor"] = cursor

        search_started = time.monotonic()
        try:
            response = httpx.post(
                f"{self._base_url}/businesses",
                json=body,
                headers={"API_KEY": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Phase 7I: log the failure and its timing before re-raising —
            # the exact same RuntimeError is still raised below, unchanged,
            # so ProviderAdapter.run() converts it into a clean PROVIDER_ERROR
            # exactly as before; this only adds a DEBUG line so a slow/failed
            # main search call is distinguishable from slow/failed
            # autocomplete lookups after the fact (see module docstring).
            elapsed_ms = (time.monotonic() - search_started) * 1000
            logger.debug(
                "Explorium POST /businesses failed error=%s elapsed_ms=%.0f", type(exc).__name__, elapsed_ms
            )
            # Network-level failure (timeout, connection error, ...) — let
            # ProviderAdapter.run() convert this into a clean, generic
            # PROVIDER_ERROR response. The exception message never contains
            # self._api_key (httpx does not echo request headers in its
            # exception messages).
            raise RuntimeError(f"Explorium request failed: {exc}") from exc

        logger.debug(
            "Explorium POST /businesses status=%d elapsed_ms=%.0f", response.status_code, (time.monotonic() - search_started) * 1000
        )

        if response.status_code == 401:
            return self._error_response(request, EXPLORIUM_AUTH_FAILED, "Explorium rejected the configured API key.")

        if response.status_code == 403:
            # A 403 from Explorium is not always an invalid key — a real,
            # live-observed case is the account running out of credits,
            # which returns a distinct message body (confirmed live:
            # {"details":"You have insufficient credits to perform this
            # operation. Please purchase additional credits.",...}). These
            # are two genuinely different, non-retryable failures a caller
            # must be able to tell apart (one is a config bug, the other
            # requires the account to be topped up) — collapsing them into
            # one generic auth-failed code would misreport which one
            # happened. Detection is a case-insensitive substring match on
            # the raw body text, not a strict JSON-shape assumption, since
            # no other 403 body shape is documented/verified.
            if "credit" in response.text.lower():
                return self._error_response(
                    request, EXPLORIUM_CREDITS_EXHAUSTED, "Explorium account is out of credits for this request."
                )
            return self._error_response(request, EXPLORIUM_AUTH_FAILED, "Explorium rejected the configured API key.")

        if response.status_code >= 400:
            # Any other HTTP error (rate limit, bad request, server error) —
            # raise with only the status code, never the response body
            # (which could theoretically echo request params back).
            raise RuntimeError(f"Explorium returned HTTP {response.status_code}")

        body_json = response.json()
        businesses = body_json.get("data") or []
        # Phase 24 one-shot E2E diagnostic ONLY — observability, no
        # behavior change: does not touch `businesses`, `records`, or any
        # returned value. Names/domains are already in the SAME response
        # body this call already received; logging them makes zero
        # additional network calls. Intended to be removed after the
        # one-shot live test this was added for.
        logger.info(
            "explorium_search_result count=%d total_results=%s names=%s",
            len(businesses),
            body_json.get("total_results"),
            [b.get("name") for b in businesses],
        )

        records: list[NormalizedRecord] = []
        for business in businesses:
            name = business.get("name")
            if not name:
                # A record with no name at all isn't a usable candidate
                # company — skip rather than fabricate a placeholder name.
                continue

            attributes: dict[str, Any] = {}
            for explorium_field, our_field in _BUSINESS_ATTRIBUTE_MAP.items():
                value = business.get(explorium_field)
                if value:
                    attributes[our_field] = value

            employee_range = business.get("number_of_employees_range")
            if employee_range:
                attributes["employee_range"] = employee_range

            linkedin_profile = business.get("linkedin_profile")
            if linkedin_profile:
                attributes["linkedin_id"] = linkedin_profile

            # Phase 15: total_results is a real, documented field on
            # Explorium's own response envelope (confirmed present in the
            # exact same body_json this method already reads "page" and
            # "data" from — see this method's own pagination-signal
            # comment below, which already references it) that this
            # adapter has NEVER captured until now — pure unused data, not
            # an invented field. It reflects how many companies matched
            # THIS BRANCH's entire filtered request (category + geography
            # + employee size together, whatever this branch's filters
            # were) — attached here, per record, exactly like every other
            # per-record attribute this method already sets, so
            # execute()'s merge loop (which knows the branch's own
            # taxonomy type and how many distinct categories fed it) can
            # decide whether/how this number is safely attributable to a
            # single category. This method itself makes no such judgment —
            # it is branch-agnostic by design (module docstring) and stays
            # that way.
            total_results = body_json.get("total_results")
            if isinstance(total_results, int):
                attributes["_branch_total_results"] = total_results

            records.append(
                NormalizedRecord(
                    external_id=str(business.get("business_id") or ""),
                    name=name,
                    attributes=attributes,
                )
            )

        # Pagination signal, confirmed live: a query with nothing left to
        # return sends the whole "page" envelope as null (not just an empty
        # page object), alongside "total_results": null and "data": []. A
        # normal page includes "page": {"size": ..., "next_cursor": ...}.
        # Treat exhaustion as "no cursor to continue with" rather than only
        # "page is null" — this also correctly covers a last page that has
        # results but no further next_cursor, which was not separately
        # live-tested but is the strictly safer reading of "no more pages".
        page_obj = body_json.get("page")
        next_cursor = (page_obj or {}).get("next_cursor")
        exhausted = next_cursor is None

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(records),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=False,
            ),
            cursor=next_cursor,
            exhausted=exhausted,
        )

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        base_filters = self._build_base_filters(request.query)
        industries = tuple(str(v) for v in (request.query.get("industries") or []))
        # Phase 7N audit finding: CompanyDiscoveryQuery.company_types is
        # populated end-to-end from a real, generic frontend field ("e.g.
        # D2C, PE-backed, Startup" — HardRulesSection.tsx) all the way to
        # this adapter's own request.query, but was never read here at
        # all — silently dropped, the one genuine plumbing gap found in
        # that audit (employee_range/geography/industry were all already
        # confirmed correct end-to-end). Explorium has no structured
        # "company type" taxonomy/filter to map these onto (unlike
        # industries, which get a real linkedin_category check), so these
        # terms join the SAME keyword-fallback OR-list already used for
        # unmatched industry terms — never a new filter type, never a
        # fabricated structured mapping, and never ICP-specific (this
        # reads whatever company_types the current request actually
        # states, exactly like industries).
        company_types = tuple(str(v) for v in (request.query.get("company_types") or []))
        # Phase 40: the subset of `industries` that is a Phase 39 AI-declared
        # compound-intersection phrase — see CompanyDiscoveryQuery.
        # combination_terms's own docstring. Read only by
        # _prioritized_keyword_terms below; absent for every pre-Phase-40
        # caller (defaults to an empty tuple), which keeps this a strict,
        # additive extension with zero behavior change for them.
        combination_terms = tuple(str(v) for v in (request.query.get("combination_terms") or []))

        # Phase 13D: unconditionally bound (mirroring `company_types`
        # immediately above) so the keyword_match_term_sources tagging
        # further down can always safely reference it, regardless of
        # which of the three website_keywords construction paths below
        # actually ran. Only the `if industries:` branch ever reassigns
        # this (from _plan_industry_branches's own return value) — the
        # `elif company_types:` and keyword-less `else:` paths correctly
        # leave it empty, since neither has any unmatched INDUSTRY term at
        # all to report a source for.
        keyword_terms: list[str] = []

        # Phase 13D: an OPTIONAL, purely additive provenance map — {cleaned
        # term (lowercased): "user" | "ai"} — built and supplied by the
        # CALLER (see app/services/discovery_strategy.py::
        # build_term_origin_map and its one real call site,
        # app/services/batch_orchestration.py) from information that only
        # exists BEFORE merge_strategy_into_hard_rules collapses user and
        # AI-proposed terms into one indistinguishable
        # CanonicalHardRules.industries/company_types tuple. Mirrors
        # run_company_discovery's own provider_order/cursors precedent
        # ("OPTIONAL, purely additive hook... defaulting preserves this
        # function's exact prior behavior for every existing caller") —
        # when absent (every pre-Phase-13D caller and every existing test),
        # every read of this map below is a no-op and record tagging is
        # byte-for-byte unchanged from before this phase. This is NEVER
        # sent to Explorium's real API — it is read only for tagging
        # NormalizedRecord.attributes further down in this method, exactly
        # like industry_match_terms/keyword_match_terms already are.
        term_origin: dict[str, str] = request.query.get("term_origin") or {}

        limit = request.query.get("limit")
        # Explorium's documented and live-tested page_size ceiling is 100 —
        # a prior version of this adapter allowed up to 500, which was never
        # actually verified against the real API and is wrong. Phase 40:
        # this is now the per-BRANCH ceiling (see
        # _EXPLORIUM_PAGE_SIZE_CEILING's own comment for why it is no
        # longer divided across active branches) as well as the
        # keyword-less/single-branch request's page size, exactly as
        # before this phase for that case.
        total_page_size = min(int(limit), _EXPLORIUM_PAGE_SIZE_CEILING) if limit else 20

        # Multi-branch state, opaque to every caller outside this method —
        # packed into/unpacked from the single ProviderRequest.cursor /
        # ProviderResponse.cursor string this adapter already exchanges with
        # company_discovery.py/batch_orchestration.py. Per branch name:
        # absent = not yet attempted (first page); a real Explorium cursor
        # string = mid-pagination; _BRANCH_EXHAUSTED = confirmed exhausted on
        # a prior round, never queried again. A cursor from a single-branch
        # (pre-Phase-7E, or industries-less) round is a plain Explorium
        # token, never JSON — incoming_branch_state falls back to treating
        # it as the keyword branch's cursor in that case (the only branch a
        # plain industries-less request ever uses).
        incoming_branch_state: dict[str, str] = {}
        if request.cursor:
            try:
                parsed = json.loads(request.cursor)
                if isinstance(parsed, dict):
                    incoming_branch_state = {k: v for k, v in parsed.items() if isinstance(v, str)}
            except ValueError:
                incoming_branch_state = {_KEYWORD_BRANCH: request.cursor}

        branch_filters: dict[str, dict[str, Any]] = {}
        term_branch_map: dict[str, str] = {}
        # Phase 41: company_types now ALWAYS gets the same structured-
        # taxonomy-first attempt industries already get — see
        # _plan_company_type_branches's own docstring for why this was
        # never tried before and why it's safe to try now. Computed
        # unconditionally (a no-op when company_types is empty) so both
        # the `if industries:` and the industries-less paths below share
        # ONE code path for it instead of two, closing the exact kind of
        # "one path has a fix, the other doesn't" latent gap Phase 13C's
        # own dedup+cap fix had to separately patch in both branches.
        company_type_term_branch_map: dict[str, str] = {}
        company_type_linkedin_values: list[str] = []
        company_type_naics_values: list[str] = []
        company_type_keyword_terms: list[str] = list(company_types)
        if company_types:
            (
                company_type_linkedin_values,
                company_type_naics_values,
                company_type_keyword_terms,
                company_type_term_branch_map,
            ) = self._plan_company_type_branches(company_types)
            if company_type_linkedin_values:
                structured_company_type = dict(base_filters)
                structured_company_type["linkedin_category"] = {"values": company_type_linkedin_values}
                branch_filters[_STRUCTURED_COMPANY_TYPE_BRANCH] = structured_company_type
            if company_type_naics_values:
                naics_company_type = dict(base_filters)
                naics_company_type["naics_category"] = {"values": company_type_naics_values}
                branch_filters[_NAICS_COMPANY_TYPE_BRANCH] = naics_company_type
        term_branch_map.update(company_type_term_branch_map)

        if industries:
            linkedin_values, naics_values, keyword_terms, industry_term_branch_map = self._plan_industry_branches(industries)
            term_branch_map.update(industry_term_branch_map)
            if linkedin_values:
                structured = dict(base_filters)
                structured["linkedin_category"] = {"values": linkedin_values}
                branch_filters[_STRUCTURED_BRANCH] = structured
            if naics_values:
                # A separate branch, never merged with the linkedin_category
                # branch or the keyword branch — Explorium's filters AND
                # together within one request (confirmed live in Phase 7E),
                # so combining naics_category with linkedin_category in one
                # call would wrongly require a company to satisfy both
                # simultaneously, excluding real matches for either side.
                naics = dict(base_filters)
                naics["naics_category"] = {"values": naics_values}
                branch_filters[_NAICS_BRANCH] = naics
            # Phase 41: only the company_type terms that did NOT resolve
            # structurally (company_type_keyword_terms) join the keyword
            # branch's OR-list now — a term that resolved via
            # _plan_company_type_branches already has its own dedicated
            # structured/naics branch above and must never ALSO be sent
            # via keyword (that would be redundant, not additive, since
            # Explorium OR-combines branches at the merge layer already).
            all_keyword_terms = list(keyword_terms)
            for term in company_type_keyword_terms:
                if term not in all_keyword_terms:
                    all_keyword_terms.append(term)
            # Phase 13C: case/whitespace-insensitive dedup (closes a real
            # gap — the check above only catches an exact literal repeat)
            # then a conservative total-size cap, both in the SAME
            # preserved order used everywhere else in this method — see
            # _dedup_terms_preserve_order's and _MAX_KEYWORD_OR_TERMS's own
            # docstrings for why this is a safety bound, not a quality
            # heuristic. Phase 40: when the cap actually truncates,
            # _prioritized_keyword_terms keeps user terms first (unchanged)
            # but no longer drops AI-proposed compound-intersection phrases
            # ahead of other, lower-value AI terms purely by list position —
            # see that function's own docstring.
            all_keyword_terms = _prioritized_keyword_terms(all_keyword_terms, _MAX_KEYWORD_OR_TERMS, term_origin, combination_terms)
            if all_keyword_terms:
                keyword = dict(base_filters)
                # "or" (not "and") because the ICP's industries/company
                # types are alternatives, never a simultaneous requirement.
                # Confirmed live against the real API: the operator value
                # must be lowercase ("or"/"and") — Explorium's own
                # published docs show uppercase, which the live API
                # actually rejects with a 422.
                keyword["website_keywords"] = {"values": all_keyword_terms, "operator": "or"}
                branch_filters[_KEYWORD_BRANCH] = keyword
        elif company_types:
            # No industries stated, but company_types is. Phase 41: only
            # the company_type terms that didn't resolve structurally
            # (company_type_keyword_terms, computed above) still fall to
            # keyword — a resolved term already has its own branch and
            # must not also be duplicated into the OR-list.
            if company_type_keyword_terms:
                keyword = dict(base_filters)
                keyword["website_keywords"] = {
                    "values": _dedup_terms_preserve_order(list(company_type_keyword_terms))[:_MAX_KEYWORD_OR_TERMS],
                    "operator": "or",
                }
                branch_filters[_KEYWORD_BRANCH] = keyword
        else:
            # Neither industries nor company_types stated — exactly
            # today's single, keyword-less request.
            branch_filters[_KEYWORD_BRANCH] = dict(base_filters)

        # A branch already confirmed exhausted on a prior round is never
        # queried again — the per-branch analogue of
        # batch_orchestration.py's existing "an exhausted provider is never
        # asked again" rule, applied one level down.
        active_branch_names = [
            name for name in branch_filters if incoming_branch_state.get(name) != _BRANCH_EXHAUSTED
        ]

        out_branch_state = dict(incoming_branch_state)

        if not active_branch_names:
            # Every branch this request would otherwise use is already
            # exhausted — an honest, immediate "nothing more" response
            # rather than a 0-branch call.
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    retrieved_at=_now(),
                    is_mock=False,
                ),
                cursor=None,
                exhausted=True,
            )

        # Phase 40: each active branch requests up to total_page_size
        # directly — see _EXPLORIUM_PAGE_SIZE_CEILING's own comment for why
        # the original even-split-across-branches scheme was removed (it
        # proportionally starved every branch as branch count grew, which
        # is exactly backwards for a compound ICP that NEEDS multiple
        # branches to corroborate). Still bounded: each branch's own
        # request is capped at Explorium's real page_size ceiling
        # (total_page_size itself is already min(limit, 100) — see above),
        # and every branch's results still flow through the SAME unchanged
        # cross-branch merge/dedup and downstream hard validation.
        per_branch_page_sizes = {name: total_page_size for name in active_branch_names}

        merged_records: list[NormalizedRecord] = []
        # Phase 37: maps external_id -> its index in merged_records, so a
        # LATER branch's record for a company already kept from an EARLIER
        # branch can have its provenance MERGED into the kept record
        # instead of being silently discarded (see the merge block below).
        # Before this phase, a company genuinely found via both the
        # Healthcare structured branch AND the SaaS keyword branch — the
        # exact "same canonical company, corroborated by multiple
        # branches" scenario hard_icp_validation.py needs to recognize —
        # would have its second branch's evidence dropped entirely here,
        # making that corroboration structurally invisible to every
        # downstream stage no matter what validation logic existed.
        merged_record_index_by_external_id: dict[str, int] = {}

        for branch_name in active_branch_names:
            incoming_cursor = incoming_branch_state.get(branch_name)
            branch_response = self._run_one_branch(
                request,
                branch_filters[branch_name],
                incoming_cursor if incoming_cursor != _BRANCH_EXHAUSTED else None,
                per_branch_page_sizes[branch_name],
            )
            if not branch_response.success:
                # An auth/credits failure applies to the whole account, not
                # just this branch — surface it immediately exactly as a
                # single-branch call always has, never partially succeed.
                return branch_response
            taxonomy_field = _STRUCTURED_TAXONOMY_FIELD.get(branch_name)
            for record in branch_response.data:
                if taxonomy_field is not None and "industry" in record.attributes:
                    # Phase 11 bridge tag: this record came from a real,
                    # live-verified exact taxonomy match (_lookup_category),
                    # not a keyword/substring guess — record.attributes
                    # already carries the RESOLVED label (via
                    # naics_description in _BUSINESS_ATTRIBUTE_MAP), which
                    # can legitimately differ, textually, from the ICP term
                    # that found it (e.g. ICP term "Healthcare" resolving to
                    # "General Medical and Surgical Hospitals"). Tagging
                    # which taxonomy field matched, plus the ICP's own
                    # industries terms that were in play for this branch,
                    # lets hard_icp_validation.py bridge that gap without
                    # any fuzzy matching — see that module's own comments.
                    # `industries` (the ICP's original terms, unchanged
                    # since execute()'s own top) is reused here rather than
                    # re-derived; it is never itself sent as a filter value
                    # (only its resolved linkedin_values/naics_values are),
                    # so attaching it here is the only place this specific
                    # record can still be tied back to it.
                    # Phase 15 (Phase 14 audit finding): capture, but do
                    # NOT classify as "broad" vs "strong" — the data to
                    # safely make that judgment does not exist yet. Two
                    # real, already-computed pieces of information:
                    #   - how many DISTINCT resolved category values fed
                    #     this branch's OR-list (branch_name tells us
                    #     which of linkedin_values/naics_values to count —
                    #     both already computed above, never re-derived).
                    #     When this is 1, Explorium's own total_results
                    #     (see _run_one_branch's own comment — a real,
                    #     previously-uncaptured response field) is safely
                    #     attributable to that ONE category. When it's >1,
                    #     total_results reflects the OR-combination of
                    #     multiple categories together, and CANNOT be
                    #     attributed to any single one of them without
                    #     guessing — confirmed as the COMMON case in
                    #     Phase 14's own realistic scenarios (most
                    #     structured branches resolve 2-3 terms at once).
                    #   - the raw total_results number itself, for the
                    #     single-category case, so a future phase with a
                    #     principled threshold (not invented here) has
                    #     real data to work from.
                    resolved_values = linkedin_values if taxonomy_field == "linkedin_category" else naics_values
                    resolved_category_count = len(resolved_values)
                    raw_total_results = record.attributes.get("_branch_total_results")
                    scope = "single_category" if resolved_category_count == 1 else "multi_category"

                    # Phase 37: the SPECIFIC ICP terms that resolved into
                    # THIS branch (via term_branch_map, computed once by
                    # _plan_industry_branches — see that method's own
                    # docstring) — never the full `industries` tuple as
                    # before. This is a strict precision improvement: a
                    # record's tag now only ever claims the terms that
                    # genuinely, structurally resolved here, which is what
                    # makes cross-branch corroboration (below, and in
                    # hard_icp_validation.py) meaningful rather than
                    # trivially true for every record regardless of branch.
                    covered_terms = [term for term, branch in term_branch_map.items() if branch == branch_name]

                    new_attributes = {
                        **record.attributes,
                        "industry_match_branch": taxonomy_field,
                        "industry_match_terms": covered_terms,
                        "industry_match_resolved_category_count": resolved_category_count,
                        # Live-test audit finding (2026-09-03): the ACTUAL
                        # resolved taxonomy label string(s) this branch's
                        # filter matched on (e.g. ["Healthcare"] for a
                        # linkedin_category branch) — previously computed
                        # right above (resolved_values) but only its COUNT
                        # was ever persisted. OBSERVABILITY ONLY: an
                        # investigation into whether this could let
                        # app/services/hard_icp_validation.py verify a
                        # candidate's reported "industry" value against
                        # this branch's own resolved value concluded that
                        # is NOT possible with real Explorium data — a
                        # linkedin_category value is a LinkedIn-specific
                        # slug/label and a naics_category value is a
                        # numeric NAICS code, while the ONLY thing
                        # Explorium's /businesses response ever reports
                        # back per-company is a naics_description TEXT
                        # (see this module's own _BUSINESS_ATTRIBUTE_MAP
                        # comment) — none of these share an identifier
                        # space with any of the others, and this codebase
                        # has and wants no code-to-description lookup that
                        # would let it compare them (that would mean
                        # hardcoding taxonomy knowledge). This field is
                        # therefore never read by hard_icp_validation.py's
                        # own trust logic (see that module's
                        # _bridged_industry_terms docstring for the full
                        # story of why structured matches are no longer
                        # trusted at all) — kept purely as honest, real,
                        # human-auditable data: which taxonomy value(s)
                        # this branch's request actually matched on, for
                        # anyone inspecting why a candidate was found.
                        "industry_match_resolved_values": list(resolved_values),
                        "industry_match_scope": scope,
                        # Phase 13D: absent (key not added at all)
                        # when term_origin was never supplied —
                        # every pre-Phase-13D caller/test — so this
                        # tagging is a strict, additive extension,
                        # never a behavior change for them.
                        **({"industry_match_term_origins": _origins_for(covered_terms, term_origin)} if term_origin else {}),
                    }
                    # Only surface total_results when it is safely
                    # attributable (single_category) AND Explorium actually
                    # returned it — never a fabricated or misattributed
                    # number for the ambiguous multi_category case.
                    if scope == "single_category" and isinstance(raw_total_results, int):
                        new_attributes["industry_match_branch_total_results"] = raw_total_results

                    record = record.model_copy(update={"attributes": new_attributes})
                elif branch_name in _STRUCTURED_COMPANY_TYPE_TAXONOMY_FIELD:
                    # Phase 41: the company_type analogue of the industry
                    # structured-bridge tag directly above — deliberately
                    # a DISTINCT key set (company_type_match_* rather than
                    # industry_match_*) so no downstream consumer
                    # (hard_icp_validation.py's industry bridging/
                    # corroboration in particular) can ever mistake a
                    # company_type structured match for industry evidence.
                    # This module maps NO Explorium response field into a
                    # "company_type" NormalizedRecord attribute at all
                    # (see _BUSINESS_ATTRIBUTE_MAP — company_type is not a
                    # key in it, unlike naics_description -> industry), so
                    # unlike the industry branch above this tag exists
                    # PURELY as discovery provenance (which real,
                    # structural signal found this candidate), never as a
                    # value hard_icp_validation.py's `company_type` field
                    # could read as evidence — that hard rule's HOLD-only
                    # behavior for Explorium-discovered candidates (no
                    # company_type value is ever written) is completely
                    # unchanged by this phase; this only ever improves
                    # WHICH candidates discovery finds and how precisely.
                    company_type_taxonomy_field = _STRUCTURED_COMPANY_TYPE_TAXONOMY_FIELD[branch_name]
                    company_type_resolved_values = (
                        company_type_linkedin_values
                        if company_type_taxonomy_field == "linkedin_category"
                        else company_type_naics_values
                    )
                    company_type_covered_terms = [
                        term for term, branch in term_branch_map.items() if branch == branch_name
                    ]
                    record = record.model_copy(
                        update={
                            "attributes": {
                                **record.attributes,
                                "company_type_match_branch": company_type_taxonomy_field,
                                "company_type_match_terms": company_type_covered_terms,
                                "company_type_match_resolved_category_count": len(company_type_resolved_values),
                                **(
                                    {"company_type_match_term_origins": _origins_for(company_type_covered_terms, term_origin)}
                                    if term_origin
                                    else {}
                                ),
                            }
                        }
                    )
                elif branch_name == _KEYWORD_BRANCH:
                    # Phase 13B (extends the Phase 11 provenance pattern):
                    # this record came from the LOW-precision
                    # website_keywords substring-fallback tier, not a
                    # verified taxonomy match — tag it with the exact terms
                    # that were actually in this round's keyword OR-list
                    # (read back from the real filter this branch sent,
                    # branch_filters[_KEYWORD_BRANCH]["website_keywords"]
                    # ["values"] — never re-derived from a local variable
                    # that only exists on some of the three code paths that
                    # can populate this branch, see this method's own
                    # branch-construction section above) so a downstream
                    # consumer (app/services/evidence_import.py,
                    # app/services/qualification_context.py) can tell Phase
                    # 12's LLM verification prompt EXACTLY which term(s)
                    # this specific candidate was actually found by,
                    # instead of only "this was a keyword-fallback match."
                    # Absent (no key at all) for the keyword-less request
                    # shape (neither industries nor company_types stated) —
                    # there is no keyword term list to attach in that case.
                    keyword_filter = branch_filters[_KEYWORD_BRANCH].get("website_keywords")
                    if keyword_filter and keyword_filter.get("values"):
                        sent_terms = list(keyword_filter["values"])
                        # Phase 13D: company_type terms must not silently
                        # read as broad INDUSTRY keyword matches to a
                        # downstream consumer — this is a genuinely
                        # different claim (a match on "D2C" says something
                        # about what KIND of business this is; a match on
                        # an unmatched industry term like "Entertainment"
                        # says something about its SECTOR). keyword_terms
                        # (unmatched industries) and company_types are both
                        # already separate local variables at this point in
                        # execute() — reused here, never re-derived — so
                        # this is exactly which real Explorium request
                        # field each sent term originated from, not an
                        # invented judgment about term quality.
                        unmatched_industry_keys = {clean_text(t).casefold() for t in keyword_terms}
                        company_type_keys = {clean_text(t).casefold() for t in company_types}
                        sources = [
                            "company_type" if clean_text(t).casefold() in company_type_keys
                            and clean_text(t).casefold() not in unmatched_industry_keys
                            else "industry"
                            for t in sent_terms
                        ]
                        record = record.model_copy(
                            update={
                                "attributes": {
                                    **record.attributes,
                                    "keyword_match_terms": sent_terms,
                                    "keyword_match_term_sources": sources,
                                    **({"keyword_match_term_origins": _origins_for(sent_terms, term_origin)} if term_origin else {}),
                                }
                            }
                        )
                if "_branch_total_results" in record.attributes:
                    # Phase 15: internal-only signal set unconditionally by
                    # _run_one_branch (branch-agnostic by design) — never a
                    # public NormalizedRecord attribute. Stripped here,
                    # once, regardless of which branch (structured/naics/
                    # keyword) produced this record: the structured branch
                    # already consumed it above (into
                    # industry_match_branch_total_results, only when
                    # safely attributable); the keyword branch never had
                    # any legitimate use for it at all (a keyword OR-list's
                    # total_results is not attributable to any single term
                    # any more than a multi-category structured branch's
                    # is — arguably less so, since it can span both
                    # industry and company_type terms together).
                    record = record.model_copy(update={"attributes": {k: v for k, v in record.attributes.items() if k != "_branch_total_results"}})
                if record.external_id and record.external_id in merged_record_index_by_external_id:
                    # Phase 37: the SAME real company was already returned
                    # by an EARLIER branch this round — merge this LATER
                    # branch's provenance into the record already kept,
                    # rather than discarding it (see
                    # merged_record_index_by_external_id's own comment
                    # above for why this matters). The outer
                    # _already_seen_provider_external_ids in
                    # batch_orchestration.py still separately catches any
                    # repeat across rounds/pages — this is purely the
                    # within-this-one-call, cross-branch case.
                    existing_index = merged_record_index_by_external_id[record.external_id]
                    merged_records[existing_index] = _merge_cross_branch_attributes(merged_records[existing_index], record)
                    continue
                if record.external_id:
                    merged_record_index_by_external_id[record.external_id] = len(merged_records)
                merged_records.append(record)
            out_branch_state[branch_name] = _BRANCH_EXHAUSTED if branch_response.exhausted else (branch_response.cursor or _BRANCH_EXHAUSTED)

        # The whole round is exhausted only once EVERY branch this ICP's
        # industries could ever use (not just the ones active this round)
        # is confirmed exhausted — a branch skipped this round because it
        # was already exhausted still counts; a branch never yet queried
        # does not.
        all_exhausted = all(out_branch_state.get(name) == _BRANCH_EXHAUSTED for name in branch_filters)
        out_cursor = None if all_exhausted else json.dumps(out_branch_state)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(merged_records),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=False,
            ),
            cursor=out_cursor,
            exhausted=all_exhausted,
        )
