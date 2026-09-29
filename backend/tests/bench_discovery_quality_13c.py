"""Phase 13C — Discovery Quality Benchmark.

NOT a pytest test file (no test_ prefix, excluded from the suite) — a
standalone script run manually via
`python tests/bench_discovery_quality_13c.py`. Read-only, no production
code modified. No real API credits consumed.

Runs the REAL, unmodified production code path for 8 realistic ICPs:

    CanonicalICP
      -> interpret_icp()                       [discovery_strategy.py, UNCHANGED]
      -> merge_strategy_into_hard_rules()       [UNCHANGED]
      -> ExploriumCompanyDiscoveryProvider      [explorium.py, UNCHANGED — including
                                                  Phase 13A's cache fix and Phase 13B's
                                                  keyword_match_terms tagging]
      -> the SAME field mapping evidence_import.py uses (replicated here,
         not re-derived)
      -> validate_against_icp() / evaluate_hard_rules() [UNCHANGED]

TWO THINGS ARE MOCKED, both clearly labeled in the printed output — nothing
else is, exactly the same discipline as the Phase 10-continuation
evaluation script this benchmark extends:

  1. The OpenAI call: no OPENAI_API_KEY is configured in this environment
     (confirmed absent from .env), so interpret_icp() is given a
     MockLLMProvider with a HAND-AUTHORED response per ICP, written to
     represent a PLAUSIBLE real GPT output. Labeled "SIMULATED LLM OUTPUT"
     everywhere.

  2. Explorium's HTTP layer: respx mocks the autocomplete + search
     endpoints with HAND-AUTHORED, REALISTIC response bodies, deliberately
     including plausible false-positive candidates (agencies,
     consultancies, staffing firms, platforms-vs-content-makers, adjacent
     industries) to genuinely stress-test the pipeline. Every mocked
     response is printed alongside the real request the adapter actually
     sent.

USER-VS-AI TERM PROVENANCE NOTE: app/services/discovery_strategy.py::
merge_strategy_into_hard_rules deliberately merges user-typed and
AI-proposed terms into ONE indistinguishable industries/company_types
list — confirmed in code, this is by design (Phase 10). A REAL running
system cannot recover, after the fact, which specific term a candidate's
keyword_match_terms tag traces back to (user-typed vs. AI-proposed) — only
THIS benchmark harness can, because it independently tracks the ICP's
original terms and the simulated strategy's proposed terms BEFORE they are
merged. Every per-term precision number below is annotated with its known
origin for that reason, with this caveat stated once here rather than
re-stated per line.

RECALL: not claimed anywhere in this benchmark. There is no reference set
of "every company that genuinely matches this ICP in the real world" — only
a hand-labeled precision judgment against the specific mocked candidates
each scenario returns. Coverage/breadth (candidate counts) is reported
as a separate, distinct metric from precision.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.services.discovery_strategy import interpret_icp, merge_strategy_into_hard_rules
from app.services.hard_icp_validation import validate_against_icp
from app.services.llm_providers.mock import MockLLMProvider

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Global tallies across all scenarios
# ---------------------------------------------------------------------------
GLOBAL = {
    "structured_relevant": 0,
    "structured_false_positive": 0,
    "structured_ambiguous": 0,
    "keyword_relevant": 0,
    "keyword_false_positive": 0,
    "keyword_ambiguous": 0,
    "total_candidates": 0,
    "total_autocomplete_calls": 0,
    "total_search_calls": 0,
    "duplicate_business_ids_seen": 0,
}
PER_TERM = defaultdict(lambda: {"relevant": 0, "false_positive": 0, "ambiguous": 0, "origin": set()})
FALSE_POSITIVE_REASONS: list[str] = []


def evidence_from_candidate(company_id: str, provider_id: str, external_id: str, name: str, attributes: dict) -> list[EvidenceRecord]:
    """Faithful replica of evidence_import.py's real field mapping,
    including Phase 11/13B evidence_text provenance carry-through."""
    from app.services.evidence_import import _industry_match_provenance

    def rec(field: str, value) -> EvidenceRecord:
        return EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id=company_id, field=field, value=value,
            source_provider_id=provider_id, source_type=SourceType.PROVIDER, external_id=external_id,
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        )

    records = [rec("company_identity", name)]
    if attributes.get("domain"):
        records.append(rec("domain", attributes["domain"]))
    if isinstance(attributes.get("employee_count"), int):
        records.append(rec("employee_count", attributes["employee_count"]))
    if attributes.get("industry"):
        provenance = _industry_match_provenance(attributes)
        records.append(EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id=company_id, field="industry",
            value=attributes["industry"], source_provider_id=provider_id, source_type=SourceType.PROVIDER,
            external_id=external_id, retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
            evidence_text=provenance,
        ))
    if attributes.get("country"):
        records.append(rec("country", attributes["country"]))
    if attributes.get("employee_range"):
        records.append(rec("employee_range", attributes["employee_range"]))
    return records


def print_header(title: str) -> None:
    print("\n" + "=" * 110)
    print(title)
    print("=" * 110)


def _branch_responder(branch_responses: dict):
    """Routes a mocked Explorium /businesses POST to the response for the
    branch its ACTUAL filter body indicates — critical for this benchmark's
    validity: each of Explorium's up-to-3 parallel branch calls
    (linkedin_category / naics_category / website_keywords) is a genuinely
    separate request in the real API, and must return DIFFERENT candidates
    in a realistic mock, exactly like the real API would. Routing every
    branch to the SAME unconditional response (this benchmark's first,
    invalidated draft) causes the merge loop's cross-branch dedup to keep
    only the first branch's tag for any business_id present in more than
    one branch's mocked data — silently collapsing every candidate to
    "structured" and hiding keyword-fallback candidates entirely. Fixed
    here by inspecting the real request body actually sent."""

    def _respond(request):
        body = json.loads(request.content)
        filters = body.get("filters", {})
        if "linkedin_category" in filters:
            data = branch_responses.get("structured", [])
        elif "naics_category" in filters:
            data = branch_responses.get("naics", [])
        elif "website_keywords" in filters:
            data = branch_responses.get("keyword", [])
        else:
            data = branch_responses.get("plain", [])
        return httpx.Response(200, json={"data": data, "page": None})

    return _respond


def run_scenario(
    scenario_name: str,
    icp: CanonicalICP,
    llm_response_payload: dict,
    ai_term_origin: dict,  # term (lowercased) -> "AI" or "user", tracked by THIS harness only
    autocomplete_mocks: list[dict],
    branch_responses: dict,  # {"structured": [...], "naics": [...], "keyword": [...]}
    relevance_labels: dict[str, tuple[str, str]],  # external_id -> (label, reason)
) -> None:
    print_header(f"SCENARIO: {scenario_name}")
    print(f"\nICP industries (user-typed): {icp.hard_rules.industries}")
    print(f"ICP company_types (user-typed): {icp.hard_rules.company_types}")

    print("\n--- SIMULATED LLM OUTPUT (not a live OpenAI call) ---")
    print(json.dumps(llm_response_payload, indent=2))

    provider = MockLLMProvider(response_text=json.dumps(llm_response_payload))
    strategy = interpret_icp(icp, provider)
    merged_hard_rules = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    merged_icp = icp.model_copy(update={"hard_rules": merged_hard_rules})
    print(f"Merged industries reaching Explorium: {merged_hard_rules.industries}")
    print(f"Merged company_types reaching Explorium: {merged_hard_rules.company_types}")

    with respx.mock:
        for mock in autocomplete_mocks:
            respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": mock["field"], "query": mock["query"]}).mock(
                return_value=httpx.Response(200, json=mock["response"])
            )
        respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
        search_route = respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_branch_responder(branch_responses))

        provider_adapter = ExploriumCompanyDiscoveryProvider(api_key="bench-key-not-real")
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_DISCOVERY,
            query={
                "industries": merged_hard_rules.industries,
                "geography_codes": tuple(e.code for e in merged_icp.hard_rules.geography.countries),
                "company_types": merged_hard_rules.company_types,
                "min_employees": merged_icp.hard_rules.employee_range.min,
                "max_employees": merged_icp.hard_rules.employee_range.max,
                "limit": 20,
            },
        )
        response = provider_adapter.run(request)

        autocomplete_call_count = sum(r.call_count for r in respx.routes) - search_route.call_count
        GLOBAL["total_autocomplete_calls"] += autocomplete_call_count
        GLOBAL["total_search_calls"] += search_route.call_count

    print(f"\nExplorium response: success={response.success}, {len(response.data)} candidates, autocomplete_calls={autocomplete_call_count}, search_calls={search_route.call_count}")

    print("\n--- PER-CANDIDATE RESULT ---")
    seen_ids = set()
    for record in response.data:
        if record.external_id in seen_ids:
            GLOBAL["duplicate_business_ids_seen"] += 1
        seen_ids.add(record.external_id)
        GLOBAL["total_candidates"] += 1

        evidence = evidence_from_candidate("company-1", "explorium-company-discovery-v1", record.external_id, record.name, record.attributes)
        validation = validate_against_icp(merged_icp, record.external_id, evidence, None, [])
        overall = validation.evaluation.overall_result.value

        tier = "structured" if "industry_match_branch" in record.attributes else ("keyword" if "keyword_match_terms" in record.attributes else "none")
        terms = record.attributes.get("industry_match_terms") or record.attributes.get("keyword_match_terms") or []

        label, reason = relevance_labels.get(record.external_id, ("UNLABELED", ""))
        tally_key = "structured" if tier == "structured" else "keyword"
        if label == "RELEVANT":
            GLOBAL[f"{tally_key}_relevant"] += 1
        elif label == "FALSE_POSITIVE":
            GLOBAL[f"{tally_key}_false_positive"] += 1
            FALSE_POSITIVE_REASONS.append(f"[{scenario_name}] {record.name} (tier={tier}, terms={terms}): {reason}")
        elif label == "AMBIGUOUS":
            GLOBAL[f"{tally_key}_ambiguous"] += 1

        for t in terms:
            key = t.lower()
            origin = ai_term_origin.get(key, "unknown")
            PER_TERM[t]["origin"].add(origin)
            if label == "RELEVANT":
                PER_TERM[t]["relevant"] += 1
            elif label == "FALSE_POSITIVE":
                PER_TERM[t]["false_positive"] += 1
            elif label == "AMBIGUOUS":
                PER_TERM[t]["ambiguous"] += 1

        print(f"\n  {record.name}")
        print(f"    domain={record.attributes.get('domain')}  industry={record.attributes.get('industry')}  employee_range={record.attributes.get('employee_range')}")
        print(f"    tier={tier}  matched_term(s)={terms}")
        print(f"    hard-rule={overall}  LABEL={label}" + (f"  reason={reason}" if reason else ""))

    print(f"\n--- SCENARIO SUMMARY: {scenario_name} ({len(response.data)} candidates) ---")


# ============================================================================
# SCENARIO 1: D2C
# ============================================================================
def scenario_d2c():
    icp = CanonicalICP(
        icp_id="b1", version=1,
        hard_rules=CanonicalHardRules(
            industries=("D2C skincare",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="India", code="IN", label="India"),)),
            employee_range=EmployeeRange(min=20, max=300),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Cosmetics, Beauty Supplies, and Perfume Stores", "Consumer Goods", "E-commerce"],
        "company_type_terms": ["D2C"], "exclusion_terms": ["agency", "consultancy"],
        "geography_notes": [], "unsupported_intent": ["growing companies"], "confidence": 78,
        "reasoning": "D2C skincare has no exact linkedin_category match.",
    }
    ai_origin = {"cosmetics, beauty supplies, and perfume stores": "AI", "consumer goods": "AI", "e-commerce": "AI", "d2c": "AI", "d2c skincare": "user"}
    autocomplete_mocks = [
        # "D2C skincare" (the user's own raw term) has no exact match at
        # either structured tier — it alone falls to keyword.
        {"field": "linkedin_category", "query": "D2C skincare", "response": []},
        {"field": "naics_category", "query": "D2C skincare", "response": []},
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {"field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": [{"query": "Cosmetics, Beauty Supplies, and Perfume Stores", "label": "Cosmetics, Beauty Supplies, and Perfume Stores", "value": "446120"}]},
        {"field": "linkedin_category", "query": "Consumer Goods", "response": [{"query": "Consumer Goods", "label": "Consumer Goods", "value": "consumer-goods"}]},
        {"field": "linkedin_category", "query": "E-commerce", "response": [{"query": "E-commerce", "label": "E-commerce", "value": "e-commerce"}]},
        {"field": "linkedin_category", "query": "D2C", "response": [{"query": "D2C", "label": "D2C", "value": "d2c"}]},
    ]
    branch_responses = {
        # linkedin_category branch: seeded by Consumer Goods + E-commerce (industries) + D2C (company_types)
        "structured": [
            {"business_id": "d2c002", "name": "Beauty Brand Growth Partners", "domain": "beautybrandgrowth.invalid", "country_name": "India", "number_of_employees_range": "11-50", "naics_description": "Consumer Goods"},
            {"business_id": "d2c004", "name": "ShopBuild E-commerce Agency", "domain": "shopbuild.invalid", "country_name": "India", "number_of_employees_range": "11-50", "naics_description": "E-commerce"},
        ],
        # naics_category branch: seeded by "Cosmetics, Beauty Supplies, and Perfume Stores"
        "naics": [
            {"business_id": "d2c001", "name": "Glow & Co Skincare", "domain": "glowandco.invalid", "country_name": "India", "number_of_employees_range": "51-200", "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores"},
            {"business_id": "d2c003", "name": "Radiance Naturals", "domain": "radiancenaturals.invalid", "country_name": "India", "number_of_employees_range": "51-200", "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores"},
        ],
        # website_keywords branch: seeded by the raw, unresolved "D2C skincare" term
        "keyword": [
            {"business_id": "d2c005", "name": "SkinLaunch Consulting", "domain": "skinlaunch.invalid", "country_name": "India", "number_of_employees_range": "11-50", "naics_description": "Management Consulting"},
        ],
    }
    labels = {
        "d2c001": ("RELEVANT", ""),
        "d2c002": ("FALSE_POSITIVE", "A growth/marketing agency serving beauty brands, not a D2C brand itself — matched via AI-expanded 'Consumer Goods' term."),
        "d2c003": ("RELEVANT", ""),
        "d2c004": ("FALSE_POSITIVE", "A Shopify/e-commerce build agency, not a D2C product company — matched via AI-expanded 'E-commerce' term."),
        "d2c005": ("FALSE_POSITIVE", "A D2C-skincare-focused MANAGEMENT CONSULTANCY, not a D2C brand itself — matched via keyword fallback on the raw, unresolved 'D2C skincare' term mentioning 'D2C' and 'skincare' on its site."),
    }
    run_scenario("D2C — skincare, India", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 2: FMCG
# ============================================================================
def scenario_fmcg():
    icp = CanonicalICP(
        icp_id="b2", version=1,
        hard_rules=CanonicalHardRules(
            industries=("FMCG",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=50, max=1000),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Consumer Goods", "Food & Beverages", "Food Production"],
        "company_type_terms": [], "exclusion_terms": ["distributor", "brokerage"],
        "geography_notes": [], "unsupported_intent": [], "confidence": 70,
        "reasoning": "FMCG is an industry category umbrella term, not a single exact taxonomy label.",
    }
    ai_origin = {"consumer goods": "AI", "food & beverages": "AI", "food production": "AI", "fmcg": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "FMCG", "response": []},
        {"field": "naics_category", "query": "FMCG", "response": []},
        {"field": "linkedin_category", "query": "Consumer Goods", "response": [{"query": "Consumer Goods", "label": "Consumer Goods", "value": "consumer-goods"}]},
        {"field": "linkedin_category", "query": "Food & Beverages", "response": [{"query": "Food & Beverages", "label": "Food & Beverages", "value": "food-beverages"}]},
        {"field": "linkedin_category", "query": "Food Production", "response": []},
        {"field": "naics_category", "query": "Food Production", "response": [{"query": "Food Production", "label": "Food Production", "value": "311999"}]},
    ]
    branch_responses = {
        # linkedin_category: Consumer Goods + Food & Beverages
        "structured": [
            {"business_id": "fmcg002", "name": "National FMCG Distribution Group", "domain": "nationalfmcgdist.invalid", "country_name": "United States", "number_of_employees_range": "1001-5000", "naics_description": "Consumer Goods"},
            {"business_id": "fmcg003", "name": "Riverbend Snack Co", "domain": "riverbendsnack.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Food & Beverages"},
        ],
        # naics_category: Food Production
        "naics": [
            {"business_id": "fmcg001", "name": "Harvest Grove Foods", "domain": "harvestgrove.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Food Production"},
        ],
        # website_keywords: the raw, unresolved "FMCG" term
        "keyword": [
            {"business_id": "fmcg004", "name": "FMCG Insights Media", "domain": "fmcginsights.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Media & Publishing"},
        ],
    }
    labels = {
        "fmcg001": ("RELEVANT", ""),
        "fmcg002": ("FALSE_POSITIVE", "A distribution/logistics company that moves FMCG goods for other brands, not an FMCG manufacturer itself — matched via AI-expanded 'Consumer Goods'."),
        "fmcg003": ("RELEVANT", ""),
        "fmcg004": ("FALSE_POSITIVE", "An FMCG-industry trade publication/media company, not an FMCG brand — matched via keyword fallback on the raw 'FMCG' term appearing in its content."),
    }
    run_scenario("FMCG — food & beverage, US", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 3: Healthcare
# ============================================================================
def scenario_healthcare():
    icp = CanonicalICP(
        icp_id="b3", version=1,
        hard_rules=CanonicalHardRules(
            industries=("Healthcare",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=50, max=1000),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Hospitals and Health Care", "Medical Practices", "Health, Wellness and Fitness"],
        "company_type_terms": [], "exclusion_terms": ["staffing agency", "recruiting"],
        "geography_notes": [], "unsupported_intent": [], "confidence": 85,
        "reasoning": "Healthcare has a direct linkedin_category match; expanded for recall.",
    }
    ai_origin = {"hospitals and health care": "AI", "medical practices": "AI", "health, wellness and fitness": "AI", "healthcare": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "hospital-health-care"}]},
        {"field": "linkedin_category", "query": "Hospitals and Health Care", "response": [{"query": "Hospitals and Health Care", "label": "Hospitals and Health Care", "value": "hospital-health-care-2"}]},
        {"field": "linkedin_category", "query": "Medical Practices", "response": [{"query": "Medical Practices", "label": "Medical Practices", "value": "medical-practice"}]},
        {"field": "linkedin_category", "query": "Health, Wellness and Fitness", "response": [{"query": "Health, Wellness and Fitness", "label": "Health, Wellness and Fitness", "value": "health-wellness-fitness"}]},
    ]
    branch_responses = {
        # linkedin_category branch: all four terms resolved here per the
        # autocomplete mocks above — this scenario has NO naics_category
        # branch at all, which is itself realistic: a well-covered
        # taxonomy like Healthcare can resolve entirely at the first tier.
        "structured": [
            {"business_id": "hc001", "name": "Midwest Regional Health Group", "domain": "midwestregionalhealth.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "General Medical and Surgical Hospitals"},
            {"business_id": "hc002", "name": "Sunrise Wellness Staffing", "domain": "sunrisewellnessstaffing.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Health, Wellness and Fitness"},
            {"business_id": "hc003", "name": "Pinecrest Medical Practice", "domain": "pinecrestmedical.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Medical Practices"},
        ],
    }
    labels = {
        "hc001": ("RELEVANT", ""),
        "hc002": ("FALSE_POSITIVE", "A healthcare STAFFING agency, not a healthcare provider — matched via the exact linkedin_category label 'Health, Wellness and Fitness' (structured tier false positive, not keyword)."),
        "hc003": ("RELEVANT", ""),
    }
    run_scenario("Healthcare — US", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 4: OTT (streaming platforms)
# ============================================================================
def scenario_ott():
    icp = CanonicalICP(
        icp_id="b4", version=1,
        hard_rules=CanonicalHardRules(
            industries=("OTT platforms",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=10, max=500),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Media Production", "Entertainment", "Broadcast Media"],
        "company_type_terms": ["Streaming"], "exclusion_terms": ["production studio", "talent agency"],
        "geography_notes": [], "unsupported_intent": [], "confidence": 55,
        "reasoning": "OTT (over-the-top streaming) has no exact taxonomy match; proposed adjacent media categories.",
    }
    ai_origin = {"media production": "AI", "entertainment": "AI", "broadcast media": "AI", "streaming": "AI", "ott platforms": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "OTT platforms", "response": []},
        {"field": "naics_category", "query": "OTT platforms", "response": []},
        {"field": "linkedin_category", "query": "Media Production", "response": [{"query": "Media Production", "label": "Media Production", "value": "media-production"}]},
        {"field": "linkedin_category", "query": "Entertainment", "response": [{"query": "Entertainment", "label": "Entertainment", "value": "entertainment"}]},
        {"field": "linkedin_category", "query": "Broadcast Media", "response": [{"query": "Broadcast Media", "label": "Broadcast Media", "value": "broadcast-media"}]},
        {"field": "linkedin_category", "query": "Streaming", "response": []},
        {"field": "naics_category", "query": "Streaming", "response": []},
    ]
    branch_responses = {
        # linkedin_category: Media Production + Entertainment + Broadcast Media
        "structured": [
            {"business_id": "ott001", "name": "StreamVault Media", "domain": "streamvault.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Media Production"},
            {"business_id": "ott002", "name": "Apex Talent & Casting Agency", "domain": "apextalent.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Entertainment"},
            {"business_id": "ott003", "name": "Riverside Broadcast Network", "domain": "riversidebroadcast.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Broadcast Media"},
        ],
        # website_keywords: "OTT platforms" (user term) + "Streaming" (AI company_type term), neither resolved structurally
        "keyword": [
            {"business_id": "ott004", "name": "NextGen Streaming Consultants", "domain": "nextgenstreaming.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Management Consulting"},
        ],
    }
    labels = {
        "ott001": ("AMBIGUOUS", "A media production company — plausibly a content studio for an OTT platform, but 'Media Production' does not confirm it OPERATES a streaming platform vs. merely produces content."),
        "ott002": ("FALSE_POSITIVE", "A talent/casting agency, matched only because it's tagged 'Entertainment' broadly — has nothing to do with operating a streaming platform."),
        "ott003": ("FALSE_POSITIVE", "A traditional broadcast network, not an OTT/streaming platform — 'Broadcast Media' is adjacent but structurally distinct from OTT."),
        "ott004": ("FALSE_POSITIVE", "A consulting firm advising streaming companies, not an OTT platform operator itself — matched via keyword fallback on 'Streaming'/'OTT platforms' mentioned in its service description."),
    }
    run_scenario("OTT — streaming platforms, US", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 5: Microdrama (genuinely niche/emerging)
# ============================================================================
def scenario_microdrama():
    icp = CanonicalICP(
        icp_id="b5", version=1,
        hard_rules=CanonicalHardRules(
            industries=("microdrama short-video production",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=5, max=100),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Media Production", "Entertainment", "Mobile Games"],
        "company_type_terms": [], "exclusion_terms": [],
        "geography_notes": [], "unsupported_intent": ["microdrama specifically — an emerging category with no taxonomy match"], "confidence": 30,
        "reasoning": "Microdrama (short-form vertical video drama apps) has no close taxonomy match at all; proposed only broad, low-confidence parent categories.",
    }
    ai_origin = {"media production": "AI", "entertainment": "AI", "mobile games": "AI", "microdrama short-video production": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "microdrama short-video production", "response": []},
        {"field": "naics_category", "query": "microdrama short-video production", "response": []},
        {"field": "linkedin_category", "query": "Media Production", "response": [{"query": "Media Production", "label": "Media Production", "value": "media-production"}]},
        {"field": "linkedin_category", "query": "Entertainment", "response": [{"query": "Entertainment", "label": "Entertainment", "value": "entertainment"}]},
        {"field": "linkedin_category", "query": "Mobile Games", "response": [{"query": "Mobile Games", "label": "Mobile Games", "value": "mobile-games"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "md001", "name": "ReelDrama Studios", "domain": "reeldrama.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Media Production"},
            {"business_id": "md002", "name": "Titan Mobile Games Inc", "domain": "titanmobile.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Mobile Games"},
            {"business_id": "md003", "name": "Acme Entertainment Law Group", "domain": "acmeent.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Entertainment"},
        ],
        # website_keywords: the raw, unresolved "microdrama short-video production" term
        "keyword": [
            {"business_id": "md004", "name": "VertiFlix", "domain": "vertiflix.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Internet Publishing"},
        ],
    }
    labels = {
        "md001": ("RELEVANT", "Name and Media Production tag plausibly align with vertical drama content — the strongest signal available without richer evidence."),
        "md002": ("FALSE_POSITIVE", "A mobile games company, wrong medium entirely and 5x over the requested employee ceiling — 'Mobile Games' is too broad a proxy for microdrama."),
        "md003": ("FALSE_POSITIVE", "An entertainment LAW firm, matched only via the broad 'Entertainment' tag — completely unrelated business."),
        "md004": ("RELEVANT", "Name plausibly suggests a vertical-video app; found only via keyword fallback since 'microdrama' has no taxonomy match at all — the genuinely obscure candidate a structured-only search would have missed entirely."),
    }
    run_scenario("Microdrama — short-video drama apps, US (niche)", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 6: B2B SaaS
# ============================================================================
def scenario_saas():
    icp = CanonicalICP(
        icp_id="b6", version=1,
        hard_rules=CanonicalHardRules(
            industries=("B2B SaaS",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=10, max=500),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Computer Software", "Information Technology and Services", "Internet"],
        "company_type_terms": ["SaaS"], "exclusion_terms": ["IT staffing", "IT consulting"],
        "geography_notes": [], "unsupported_intent": ["recently funded"], "confidence": 88,
        "reasoning": "B2B SaaS has no single exact taxonomy label; expanded into standard adjacent categories.",
    }
    ai_origin = {"computer software": "AI", "information technology and services": "AI", "internet": "AI", "saas": "AI", "b2b saas": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "B2B SaaS", "response": []},
        {"field": "naics_category", "query": "B2B SaaS", "response": []},
        {"field": "linkedin_category", "query": "Computer Software", "response": [{"query": "Computer Software", "label": "Computer Software", "value": "computer-software"}]},
        {"field": "linkedin_category", "query": "Information Technology and Services", "response": [{"query": "Information Technology and Services", "label": "Information Technology and Services", "value": "it-services"}]},
        {"field": "linkedin_category", "query": "Internet", "response": [{"query": "Internet", "label": "Internet", "value": "internet"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "saas001", "name": "Ledgerly", "domain": "ledgerly.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Computer Software"},
            {"business_id": "saas002", "name": "Apex IT Staffing Solutions", "domain": "apexitstaffing.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Information Technology and Services"},
            {"business_id": "saas003", "name": "Northbeam Analytics", "domain": "northbeamanalytics.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Computer Software"},
            {"business_id": "saas004", "name": "Global Internet Registrars LLC", "domain": "globalregistrars.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Internet"},
        ],
        # website_keywords: "B2B SaaS" (user) + "SaaS" (AI company_type), neither resolved structurally
        "keyword": [
            {"business_id": "saas005", "name": "SaaS Growth Advisors", "domain": "saasgrowthadvisors.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Management Consulting"},
        ],
    }
    labels = {
        "saas001": ("RELEVANT", ""),
        "saas002": ("FALSE_POSITIVE", "An IT staffing agency, not a software company — matched via AI-expanded 'Information Technology and Services'."),
        "saas003": ("RELEVANT", ""),
        "saas004": ("FALSE_POSITIVE", "A domain registrar, not a SaaS product company — 'Internet' is an extremely broad category, matched via AI-expanded term."),
        "saas005": ("FALSE_POSITIVE", "A consultancy that advises SaaS companies, not a SaaS company itself — matched via keyword fallback on 'SaaS'/'B2B SaaS' in its own marketing copy."),
    }
    run_scenario("B2B SaaS — US", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 7: Niche #1 — Industrial drone inspection
# ============================================================================
def scenario_drone_inspection():
    icp = CanonicalICP(
        icp_id="b7", version=1,
        hard_rules=CanonicalHardRules(
            industries=("industrial drone inspection services",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=5, max=100),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Aviation & Aerospace", "Industrial Automation", "Utilities"],
        "company_type_terms": [], "exclusion_terms": [],
        "geography_notes": [], "unsupported_intent": ["drone inspection specifically — no taxonomy category exists"], "confidence": 45,
        "reasoning": "No close taxonomy match exists; proposed only broad, plausible parent categories, flagged low confidence.",
    }
    ai_origin = {"aviation & aerospace": "AI", "industrial automation": "AI", "utilities": "AI", "industrial drone inspection services": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "industrial drone inspection services", "response": []},
        {"field": "naics_category", "query": "industrial drone inspection services", "response": []},
        {"field": "linkedin_category", "query": "Aviation & Aerospace", "response": [{"query": "Aviation & Aerospace", "label": "Aviation & Aerospace", "value": "aviation-aerospace"}]},
        {"field": "linkedin_category", "query": "Industrial Automation", "response": []},
        {"field": "naics_category", "query": "Industrial Automation", "response": []},
        {"field": "linkedin_category", "query": "Utilities", "response": [{"query": "Utilities", "label": "Utilities", "value": "utilities"}]},
    ]
    branch_responses = {
        # linkedin_category: Aviation & Aerospace + Utilities
        "structured": [
            {"business_id": "drone001", "name": "Continental Airlines Cargo Division", "domain": "continentalcargo.invalid", "country_name": "United States", "number_of_employees_range": "10001+", "naics_description": "Aviation & Aerospace"},
            {"business_id": "drone003", "name": "Metro Power & Utilities Co", "domain": "metropower.invalid", "country_name": "United States", "number_of_employees_range": "5000-10000", "naics_description": "Utilities"},
        ],
        # website_keywords: "industrial drone inspection services" (user) + "Industrial Automation" (AI), neither resolved structurally
        "keyword": [
            {"business_id": "drone002", "name": "SkyGrid Inspections", "domain": "skygridinspections.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Professional Services"},
        ],
    }
    labels = {
        "drone001": ("FALSE_POSITIVE", "A giant airline, not a drone inspection company and 100x over the requested employee ceiling — 'Aviation & Aerospace' is far too broad a proxy."),
        "drone002": ("RELEVANT", "Name plausibly suggests drone inspection, falls in the correct size range — the genuinely obscure niche company, found ONLY via keyword fallback since no structured category exists for this specialization at all."),
        "drone003": ("FALSE_POSITIVE", "A utility company (a plausible drone-inspection CUSTOMER), not a drone inspection vendor — 'Utilities' captures the wrong side of the relationship entirely."),
    }
    run_scenario("Niche #1 — industrial drone inspection, US", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 8: Niche #2 — B2B carbon-accounting software
# ============================================================================
def scenario_carbon_accounting():
    icp = CanonicalICP(
        icp_id="b8", version=1,
        hard_rules=CanonicalHardRules(
            industries=("carbon accounting software",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=5, max=150),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Computer Software", "Environmental Services", "Renewables & Environment"],
        "company_type_terms": ["SaaS"], "exclusion_terms": ["consulting", "auditing firm"],
        "geography_notes": [], "unsupported_intent": [], "confidence": 50,
        "reasoning": "Carbon accounting software is a specific software niche with no exact taxonomy label; proposed the closest real software + environmental categories.",
    }
    ai_origin = {"computer software": "AI", "environmental services": "AI", "renewables & environment": "AI", "saas": "AI", "carbon accounting software": "user"}
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "carbon accounting software", "response": []},
        {"field": "naics_category", "query": "carbon accounting software", "response": []},
        {"field": "linkedin_category", "query": "Computer Software", "response": [{"query": "Computer Software", "label": "Computer Software", "value": "computer-software"}]},
        {"field": "linkedin_category", "query": "Environmental Services", "response": [{"query": "Environmental Services", "label": "Environmental Services", "value": "environmental-services"}]},
        {"field": "linkedin_category", "query": "Renewables & Environment", "response": [{"query": "Renewables & Environment", "label": "Renewables & Environment", "value": "renewables-environment"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "carbon001", "name": "Emitrack Software", "domain": "emitrack.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Computer Software"},
            {"business_id": "carbon002", "name": "GreenPath Environmental Consulting", "domain": "greenpathconsulting.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Environmental Services"},
            {"business_id": "carbon003", "name": "Meridian Solar Farms LLC", "domain": "meridiansolar.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Renewables & Environment"},
        ],
        # website_keywords: "carbon accounting software" (user) + "SaaS" (AI), neither resolved structurally
        "keyword": [
            {"business_id": "carbon004", "name": "Sustainability Metrics Advisory", "domain": "sustainmetrics.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Management Consulting"},
        ],
    }
    labels = {
        "carbon001": ("RELEVANT", "A software company; name plausibly aligns with emissions tracking — reasonable structured-tier proxy."),
        "carbon002": ("FALSE_POSITIVE", "An environmental CONSULTING firm (a services business), not a software company — 'Environmental Services' captures the wrong business model."),
        "carbon003": ("FALSE_POSITIVE", "A solar energy operator, not a software company at all — 'Renewables & Environment' is a subject-matter-adjacent but structurally unrelated category."),
        "carbon004": ("FALSE_POSITIVE", "A sustainability consulting/advisory firm, not carbon accounting SOFTWARE — matched via keyword fallback on 'carbon accounting software'/'SaaS' mentioned in its marketing copy."),
    }
    run_scenario("Niche #2 — carbon accounting software, US", icp, llm_payload, ai_origin, autocomplete_mocks, branch_responses, labels)


def print_final_report():
    print_header("GLOBAL BENCHMARK RESULTS")
    s_total = GLOBAL["structured_relevant"] + GLOBAL["structured_false_positive"] + GLOBAL["structured_ambiguous"]
    k_total = GLOBAL["keyword_relevant"] + GLOBAL["keyword_false_positive"] + GLOBAL["keyword_ambiguous"]
    print(f"\nTotal candidates across all scenarios: {GLOBAL['total_candidates']}")
    print(f"Total autocomplete API calls: {GLOBAL['total_autocomplete_calls']}")
    print(f"Total search API calls: {GLOBAL['total_search_calls']}")
    print(f"Duplicate business_ids observed: {GLOBAL['duplicate_business_ids_seen']}")

    print(f"\nSTRUCTURED tier: {s_total} labeled candidates")
    if s_total:
        print(f"  relevant={GLOBAL['structured_relevant']}  false_positive={GLOBAL['structured_false_positive']}  ambiguous={GLOBAL['structured_ambiguous']}")
        strict_precision = GLOBAL["structured_relevant"] / s_total
        lenient_precision = (GLOBAL["structured_relevant"] + GLOBAL["structured_ambiguous"]) / s_total
        print(f"  strict precision (relevant/total) = {strict_precision:.2f}")
        print(f"  lenient precision ((relevant+ambiguous)/total) = {lenient_precision:.2f}")

    print(f"\nKEYWORD-FALLBACK tier: {k_total} labeled candidates")
    if k_total:
        print(f"  relevant={GLOBAL['keyword_relevant']}  false_positive={GLOBAL['keyword_false_positive']}  ambiguous={GLOBAL['keyword_ambiguous']}")
        strict_precision = GLOBAL["keyword_relevant"] / k_total
        lenient_precision = (GLOBAL["keyword_relevant"] + GLOBAL["keyword_ambiguous"]) / k_total
        print(f"  strict precision (relevant/total) = {strict_precision:.2f}")
        print(f"  lenient precision ((relevant+ambiguous)/total) = {lenient_precision:.2f}")

    print_header("PER-TERM PRECISION (origin tracked independently by this harness, not by the running system)")
    for term, stats in sorted(PER_TERM.items(), key=lambda kv: -(kv[1]["false_positive"])):
        total = stats["relevant"] + stats["false_positive"] + stats["ambiguous"]
        if total == 0:
            continue
        precision = stats["relevant"] / total
        origin = "/".join(sorted(stats["origin"]))
        print(f"  '{term}' [{origin}]: relevant={stats['relevant']} fp={stats['false_positive']} ambiguous={stats['ambiguous']}  precision={precision:.2f}")

    print_header("FALSE POSITIVE REASONS")
    for reason in FALSE_POSITIVE_REASONS:
        print(f"  - {reason}")


if __name__ == "__main__":
    scenario_d2c()
    scenario_fmcg()
    scenario_healthcare()
    scenario_ott()
    scenario_microdrama()
    scenario_saas()
    scenario_drone_inspection()
    scenario_carbon_accounting()
    print_final_report()
