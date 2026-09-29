"""Phase 10 — OpenAI ICP Interpreter.

Expands a CanonicalICP's own industry/company-type language into additional
CANDIDATE terms before company discovery runs — never a second source of
truth for what an Explorium taxonomy value is, and never a replacement for
the user's own ICP.

Ordering, once per ICP submission (never per candidate/company):

    CanonicalICP
      -> build_discovery_strategy_request()   (compact, deterministic)
      -> provider.qualify(request)            (ONE LLMProvider call)
      -> parse + validate RawDiscoveryStrategyOutput (strict schema)
      -> cap term counts
      -> DiscoveryStrategy (always produced, success or failure — see below)
      -> merge_strategy_into_hard_rules()     (additive only, never removes
                                                 a user-typed term)
      -> build_company_discovery_query()      (app/services/company_discovery.py,
                                                 UNCHANGED)
      -> Explorium's existing linkedin_category -> naics_category ->
         keyword cascade (app/providers/explorium.py, UNCHANGED)

Anti-fabrication, structurally enforced (mirrors Phase 16's own discipline
in app/services/llm_qualification.py, applied here to discovery instead of
qualification):

  - The LLM is never asked for, and the schema cannot represent, a resolved
    Explorium taxonomy value (no `linkedin_category`/`naics_category`
    field exists anywhere in DiscoveryStrategy or its JSON schema — see
    app/schemas/discovery_strategy.py and
    app/services/llm_providers/openai_provider.py's
    DISCOVERY_STRATEGY_RESPONSE_SCHEMA). It can only propose free-text
    terms, in the exact shape CompanyDiscoveryQuery.industries/
    company_types already accept from user-typed ICP fields today.
  - Whether any given term (user-typed or AI-proposed) actually resolves
    to a real Explorium category is decided ONLY by
    ExploriumCompanyDiscoveryProvider's own live autocomplete call
    (_lookup_category) — this module never touches that logic, never
    guesses at a taxonomy value itself, and cannot bypass it.
  - A provider failure, malformed JSON, or schema violation degrades to
    the ICP's own original terms, unexpanded — it NEVER blocks discovery,
    and it never silently invents a fallback expansion of its own. See
    `interpret_icp`'s failure branches.
  - Merging is strictly additive (see merge_strategy_into_hard_rules): no
    user-typed industries/company_types/exclusions/geography value is ever
    removed, reordered away, or overridden by an AI-proposed one.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.schemas.candidate_company import CompanyDiscoveryQuery
from app.schemas.canonical_icp import CanonicalHardRules, CanonicalICP
from app.schemas.discovery_strategy import DiscoveryStrategy, DiscoveryStrategyRequest, RawDiscoveryStrategyOutput
from app.services.company_discovery import build_company_discovery_query
from app.services.icp_normalization import clean_text
from app.services.llm_providers.base import LLMProvider

PROMPT_VERSION = "phase10-v3"  # bumped: Phase 39 — combination_industry_terms is now a separate output field

# A hard ceiling on how many AI-proposed terms can be merged per field, so
# recall improvement can never explode keyword-fallback volume (see the
# Phase 10 audit's Q9: each additional term is a potential keyword-tier
# hit in app/providers/explorium.py's cascade, capped here rather than
# inside Explorium itself). Chosen independently for industries vs.
# company_types since they're merged into separate CompanyDiscoveryQuery
# fields and go through separate keyword-branch budgets.
MAX_AI_INDUSTRY_TERMS = 6
MAX_AI_COMBINATION_INDUSTRY_TERMS = 6
MAX_AI_COMPANY_TYPE_TERMS = 4
MAX_AI_EXCLUSION_TERMS = 8

_SYSTEM_INSTRUCTIONS = (
    "You are a company-discovery term-expansion assistant. You are NOT a company database and you do not "
    "know Explorium's actual taxonomy — you may only PROPOSE free-text candidate terms, never declare or "
    "invent a specific taxonomy category, code, or label as if it were verified.\n\n"
    "COMPOUND vs ALTERNATIVE industries — read the ICP's industries list carefully before expanding it:\n"
    "- Some ICPs list MULTIPLE industry terms that together describe ONE compound business concept the user "
    "wants companies operating at the INTERSECTION of (e.g. industries=['Healthcare','SaaS'] means software "
    "companies that serve healthcare — NOT any healthcare company, and NOT any SaaS company). For a compound "
    "ICP like this, do not expand each term independently into its own separate synonym cluster (that produces "
    "irrelevant companies matching only ONE side, e.g. a plain hospital or an unrelated SaaS tool). Instead, "
    "prioritize proposing TERMS THAT NAME THE COMBINATION ITSELF as a single phrase — e.g. for "
    "['Healthcare','SaaS']: 'Healthcare Software', 'Health Tech Software', 'Clinical Software', 'Medical "
    "Software' — real compound-industry phrasings a company-database taxonomy might list as ONE category, not "
    "a Healthcare synonym plus a separate SaaS synonym. Apply this same combination logic to ANY compound "
    "pairing (e.g. ['Fintech','SaaS'] -> 'Fintech Software', 'Financial Software'; ['D2C','Healthcare'] -> "
    "'Direct-to-Consumer Health', 'D2C Wellness Brands'; ['FMCG'] alone has no second term to combine with, so "
    "expand it normally). Put EVERY such combination phrase in combination_industry_terms, NEVER in "
    "industry_terms — this distinction matters downstream even when the phrase does not literally repeat any "
    "of the ICP's own words (e.g. 'Clinical Software' for ['Healthcare','SaaS'] still belongs in "
    "combination_industry_terms, not industry_terms, because it is still meant to name the intersection, not a "
    "synonym of just one side).\n"
    "- Some ICPs genuinely list multiple industries as ALTERNATIVES the user would accept any of (e.g. "
    "industries=['Retail','Hospitality'] with no combination reading that makes sense) — for those, expanding "
    "each term independently into industry_terms is correct and expected, and combination_industry_terms "
    "should stay empty.\n"
    "- When genuinely unsure which reading applies, still propose a FEW combination-phrase candidates into "
    "combination_industry_terms (they cost nothing if wrong — an unmatched term simply degrades to a "
    "low-precision fallback downstream) rather than defaulting to independent expansion.\n\n"
    "STAY NARROW: propose only CLOSE, industry-specific synonyms and real compound phrasings — never a broad, "
    "loosely-related, or generic term whose match would likely include companies outside the ICP's actual "
    "intent (a term is only worth proposing if a company matching it would very plausibly satisfy the FULL "
    "stated ICP, not just resemble one word of it). Fewer, tighter terms are better than many loose ones.\n\n"
    "Given an ICP's own industries/company-types/exclusions, propose additional close-synonym industry terms "
    "(industry_terms), compound-intersection phrases (combination_industry_terms), and "
    "company-type terms that a real downstream taxonomy lookup might independently match — do not invent "
    "unrelated industries. "
    "Extract any negative intent (e.g. 'not an agency', 'excluding wholesalers') as exclusion_terms, never as "
    "an industry term. Extract any part of the ICP with no plausible company-database filter (e.g. 'growing "
    "companies', 'recently funded') as unsupported_intent, verbatim, never guessed into a fake filter. Put any "
    "geography phrasing you cannot confidently resolve into geography_notes as plain notes, never a filter. "
    "Respond with strict JSON matching the required schema only."
)


def build_discovery_strategy_request(icp: CanonicalICP) -> DiscoveryStrategyRequest:
    """Compact, deterministic: the same ICP always builds the same request.
    Reads only icp.hard_rules directly plus a flattened view of soft
    preferences for context — never re-derives normalization, mirroring
    app/services/qualification_context.py's own "select and compress,
    never re-derive" discipline."""
    hard = icp.hard_rules
    soft = icp.soft_preferences
    geography_terms = tuple(entry.label for entry in hard.geography.countries) + hard.geography.unrecognized
    # Phase 4 fix: soft.custom_preferences (label+description pairs — e.g.
    # a "revenue"/"funding"/"technology" filter with no dedicated
    # HardRules/SoftPreferences field yet, see
    # app/services/filter_projection.py's own comment) was previously
    # omitted here entirely — silently invisible to this passive context,
    # exactly like app/services/qualification_context.py's own identical
    # gap (see that module's _soft_preference_terms for the full root
    # cause). This is passive CONTEXT only: the LLM's own strict output
    # schema (RawDiscoveryStrategyOutput) still only ever proposes
    # industry_terms/company_type_terms/exclusion_terms/geography_notes/
    # unsupported_intent — adding more context here creates no new output
    # channel and cannot let the LLM invent a fake taxonomy term from it.
    custom_terms = tuple(f"{c.label}: {c.description}" for c in soft.custom_preferences)
    soft_terms = (*soft.business_models, *soft.commercial_signals, *soft.growth_signals, *soft.marketing_signals, *custom_terms)
    employee_label = "not constrained"
    if hard.employee_range.min is not None or hard.employee_range.max is not None:
        lo = hard.employee_range.min if hard.employee_range.min is not None else "0"
        hi = hard.employee_range.max if hard.employee_range.max is not None else "unbounded"
        employee_label = f"{lo}-{hi}"

    return DiscoveryStrategyRequest(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        industries=hard.industries,
        geography=geography_terms,
        employee_range=employee_label,
        company_types=hard.company_types,
        exclusions=hard.exclusions,
        soft_preferences=soft_terms,
    )


def build_discovery_strategy_prompt(request: DiscoveryStrategyRequest) -> str:
    """A compact, deterministic textual rendering of the request — the
    same request always renders to the same prompt string, mirroring
    app/services/llm_qualification.py::build_prompt."""
    payload = {
        "icp_id": request.icp_id,
        "icp_version": request.icp_version,
        "industries": request.industries,
        "geography": request.geography,
        "employee_range": request.employee_range,
        "company_types": request.company_types,
        "exclusions": request.exclusions,
        "soft_preferences": request.soft_preferences,
    }
    return _SYSTEM_INSTRUCTIONS + "\n\n" + json.dumps(payload, sort_keys=True)


def _cap(terms: tuple[str, ...], limit: int) -> tuple[str, ...]:
    """Cleans, dedupes (case-insensitive, order-preserving), then caps —
    the same discipline as icp_normalization.py's own _clean_string_list,
    applied here to AI-proposed terms rather than user-typed ones."""
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = clean_text(term)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return tuple(result)


def _degraded_strategy(icp: CanonicalICP, status: str, provider: LLMProvider, error_message: str) -> DiscoveryStrategy:
    return DiscoveryStrategy(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        status=status,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        error_message=error_message,
    )


def interpret_icp(icp: CanonicalICP, provider: LLMProvider) -> DiscoveryStrategy:
    """Pure orchestration: no database access, no discovery call. Exactly
    one LLMProvider call, regardless of how many candidates discovery will
    later return — this function is called once per ICP submission by the
    caller (never in a per-candidate loop), mirroring Phase 16's
    qualify_lead being called once per candidate rather than in a batch
    loop of its own.

    Every failure mode degrades to a DiscoveryStrategy carrying no
    proposed terms at all (status != SUCCESS) rather than raising — the
    caller (merge_strategy_into_hard_rules) always has a safe, valid
    DiscoveryStrategy to merge, and merging an empty strategy is a no-op
    that leaves the ICP's own terms completely unchanged. Discovery is
    never blocked by an LLM outage."""
    request = build_discovery_strategy_request(icp)
    response = provider.qualify(request)

    if not response.success:
        error = response.error
        message = error.message if error else "unknown provider failure"
        return _degraded_strategy(icp, "PROVIDER_UNAVAILABLE", provider, message)

    if not response.raw_text or not response.raw_text.strip():
        return _degraded_strategy(icp, "PROVIDER_UNAVAILABLE", provider, "provider returned an empty response")

    try:
        parsed_json = json.loads(response.raw_text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _degraded_strategy(icp, "MALFORMED_OUTPUT", provider, f"could not parse JSON: {exc}")

    try:
        output = RawDiscoveryStrategyOutput.model_validate(parsed_json)
    except ValidationError as exc:
        return _degraded_strategy(icp, "SCHEMA_INVALID", provider, f"schema validation failed: {exc}")

    return DiscoveryStrategy(
        icp_id=icp.icp_id,
        icp_version=icp.version,
        status="SUCCESS",
        industry_terms=_cap(tuple(output.industry_terms), MAX_AI_INDUSTRY_TERMS),
        combination_industry_terms=_cap(tuple(output.combination_industry_terms), MAX_AI_COMBINATION_INDUSTRY_TERMS),
        company_type_terms=_cap(tuple(output.company_type_terms), MAX_AI_COMPANY_TYPE_TERMS),
        exclusion_terms=_cap(tuple(output.exclusion_terms), MAX_AI_EXCLUSION_TERMS),
        geography_notes=tuple(clean_text(n) for n in output.geography_notes if clean_text(n)),
        unsupported_intent=tuple(clean_text(n) for n in output.unsupported_intent if clean_text(n)),
        confidence=output.confidence,
        reasoning=output.reasoning,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
    )


def _merge_terms(original: tuple[str, ...], proposed: tuple[str, ...]) -> tuple[str, ...]:
    """Additive, order-preserving, case-insensitive dedup: every original
    term is kept, in its original order, first — proposed terms are only
    ever appended, never substituted for or reordered ahead of an
    original one."""
    seen = {t.lower() for t in original}
    merged = list(original)
    for term in proposed:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(term)
    return tuple(merged)


def merge_strategy_into_hard_rules(hard_rules: CanonicalHardRules, strategy: DiscoveryStrategy) -> CanonicalHardRules:
    """The ONLY place AI-proposed terms ever reach the ICP's own hard
    rules — additive only (see _merge_terms), and only ever this one
    merged CanonicalHardRules is what build_company_discovery_query
    (app/services/company_discovery.py, UNCHANGED) ever sees. Explorium
    itself receives no new field, no new code path, and no awareness that
    any given term originated from an LLM rather than the user.

    Phase 39: strategy.combination_industry_terms is merged into
    `industries` exactly like strategy.industry_terms (Explorium still
    tries every combination phrase through the SAME structured/naics/
    keyword cascade, unchanged) — but the merged terms that ACTUALLY
    survived into `industries` (i.e. weren't already present as some
    other term, case-insensitively) are ALSO recorded into the new
    `industry_combination_terms` field, so app/services/
    hard_icp_validation.py can later recognize which of `industries`'
    entries are AI-proposed compound-intersection phrases rather than
    independently-required terms — see CanonicalHardRules.
    industry_combination_terms's own docstring for why that distinction
    matters. Only terms genuinely newly merged in are recorded (never a
    term that was already one of the ICP's own pre-existing industries,
    which _merge_terms's dedup would have silently absorbed into the
    'user' side of the merge — see build_term_origin_map's own identical
    "there's only ever one truthful origin" comment for this exact case).

    A strategy with status != SUCCESS carries no terms (enforced by
    DiscoveryStrategy's own validator) so merging it is always a safe
    no-op that returns hard_rules unchanged in every field this module
    touches."""
    merged_industries = _merge_terms(hard_rules.industries, (*strategy.industry_terms, *strategy.combination_industry_terms))
    pre_merge_keys = {t.lower() for t in hard_rules.industries}
    combination_terms = tuple(
        t for t in strategy.combination_industry_terms if t.lower() not in pre_merge_keys
    )
    return hard_rules.model_copy(
        update={
            "industries": merged_industries,
            "industry_combination_terms": _merge_terms(hard_rules.industry_combination_terms, combination_terms),
            "company_types": _merge_terms(hard_rules.company_types, strategy.company_type_terms),
            "exclusions": _merge_terms(hard_rules.exclusions, strategy.exclusion_terms),
        }
    )


def build_term_origin_map(hard_rules_before_merge: CanonicalHardRules, strategy: DiscoveryStrategy) -> dict[str, str]:
    """Phase 13D — the ONLY place user-vs-AI term origin is still knowable
    at all: merge_strategy_into_hard_rules (above) deliberately collapses
    user-typed and AI-proposed terms into ONE indistinguishable tuple, by
    design (see its own docstring — "Explorium itself receives no new
    field... and no awareness that any given term originated from an LLM
    rather than the user"), and that design is UNCHANGED by this function.
    This captures the origin BEFORE that collapse happens, as a small,
    separate, purely-additive side map — never a change to
    CompanyDiscoveryQuery's schema or to what Explorium's real API
    request contains (see app/providers/explorium.py::execute()'s own
    term_origin comment: it is read only to tag NormalizedRecord.attributes
    for Phase 12's benefit, never sent to Explorium itself).

    Returns {cleaned_term.casefold(): "user" | "ai"} across both industries
    and company_types together (a term appearing as both a user term and
    an AI-proposed term — the realistic Phase 10 case where the AI simply
    reaffirms what the user already typed — is correctly reported as
    "user", since _merge_terms's own case-insensitive dedup means the AI's
    duplicate is silently absorbed and never separately present in the
    merged tuple at all; there's only ever one truthful origin for that
    key, and it's the one that was already there first).

    Called ONLY at the one real integration point (see
    app/services/batch_orchestration.py::_run_one_discovery_round) that
    has BOTH pieces of information simultaneously — before
    merge_strategy_into_hard_rules is called, not after. Pure, no
    side effects, no new LLM call (interpret_icp has already run by the
    time this is called; this function makes zero provider calls of its
    own)."""
    origins: dict[str, str] = {}
    for term in (*hard_rules_before_merge.industries, *hard_rules_before_merge.company_types):
        origins[clean_text(term).casefold()] = "user"
    for term in (*strategy.industry_terms, *strategy.combination_industry_terms, *strategy.company_type_terms):
        key = clean_text(term).casefold()
        origins.setdefault(key, "ai")
    return origins


def interpret_icp_and_build_query(
    icp: CanonicalICP, provider: LLMProvider, limit: int = 20
) -> tuple[CompanyDiscoveryQuery, DiscoveryStrategy]:
    """The single, composed entry point a caller wires in wherever it
    currently calls build_company_discovery_query(icp, limit) directly
    (app/services/company_discovery.py) — a drop-in replacement, not a
    parallel code path. Calls the LLMProvider exactly ONCE (interpret_icp
    makes exactly one provider.qualify() call; nothing here loops per
    candidate or per Explorium branch/page). Returns both the merged
    query AND the DiscoveryStrategy itself so a caller can log/persist
    strategy.unsupported_intent, strategy.reasoning, and strategy.status
    for audit/UI — never silently swallowed.

    Deliberately NOT wired into app/services/batch_orchestration.py in
    this phase: batch_orchestration.py rebuilds its CanonicalICP fresh on
    every discovery round (including "Find More" continuation), and this
    codebase has no persisted-strategy-per-batch field yet — adding one
    would be a BatchModel/persistence change beyond this phase's stated
    minimal scope. A caller integrating this into the batch flow should
    call it once, at batch creation, and reuse the returned
    CompanyDiscoveryQuery.industries/company_types (already merged) as the
    ICP's own terms for subsequent rounds, rather than calling this
    function again per round.
    """
    strategy = interpret_icp(icp, provider)
    merged_hard_rules = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    merged_icp = icp.model_copy(update={"hard_rules": merged_hard_rules})
    query = build_company_discovery_query(merged_icp, limit=limit)
    return query, strategy
