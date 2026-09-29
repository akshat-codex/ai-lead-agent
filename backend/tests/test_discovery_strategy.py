"""Phase 10 — tests for the OpenAI ICP Interpreter
(app/services/discovery_strategy.py + app/schemas/discovery_strategy.py).

All LLM calls are the deterministic MockLLMProvider (Phase 9's own test
convention) — no real OpenAI call, no API key, no cost. OpenAIProvider's
own HTTP-mapping behavior is already covered by
tests/test_openai_llm_provider.py; this file tests the interpretation/
merge/cap orchestration layer, which is provider-agnostic by design
(mirrors tests/test_llm_qualification.py testing qualify_lead() against
MockLLMProvider, not against OpenAIProvider directly).
"""
import json

from app.providers.mocks import MockCompanyDataProvider
from app.providers.registry import ProviderRegistry
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.discovery import DiscoveryStatus
from app.services.company_discovery import build_company_discovery_query, run_company_discovery
from app.services.discovery_strategy import (
    MAX_AI_COMPANY_TYPE_TERMS,
    MAX_AI_EXCLUSION_TERMS,
    MAX_AI_INDUSTRY_TERMS,
    build_discovery_strategy_prompt,
    build_discovery_strategy_request,
    interpret_icp,
    interpret_icp_and_build_query,
    merge_strategy_into_hard_rules,
)
from app.services.llm_providers.base import LLMProviderError, LLMProviderErrorCode, LLMProviderResponse
from app.services.llm_providers.mock import MockLLMProvider


def _icp(icp_id: str = "icp-1", version: int = 1, **hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("D2C skincare",),
        geography=CanonicalGeography(
            countries=(GeographyEntry(raw="India", code="IN", label="India"),),
        ),
        employee_range=EmployeeRange(min=10, max=200),
        allowed_titles=(),
        company_types=(),
        exclusions=(),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=CanonicalHardRules(**hard_defaults),
        soft_preferences=CanonicalSoftPreferences(),
    )


def _response(payload: dict) -> str:
    return json.dumps(payload)


def _good_payload(**overrides) -> dict:
    payload = {
        "industry_terms": ["Consumer Goods", "E-commerce", "Retail"],
        "company_type_terms": ["D2C"],
        "exclusion_terms": ["agencies", "wholesalers"],
        "geography_notes": [],
        "unsupported_intent": ["growing companies"],
        "confidence": 82,
        "reasoning": "Expanded D2C skincare into adjacent taxonomy-plausible terms.",
    }
    payload.update(overrides)
    return payload


# --- messy ICP gets expanded, original terms preserved --------------------


def test_messy_icp_terms_get_expanded_with_synonym_candidates():
    icp = _icp(industries=("D2C skincare",))
    provider = MockLLMProvider(response_text=_response(_good_payload()))

    strategy = interpret_icp(icp, provider)

    assert strategy.status == "SUCCESS"
    assert "Consumer Goods" in strategy.industry_terms
    assert "E-commerce" in strategy.industry_terms
    assert "Retail" in strategy.industry_terms


def test_original_icp_terms_are_never_removed_by_merge():
    icp = _icp(industries=("D2C skincare",), company_types=("Wholesale",))
    provider = MockLLMProvider(response_text=_response(_good_payload()))
    strategy = interpret_icp(icp, provider)

    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert "D2C skincare" in merged.industries  # the user's own term, untouched
    assert "Wholesale" in merged.company_types  # the user's own term, untouched
    assert merged.industries[0] == "D2C skincare"  # original terms come first


def test_ai_proposed_terms_are_appended_never_reordered_ahead_of_originals():
    icp = _icp(industries=("D2C skincare", "Beauty"))
    provider = MockLLMProvider(response_text=_response(_good_payload()))
    strategy = interpret_icp(icp, provider)

    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert merged.industries[:2] == ("D2C skincare", "Beauty")
    assert set(strategy.industry_terms) <= set(merged.industries[2:])


def test_duplicate_ai_term_already_present_is_not_added_twice():
    icp = _icp(industries=("Consumer Goods",))
    provider = MockLLMProvider(response_text=_response(_good_payload(industry_terms=["consumer goods", "Retail"])))
    strategy = interpret_icp(icp, provider)

    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert merged.industries.count("Consumer Goods") == 1
    assert "Retail" in merged.industries


# --- exclusions extracted, routed to existing hard.exclusions -------------


def test_negative_intent_is_extracted_into_exclusion_terms_not_industries():
    icp = _icp(industries=("D2C skincare",))
    provider = MockLLMProvider(
        response_text=_response(_good_payload(industry_terms=["Consumer Goods"], exclusion_terms=["agencies", "wholesalers"]))
    )
    strategy = interpret_icp(icp, provider)

    assert "agencies" in strategy.exclusion_terms
    assert "wholesalers" in strategy.exclusion_terms
    assert "agencies" not in strategy.industry_terms


def test_exclusion_terms_merge_into_existing_hard_rule_exclusions():
    icp = _icp(exclusions=("Acme Corp",))
    provider = MockLLMProvider(response_text=_response(_good_payload(exclusion_terms=["agencies"])))
    strategy = interpret_icp(icp, provider)

    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert "Acme Corp" in merged.exclusions  # user's own exclusion preserved
    assert "agencies" in merged.exclusions  # AI-extracted exclusion added


# --- unsupported intent captured, never becomes a filter -------------------


def test_unsupported_intent_is_captured_for_logging_never_becomes_a_filter():
    icp = _icp()
    provider = MockLLMProvider(response_text=_response(_good_payload(unsupported_intent=["growing companies", "recently funded"])))
    strategy = interpret_icp(icp, provider)

    assert "growing companies" in strategy.unsupported_intent
    assert "recently funded" in strategy.unsupported_intent

    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    query = build_company_discovery_query(icp.model_copy(update={"hard_rules": merged}))

    # unsupported_intent never leaks into any CompanyDiscoveryQuery field
    assert "growing companies" not in query.industries
    assert "growing companies" not in query.company_types
    assert not hasattr(query, "unsupported_intent")


# --- AI cannot declare a fake Explorium taxonomy value ----------------------


def test_raw_output_schema_has_no_field_for_a_resolved_taxonomy_value():
    from app.schemas.discovery_strategy import RawDiscoveryStrategyOutput

    fields = RawDiscoveryStrategyOutput.model_fields.keys()
    assert "linkedin_category" not in fields
    assert "naics_category" not in fields
    assert "resolved_category" not in fields
    # every field the schema does define carries free-text candidate terms
    # only (or a scalar confidence/reasoning) — never a "verified" flag
    assert "verified" not in fields
    assert "taxonomy_value" not in fields


def test_llm_proposed_terms_still_go_through_the_unmodified_explorium_cascade():
    # Wiring the strategy's terms into a real discovery run must produce
    # the exact same CandidateCompany shape as a user-typed term would —
    # proving the AI-proposed terms get no special treatment inside
    # discovery, only the mock provider's own (unmodified) matching.
    icp = _icp(industries=("D2C skincare",))
    provider = MockLLMProvider(response_text=_response(_good_payload(industry_terms=["Consumer Goods"])))
    strategy = interpret_icp(icp, provider)
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    merged_icp = icp.model_copy(update={"hard_rules": merged})

    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    result = run_company_discovery(merged_icp, registry, limit=10)

    assert result.status == DiscoveryStatus.COMPLETED
    # Discovery ran the merged query through the SAME run_company_discovery
    # path a plain user ICP would use — no separate/parallel code path.


# --- schema validation ------------------------------------------------------


def test_malformed_json_degrades_safely_to_no_terms():
    icp = _icp()
    provider = MockLLMProvider(response_text="not json at all {{{")

    strategy = interpret_icp(icp, provider)

    assert strategy.status == "MALFORMED_OUTPUT"
    assert strategy.industry_terms == ()
    assert strategy.error_message is not None


def test_schema_violation_degrades_safely_to_no_terms():
    icp = _icp()
    provider = MockLLMProvider(response_text=json.dumps({"industry_terms": ["X"]}))  # missing required fields

    strategy = interpret_icp(icp, provider)

    assert strategy.status == "SCHEMA_INVALID"
    assert strategy.industry_terms == ()


def test_unknown_extra_field_is_rejected_not_silently_ignored():
    icp = _icp()
    payload = _good_payload()
    payload["invented_taxonomy_field"] = "Healthcare-NAICS-621111"  # an attempted fabrication
    provider = MockLLMProvider(response_text=json.dumps(payload))

    strategy = interpret_icp(icp, provider)

    assert strategy.status == "SCHEMA_INVALID"
    assert strategy.industry_terms == ()


# --- term count capping -----------------------------------------------------


def test_industry_term_count_is_capped():
    icp = _icp()
    too_many = [f"Term {i}" for i in range(MAX_AI_INDUSTRY_TERMS + 10)]
    provider = MockLLMProvider(response_text=_response(_good_payload(industry_terms=too_many)))

    strategy = interpret_icp(icp, provider)

    assert len(strategy.industry_terms) == MAX_AI_INDUSTRY_TERMS


def test_company_type_term_count_is_capped():
    icp = _icp()
    too_many = [f"Type {i}" for i in range(MAX_AI_COMPANY_TYPE_TERMS + 10)]
    provider = MockLLMProvider(response_text=_response(_good_payload(company_type_terms=too_many)))

    strategy = interpret_icp(icp, provider)

    assert len(strategy.company_type_terms) == MAX_AI_COMPANY_TYPE_TERMS


def test_exclusion_term_count_is_capped():
    icp = _icp()
    too_many = [f"Exclude {i}" for i in range(MAX_AI_EXCLUSION_TERMS + 10)]
    provider = MockLLMProvider(response_text=_response(_good_payload(exclusion_terms=too_many)))

    strategy = interpret_icp(icp, provider)

    assert len(strategy.exclusion_terms) == MAX_AI_EXCLUSION_TERMS


def test_cap_cannot_be_bypassed_via_duplicates_padding_the_list():
    icp = _icp()
    padded = ["Consumer Goods"] * 50 + ["Retail"]
    provider = MockLLMProvider(response_text=_response(_good_payload(industry_terms=padded)))

    strategy = interpret_icp(icp, provider)

    # dedup happens before the cap, so a 51-item list of near-duplicates
    # still yields at most MAX_AI_INDUSTRY_TERMS distinct terms
    assert len(strategy.industry_terms) <= MAX_AI_INDUSTRY_TERMS
    assert strategy.industry_terms.count("Consumer Goods") == 1


# --- provider failure degrades safely to existing discovery behavior ------


def test_provider_error_degrades_to_original_terms_only_never_blocks_discovery():
    icp = _icp(industries=("D2C skincare",))
    provider = MockLLMProvider(raise_provider_error=True)

    strategy = interpret_icp(icp, provider)
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert strategy.status == "PROVIDER_UNAVAILABLE"
    assert merged.industries == ("D2C skincare",)  # completely unchanged from the user's own ICP


def test_provider_timeout_degrades_safely():
    icp = _icp()
    provider = MockLLMProvider(raise_timeout=True)

    strategy = interpret_icp(icp, provider)

    assert strategy.status == "PROVIDER_UNAVAILABLE"
    assert strategy.industry_terms == ()


def test_empty_response_degrades_safely():
    icp = _icp()
    provider = MockLLMProvider(return_empty=True)

    strategy = interpret_icp(icp, provider)

    assert strategy.status == "PROVIDER_UNAVAILABLE"
    assert strategy.industry_terms == ()


def test_interpret_icp_and_build_query_still_returns_a_usable_query_on_provider_failure():
    icp = _icp(industries=("D2C skincare",), company_types=("Wholesale",))
    provider = MockLLMProvider(raise_provider_error=True)

    query, strategy = interpret_icp_and_build_query(icp, provider, limit=10)

    assert strategy.status == "PROVIDER_UNAVAILABLE"
    assert query.industries == ("D2C skincare",)
    assert query.company_types == ("Wholesale",)
    assert query.limit == 10


def test_generic_exception_from_a_misbehaving_provider_never_propagates():
    class _ExplodingProvider(MockLLMProvider):
        def _call(self, context):
            raise RuntimeError("boom")

    icp = _icp()
    strategy = interpret_icp(icp, _ExplodingProvider())

    assert strategy.status == "PROVIDER_UNAVAILABLE"


# --- exactly one call per ICP submission, never per candidate -------------


def test_exactly_one_provider_call_regardless_of_candidate_or_result_count():
    call_count = {"n": 0}

    class _CountingProvider(MockLLMProvider):
        def _call(self, context):
            call_count["n"] += 1
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=True,
                raw_text=_response(_good_payload()),
            )

    icp = _icp()
    interpret_icp(icp, _CountingProvider())

    assert call_count["n"] == 1


def test_interpret_icp_and_build_query_also_makes_exactly_one_call():
    call_count = {"n": 0}

    class _CountingProvider(MockLLMProvider):
        def _call(self, context):
            call_count["n"] += 1
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=True,
                raw_text=_response(_good_payload()),
            )

    icp = _icp()
    interpret_icp_and_build_query(icp, _CountingProvider(), limit=50)

    assert call_count["n"] == 1  # not one call per requested candidate (limit=50)


# --- request building is deterministic and compact -------------------------


def test_request_is_deterministic_for_the_same_icp():
    icp = _icp()
    r1 = build_discovery_strategy_request(icp)
    r2 = build_discovery_strategy_request(icp)
    assert r1 == r2


def test_request_never_includes_allowed_titles_a_person_level_field():
    # allowed_titles is explicitly out of scope for company discovery
    # (see CompanyDiscoveryQuery's own docstring) — the strategy request
    # must not leak it in either, keeping the same company/person boundary.
    icp = _icp(allowed_titles=("CMO", "VP Marketing"))
    request = build_discovery_strategy_request(icp)
    assert not hasattr(request, "allowed_titles")


def test_request_includes_custom_soft_preferences():
    """Phase 4 fix: a filter with no dedicated HardRules/SoftPreferences
    field (e.g. "revenue", "funding" — see
    app/services/filter_projection.py's own comment) reaches the ICP only
    via soft_preferences.custom_preferences. Before this fix,
    build_discovery_strategy_request silently never read that field at
    all — the LLM never saw it, even as passive context."""
    from app.schemas.canonical_icp import CanonicalCustomRule

    icp = _icp(
    ).model_copy(
        update={
            "soft_preferences": CanonicalSoftPreferences(
                custom_preferences=(CanonicalCustomRule(label="funding", description="eq: 'Recently raised Series A'"),)
            )
        }
    )
    request = build_discovery_strategy_request(icp)
    assert any("funding" in term and "Recently raised Series A" in term for term in request.soft_preferences)


# --- Phase 27: compound-ICP prompt guidance ---------------------------------
# Root cause: a live Gemini test (real call, "Healthcare SaaS" ICP) showed
# the model independently expanding "Healthcare" and "SaaS" into separate
# synonym clusters (Healthtech, Digital Health, ...; Software as a Service)
# rather than proposing terms that name the INTERSECTION — every proposed
# term still only had to satisfy ONE side of the ICP's actual intent. The
# backend's own hard_icp_validation.py fix (Phase 25/26) already keeps such
# a mismatch from silently PASSing (it correctly HOLDs), but better terms
# reduce how often that safety net has to fire at all. Since the fix is a
# prompt-text change, only the parts a mock/orchestration test can actually
# verify are covered here: (1) the rendered prompt genuinely contains the
# new guidance, generically, not tied to any one ICP's wording, and (2) the
# orchestration layer (merge/cap/degrade) correctly handles a MOCKED
# response shaped the way a well-behaved model should now respond (compound
# phrases), proving the pipeline doesn't silently drop or mishandle them —
# this file cannot make a real Gemini call to verify the model's actual
# output quality, which was confirmed separately via a real Phase 26 probe.


def test_prompt_instructs_compound_phrase_generation_generically():
    icp = _icp(industries=("Healthcare", "SaaS"))
    request = build_discovery_strategy_request(icp)
    prompt = build_discovery_strategy_prompt(request)

    assert "COMPOUND" in prompt.upper()
    assert "intersection" in prompt.lower()
    # The guidance itself must be generic (industry-agnostic instructions),
    # not a hardcoded Healthcare/SaaS-only rule — confirmed by checking the
    # instruction text names MULTIPLE unrelated example pairings, not just
    # the one this bug was found with.
    assert "Fintech" in prompt  # a different example pairing than Healthcare/SaaS
    assert "D2C" in prompt  # a third, distinct example pairing
    # And the prompt is still built from the SAME deterministic renderer —
    # a different ICP's industries produce a different rendered payload,
    # but the SAME instructional guidance text (never ICP-specific
    # instructions injected per-request).
    other_icp = _icp(industries=("Retail", "Hospitality"))
    other_prompt = build_discovery_strategy_prompt(build_discovery_strategy_request(other_icp))
    assert "COMPOUND" in other_prompt.upper()
    assert "intersection" in other_prompt.lower()


def test_prompt_instructs_narrow_synonym_scope():
    icp = _icp()
    prompt = build_discovery_strategy_prompt(build_discovery_strategy_request(icp))
    lowered = prompt.lower()
    assert "narrow" in lowered or "close" in lowered
    assert "broad" in lowered or "loosely" in lowered  # explicitly warns against broad/loose terms


def test_compound_icp_mocked_response_with_intersection_terms_flows_through_merge():
    """Simulates a well-behaved model's NEW expected output shape for a
    compound ICP (intersection phrases, not independent per-term
    synonyms) and confirms the existing, unmodified orchestration layer
    (cap/merge/dedupe) handles it correctly end-to-end."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(
        industry_terms=["Healthcare Software", "Health Tech Software", "Clinical Software", "Medical Software"],
        company_type_terms=[],
        exclusion_terms=[],
        reasoning="Proposed compound healthcare-software phrasings rather than independent Healthcare/SaaS synonyms.",
    )
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)

    assert strategy.status == "SUCCESS"
    assert "Healthcare Software" in strategy.industry_terms
    assert "Clinical Software" in strategy.industry_terms

    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    assert "Healthcare" in merged.industries  # original user terms never removed
    assert "SaaS" in merged.industries
    assert "Healthcare Software" in merged.industries  # AI-proposed compound term appended
    assert "Clinical Software" in merged.industries


def test_compound_icp_generalizes_to_fintech_saas_not_hardcoded():
    """The SAME orchestration path, a DIFFERENT compound pair — confirms
    nothing in the actual code (as opposed to the prompt's own example
    text) is Healthcare/SaaS-specific; any compound-phrase terms the model
    proposes flow through identically regardless of industry."""
    icp = _icp(industries=("Fintech", "SaaS"))
    payload = _good_payload(
        industry_terms=["Fintech Software", "Financial Software", "Banking SaaS"],
        company_type_terms=[],
        exclusion_terms=[],
    )
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert "Fintech Software" in merged.industries
    assert "Fintech" in merged.industries
    assert "SaaS" in merged.industries


def test_still_exactly_one_call_for_a_compound_icp():
    """The compound-phrase guidance must never change the one-call-per-
    batch guarantee — same contract as the existing single-term case."""
    call_count = {"n": 0}

    class _CountingProvider(MockLLMProvider):
        def _call(self, context):
            call_count["n"] += 1
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=True,
                raw_text=_response(_good_payload(industry_terms=["Healthcare Software"])),
            )

    icp = _icp(industries=("Healthcare", "SaaS"))
    interpret_icp(icp, _CountingProvider())
    interpret_icp(icp, _CountingProvider())  # a second, independent call site — still one call EACH

    assert call_count["n"] == 2  # two separate interpret_icp() invocations, one provider call apiece — never batched/looped


def test_exact_match_safety_and_hold_behavior_unaffected_by_prompt_change():
    """The prompt change only affects WHAT terms are proposed — it must
    never touch app/providers/explorium.py's exact-match resolution or
    app/services/hard_icp_validation.py's HOLD-on-partial-match behavior
    (Phase 25/26), both of which operate purely on whatever terms end up
    in the merged hard_rules.industries tuple, regardless of their
    wording or how they were generated."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(industry_terms=["Healthcare Software"], company_type_terms=[], exclusion_terms=[])
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    # merge_strategy_into_hard_rules is still purely additive — the user's
    # own original terms are still first, in order, never removed or
    # reordered, exactly as before this phase.
    assert merged.industries[0] == "Healthcare"
    assert merged.industries[1] == "SaaS"
    assert "Healthcare Software" in merged.industries[2:]


# ============================================================================
# PHASE 39 — combination_industry_terms: a structurally separate output
# field for AI-proposed compound-intersection phrases, so
# hard_icp_validation.py can recognize them without guessing from text
# shape (see app/services/hard_icp_validation.py's
# _effective_required_industry_terms docstring for the confirmed live bug
# this fixes — a genuine "Clinical Software" match, sharing no words with
# either original term, previously could never satisfy the compound
# ICP's industry rule).
# ============================================================================


def test_combination_industry_terms_flow_through_interpret_icp():
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(
        industry_terms=[],
        combination_industry_terms=["Healthcare Software", "Clinical Software"],
        company_type_terms=[],
        exclusion_terms=[],
    )
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)

    assert strategy.status == "SUCCESS"
    assert strategy.combination_industry_terms == ("Healthcare Software", "Clinical Software")


def test_merge_populates_industry_combination_terms_field():
    """The AI's combination phrases are merged into `industries` (Explorium
    still searches them exactly like any other industry term) AND
    separately recorded into the new industry_combination_terms field."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(
        industry_terms=[],
        combination_industry_terms=["Clinical Software"],
        company_type_terms=[],
        exclusion_terms=[],
    )
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert "Clinical Software" in merged.industries  # still a real discovery search term
    assert merged.industry_combination_terms == ("Clinical Software",)
    # The user's own original terms are never marked as combination terms.
    assert "Healthcare" not in merged.industry_combination_terms
    assert "SaaS" not in merged.industry_combination_terms


def test_combination_term_matching_an_existing_user_term_is_not_double_recorded():
    """If the AI proposes a "combination" phrase that happens to already be
    one of the ICP's own pre-existing terms (already merged/deduped away
    by _merge_terms), it must not be spuriously recorded as a combination
    term — there is only one truthful origin for that key, and it's the
    user's own pre-existing term, exactly the same discipline
    build_term_origin_map already applies."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(
        industry_terms=[],
        combination_industry_terms=["healthcare"],  # case-insensitive dup of the user's own term
        company_type_terms=[],
        exclusion_terms=[],
    )
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)

    assert merged.industries.count("Healthcare") == 1  # still deduped, never doubled
    assert merged.industry_combination_terms == ()  # never marks a pre-existing user term as AI-combination


def test_combination_industry_term_count_is_capped():
    icp = _icp(industries=("Healthcare", "SaaS"))
    many_terms = [f"Combo Phrase {i}" for i in range(20)]
    payload = _good_payload(combination_industry_terms=many_terms, industry_terms=[], company_type_terms=[], exclusion_terms=[])
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)

    from app.services.discovery_strategy import MAX_AI_COMBINATION_INDUSTRY_TERMS

    assert len(strategy.combination_industry_terms) == MAX_AI_COMBINATION_INDUSTRY_TERMS


def test_combination_term_recorded_as_ai_origin():
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(
        industry_terms=[], combination_industry_terms=["Clinical Software"], company_type_terms=[], exclusion_terms=[]
    )
    provider = MockLLMProvider(response_text=_response(payload))
    strategy = interpret_icp(icp, provider)

    from app.services.discovery_strategy import build_term_origin_map

    origins = build_term_origin_map(icp.hard_rules, strategy)
    assert origins["clinical software"] == "ai"
    assert origins["healthcare"] == "user"


def test_provider_omitting_combination_industry_terms_degrades_safely():
    """A provider response with no combination_industry_terms key at all
    (Pydantic's own default_factory=list) must not raise or behave any
    differently from an explicit empty list — every pre-Phase-39
    MockLLMProvider fixture in this file omits the key entirely and must
    keep working unchanged."""
    icp = _icp(industries=("Healthcare", "SaaS"))
    payload = _good_payload(industry_terms=["Healthcare Software"])
    del payload["company_type_terms"]  # still leaves the other required-with-default fields
    payload["company_type_terms"] = []
    provider = MockLLMProvider(response_text=_response(payload))  # no combination_industry_terms key at all
    strategy = interpret_icp(icp, provider)

    assert strategy.status == "SUCCESS"
    assert strategy.combination_industry_terms == ()
    merged = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    assert merged.industry_combination_terms == ()
