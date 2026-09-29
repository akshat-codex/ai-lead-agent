"""Phase 13C — keyword OR-list hygiene regression tests.

Prevents a large collection of unmatched terms from becoming one
unbounded, redundant website_keywords OR-list, via two SAFETY-BOUND
mechanisms only (never a "which terms are better" heuristic — the Phase
13C benchmark found false-positive risk driven by CATEGORY BREADTH, not
term COUNT, so this phase deliberately does not attempt to rank term
"specificity"):

  1. Case/whitespace-insensitive dedup (app/providers/explorium.py::
     _dedup_terms_preserve_order) — closes a real, confirmed gap: the
     pre-existing `if term not in list` check only caught an EXACT literal
     repeat, so "Consumer Goods" and "consumer goods" (e.g. one user-typed,
     one AI-proposed) both survived as separate, redundant OR-list entries.
  2. A conservative total-size cap (_MAX_KEYWORD_OR_TERMS = 12) — a safety
     ceiling only, applied in the SAME preserved order used everywhere
     else (user terms first, matching app/services/discovery_strategy.py::
     merge_strategy_into_hard_rules's own additive-only ordering), never a
     semantic "keep the good ones" judgment.

Both apply identically across all three website_keywords construction
paths in ExploriumCompanyDiscoveryProvider.execute(): industries+
company_types merged, company_types alone, and (implicitly, since it has
no keyword terms to dedup/cap at all) the keyword-less request shape.

Follows test_explorium_provider.py's exact respx-mocking convention.
"""
import json

import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import (
    ExploriumCompanyDiscoveryProvider,
    _MAX_KEYWORD_OR_TERMS,
    _dedup_terms_preserve_order,
)

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"


def _provider(api_key: str = "test-key-12345") -> ExploriumCompanyDiscoveryProvider:
    return ExploriumCompanyDiscoveryProvider(api_key=api_key)


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query=query)


def _business_calls():
    return [c for c in respx.calls if str(c.request.url).startswith(EXPLORIUM_SEARCH_URL)]


def _mock_no_match_everywhere():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))


# ============================================================================
# Unit tests for the dedup helper itself
# ============================================================================


def test_dedup_helper_removes_case_insensitive_duplicates():
    result = _dedup_terms_preserve_order(["Consumer Goods", "consumer goods", "CONSUMER GOODS"])
    assert result == ["Consumer Goods"]


def test_dedup_helper_removes_whitespace_variant_duplicates():
    result = _dedup_terms_preserve_order(["D2C", " D2C ", "D2C  "])
    assert result == ["D2C"]


def test_dedup_helper_preserves_first_occurrence_casing_and_order():
    result = _dedup_terms_preserve_order(["Healthcare", "SaaS", "healthcare", "D2C"])
    assert result == ["Healthcare", "SaaS", "D2C"]


def test_dedup_helper_drops_blank_terms():
    result = _dedup_terms_preserve_order(["Healthcare", "", "   ", "D2C"])
    assert result == ["Healthcare", "D2C"]


def test_dedup_helper_is_a_pure_no_op_on_already_distinct_terms():
    result = _dedup_terms_preserve_order(["Healthcare", "SaaS", "D2C"])
    assert result == ["Healthcare", "SaaS", "D2C"]


# ============================================================================
# End-to-end: user terms preserved
# ============================================================================


@respx.mock
def test_user_typed_terms_still_reach_the_keyword_or_list_unchanged():
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C skincare", "Healthcare"]))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["D2C skincare", "Healthcare"]


# ============================================================================
# Useful AI terms preserved (distinct terms are never dropped by the dedup)
# ============================================================================


@respx.mock
def test_distinct_ai_expanded_terms_are_all_preserved():
    """Simulates Phase 10's merge: a user term plus several genuinely
    distinct AI-proposed terms, none of which structurally resolve — every
    one must still reach the OR-list, since dedup only removes REDUNDANT
    entries, never distinct ones."""
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C skincare", "Consumer Goods", "E-commerce", "Retail"]))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["D2C skincare", "Consumer Goods", "E-commerce", "Retail"]


# ============================================================================
# Duplicate terms removed (the actual fix)
# ============================================================================


@respx.mock
def test_case_variant_duplicate_between_user_and_ai_term_is_removed():
    """The realistic Phase 10 scenario this fix targets: a user typed
    'Consumer Goods' directly AND the AI also proposed 'consumer goods' (a
    close synonym in different casing) — merge_strategy_into_hard_rules's
    own case-insensitive dedup (app/services/discovery_strategy.py::
    _merge_terms) already prevents this exact pair from co-existing in
    icp.hard_rules.industries, so this test targets the case Explorium's
    OWN construction can still introduce: a term appearing in BOTH
    industries and company_types with different casing, which
    merge_strategy_into_hard_rules never sees together since they're
    different ICP fields."""
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["consumer goods"], company_types=["Consumer Goods"]))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["consumer goods"]  # first occurrence's casing kept, never repeated


@respx.mock
def test_whitespace_variant_duplicate_is_removed():
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["D2C"], company_types=[" D2C "]))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["D2C"]


@respx.mock
def test_exact_duplicate_still_removed_regression_guard():
    """The pre-existing exact-match dedup contract
    (test_explorium_provider.py::test_duplicate_term_between_industries_and_company_types_not_repeated)
    must still hold — the new case-insensitive dedup is a strict superset,
    never a narrowing."""
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=["Startup"], company_types=["Startup"]))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["Startup"]


@respx.mock
def test_company_types_only_path_also_dedups():
    """The elif company_types: (no industries stated) path had no dedup
    at all before this phase — regression guard that it now does."""
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(company_types=["Startup", "startup", " Startup "]))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["Startup"]


# ============================================================================
# Overly broad keyword-term VOLUME handled safely (the cap)
# ============================================================================


@respx.mock
def test_keyword_or_list_is_capped_at_the_safety_ceiling():
    many_terms = [f"Industry Term {i}" for i in range(20)]
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=tuple(many_terms)))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert len(filters["website_keywords"]["values"]) == _MAX_KEYWORD_OR_TERMS


@respx.mock
def test_cap_keeps_terms_in_original_user_first_order_never_reordered():
    """The cap truncates, it never re-ranks — the first N terms in the
    ICP's own order survive, exactly matching
    merge_strategy_into_hard_rules's "user terms first" ordering, never a
    new judgment about which terms are "better"."""
    many_terms = [f"Term {i:02d}" for i in range(20)]
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=tuple(many_terms)))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == many_terms[:_MAX_KEYWORD_OR_TERMS]


@respx.mock
def test_below_cap_term_count_is_never_truncated():
    """A realistic Phase 10-bounded scenario (well under the cap) must
    reach Explorium with every term intact — the cap must never bite
    ordinary usage, only pathological volume."""
    terms = ["D2C skincare", "Consumer Goods", "E-commerce", "Cosmetics"]  # 4, well under 12
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=tuple(terms)))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == terms


@respx.mock
def test_cap_applies_after_dedup_not_before():
    """Deduping first means a pathological list of near-duplicates doesn't
    waste cap budget on redundant entries — 20 case-variant duplicates of
    ONE real term should collapse to 1 entry, not be capped down to 12
    near-identical copies of it."""
    duplicated = ["Consumer Goods" if i % 2 == 0 else "consumer goods" for i in range(20)]
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=tuple(duplicated)))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["Consumer Goods"]


# ============================================================================
# Keyword provenance remains correct (Phase 13B integration)
# ============================================================================


@respx.mock
def test_keyword_match_terms_provenance_reflects_the_deduped_capped_list():
    """The Phase 13B provenance tag must reflect what was ACTUALLY sent to
    Explorium (post-dedup, post-cap) — an honest record of the real
    search, not the raw pre-cleanup input."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "kw001", "name": "Some Co", "naics_description": "X"}]}
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["D2C", "d2c", " D2C "]))  # 3 case/whitespace variants of one term

    record = response.data[0]
    assert record.attributes["keyword_match_terms"] == ["D2C"]  # deduped, matches what was actually sent


# ============================================================================
# Structured LinkedIn/NAICS discovery completely unchanged
# ============================================================================


@respx.mock
def test_structured_branch_unaffected_by_keyword_dedup_or_cap():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"business_id": "st001", "name": "Health Co", "naics_description": "General Medical"}]}
        )
    )
    provider = _provider()
    response = provider.run(_request(industries=["Healthcare"]))

    calls = _business_calls()
    assert len(calls) == 1  # structured only, no keyword call at all
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["linkedin_category"] == {"values": ["healthcare"]}
    assert "website_keywords" not in filters
    record = response.data[0]
    assert record.attributes["industry_match_branch"] == "linkedin_category"
    assert "keyword_match_terms" not in record.attributes


@respx.mock
def test_naics_branch_unaffected_by_keyword_dedup_or_cap():
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "naics_category", "query": "Widgetology"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Widgetology", "label": "Widgetology", "value": "999999"}])
    )
    respx.post(EXPLORIUM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"business_id": "na001", "name": "Widget Co", "naics_description": "Widgetology"}]})
    )
    provider = _provider()
    response = provider.run(_request(industries=["Widgetology"]))

    calls = _business_calls()
    assert len(calls) == 1
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["naics_category"] == {"values": ["999999"]}
    assert "website_keywords" not in filters
    record = response.data[0]
    assert record.attributes["industry_match_branch"] == "naics_category"


@respx.mock
def test_mixed_structured_and_keyword_branches_each_only_get_their_own_terms():
    """Healthcare (structured) + a large batch of unmatched terms (keyword,
    subject to dedup/cap) together — the structured branch's filter must
    contain ONLY the resolved value, completely unaffected by however many
    terms fell to keyword."""
    many_unmatched = [f"Obscure Term {i}" for i in range(15)]
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": "linkedin_category", "query": "Healthcare"}).mock(
        return_value=httpx.Response(200, json=[{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}])
    )
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=tuple(["Healthcare", *many_unmatched])))

    calls = _business_calls()
    assert len(calls) == 2
    all_filters = [json.loads(c.request.content)["filters"] for c in calls]
    structured = next(f for f in all_filters if "linkedin_category" in f)
    keyword = next(f for f in all_filters if "website_keywords" in f)
    assert structured["linkedin_category"] == {"values": ["healthcare"]}
    assert "website_keywords" not in structured
    assert len(keyword["website_keywords"]["values"]) == _MAX_KEYWORD_OR_TERMS  # capped, structured branch untouched


# ============================================================================
# No extra OpenAI calls / no extra discovery provider (structural guards)
# ============================================================================


def test_no_new_llm_or_provider_imports_introduced():
    """This phase touches ONLY app/providers/explorium.py's own keyword
    OR-list construction — confirms no accidental import of an LLM
    provider or a second discovery provider was introduced by this
    change."""
    import app.providers.explorium as explorium_module

    source_path = explorium_module.__file__
    with open(source_path, encoding="utf-8") as f:
        source = f.read()
    assert "llm_providers" not in source
    assert "OpenAIProvider" not in source
    assert "import exa" not in source.lower()
    assert "perplexity" not in source.lower()


# ============================================================================
# Existing failure/degradation behavior preserved
# ============================================================================


@respx.mock
def test_autocomplete_failure_still_degrades_to_deduped_capped_keyword_fallback():
    """Phase 13A's cache-failure handling + this phase's dedup/cap must
    compose correctly: a failed autocomplete lookup still falls through to
    keyword, and that keyword list is still deduped/capped exactly as any
    other keyword list would be."""
    respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    response = provider.run(_request(industries=["Healthcare", "healthcare"]))  # case-variant duplicate

    assert response.success is True
    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == ["Healthcare"]  # deduped even under failure-degradation


# ============================================================================
# PHASE 40 — intelligent, deterministic truncation: user terms preserved
# first (unchanged), but an AI-declared compound-intersection phrase
# (CompanyDiscoveryQuery.combination_terms — see app/services/
# hard_icp_validation.py's Phase 39 fix for why these terms matter most)
# is no longer silently pushed out by pure list-position truncation ahead
# of a lower-value AI synonym.
# ============================================================================


@respx.mock
def test_long_user_term_list_still_truncates_in_original_order_when_no_combination_terms():
    """Zero behavior change for the common case: no combination_terms
    supplied at all (every pre-Phase-40 caller) — truncation stays exactly
    list-order, byte-for-byte identical to before this phase."""
    many_terms = [f"User Term {i:02d}" for i in range(20)]
    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request(industries=tuple(many_terms)))

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == many_terms[:_MAX_KEYWORD_OR_TERMS]


@respx.mock
def test_ai_combination_term_survives_truncation_pushed_out_by_list_position():
    """The confirmed real-world gap this phase fixes: a long user-typed
    term list (already at the cap on its own) plus an AI-proposed
    combination phrase appended LAST (lowest list position, per
    merge_strategy_into_hard_rules's own additive "user terms first"
    ordering) — before this phase the combination term would be silently
    truncated away purely for coming last; it must now survive, since
    tier 1 (12 user terms) already fills the cap and tier 2 (the
    combination term) has zero room — so this test uses 11 user terms,
    leaving exactly one slot, which the combination term must win over
    the trailing plain AI term that would otherwise occupy it by list
    position."""
    user_terms = [f"User Term {i:02d}" for i in range(11)]  # 11 user terms — one slot left under the cap of 12
    ai_terms = ["Plain user-ranked-but-untagged filler"]  # never reaches term_origin explicitly -> tier 1 (safe default)
    combination_term = "Clinical Software"
    all_terms = tuple([*user_terms, combination_term])  # combination term is LAST by list position
    term_origin = {t.lower(): "user" for t in user_terms} | {combination_term.lower(): "ai"}

    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(
        _request(
            industries=all_terms,
            term_origin=term_origin,
            combination_terms=(combination_term,),
        )
    )

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    values = filters["website_keywords"]["values"]
    assert len(values) == _MAX_KEYWORD_OR_TERMS
    assert combination_term in values  # survives despite being last by list position
    assert set(user_terms) <= set(values)  # all 11 user terms still present, untouched


@respx.mock
def test_combination_term_never_bumps_a_user_term():
    """User terms are STILL always tier 1 — a combination term is
    prioritized only over OTHER AI terms, never over a genuine user-typed
    term. 13 user terms (one over the cap) + 1 combination term: the
    combination term must still be dropped, since every one of the 12
    surviving slots is rightfully a user term."""
    user_terms = [f"User Term {i:02d}" for i in range(13)]  # already 1 over the cap on user terms alone
    combination_term = "Clinical Software"
    term_origin = {t.lower(): "user" for t in user_terms} | {combination_term.lower(): "ai"}

    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(
        _request(
            industries=tuple([*user_terms, combination_term]),
            term_origin=term_origin,
            combination_terms=(combination_term,),
        )
    )

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    values = filters["website_keywords"]["values"]
    assert len(values) == _MAX_KEYWORD_OR_TERMS
    assert combination_term not in values  # never displaces a genuine user term
    assert values == user_terms[:_MAX_KEYWORD_OR_TERMS]


@respx.mock
def test_combination_term_beats_plain_ai_term_when_both_compete_for_the_last_slot():
    """Direct tier-2-vs-tier-3 comparison: a plain AI synonym appears
    BEFORE the combination term in list order, but the combination term
    must still win the last available slot — proving the reordering is
    priority-driven, not merely "whichever AI term happens to be first"."""
    user_terms = [f"User Term {i:02d}" for i in range(11)]
    plain_ai_term = "AAA Plain Synonym"  # sorts/lists first among the two AI terms
    combination_term = "Clinical Software"
    all_terms = tuple([*user_terms, plain_ai_term, combination_term])
    term_origin = {t.lower(): "user" for t in user_terms} | {
        plain_ai_term.lower(): "ai",
        combination_term.lower(): "ai",
    }

    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(
        _request(
            industries=all_terms,
            term_origin=term_origin,
            combination_terms=(combination_term,),
        )
    )

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    values = filters["website_keywords"]["values"]
    assert len(values) == _MAX_KEYWORD_OR_TERMS
    assert combination_term in values
    assert plain_ai_term not in values


@respx.mock
def test_survivors_are_sent_in_original_relative_order_not_priority_order():
    """The REQUEST SHAPE must never change — only which terms survive.
    Even though the combination term is promoted for SELECTION purposes,
    the final website_keywords list sent to Explorium keeps every
    survivor in its ORIGINAL relative order (user terms first
    positionally, exactly as before this phase), never reshuffled to put
    the combination term first."""
    user_terms = [f"User Term {i:02d}" for i in range(11)]
    combination_term = "Clinical Software"
    all_terms = tuple([*user_terms, combination_term])
    term_origin = {t.lower(): "user" for t in user_terms} | {combination_term.lower(): "ai"}

    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(
        _request(
            industries=all_terms,
            term_origin=term_origin,
            combination_terms=(combination_term,),
        )
    )

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    values = filters["website_keywords"]["values"]
    assert values == [*user_terms, combination_term]  # original relative order preserved


@respx.mock
def test_below_cap_combination_terms_never_truncated_at_all():
    """When the term list already fits under the cap, combination_terms
    changes nothing — every term reaches Explorium, exactly as before this
    phase."""
    terms = ["Healthcare", "SaaS", "Clinical Software"]
    term_origin = {"healthcare": "user", "saas": "user", "clinical software": "ai"}

    _mock_no_match_everywhere()
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(
        _request(industries=tuple(terms), term_origin=term_origin, combination_terms=("Clinical Software",))
    )

    calls = _business_calls()
    filters = json.loads(calls[0].request.content)["filters"]
    assert filters["website_keywords"]["values"] == terms


@respx.mock
def test_keyword_less_request_shape_still_produces_no_website_keywords_key():
    """Regression guard: neither industries nor company_types stated — the
    plain, keyword-less request shape must remain completely untouched by
    this phase (nothing to dedup/cap, no key at all)."""
    respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    provider = _provider()
    provider.run(_request())

    calls = _business_calls()
    body = json.loads(calls[0].request.content)
    # An empty filters dict is falsy, so _run_one_branch never sets the
    # "filters" key at all for this request shape — confirming there is
    # truly nothing to dedup/cap here, not just an empty website_keywords.
    filters = body.get("filters", {})
    assert "website_keywords" not in filters
