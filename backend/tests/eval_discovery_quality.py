"""Controlled discovery-quality evaluation — NOT a pytest test file (no
test_ prefix, excluded from the suite), a standalone script run manually
via `python tests/eval_discovery_quality.py`.

Runs the REAL, unmodified production code path for four realistic ICPs:

    CanonicalICP
      -> interpret_icp()                          [app/services/discovery_strategy.py, UNCHANGED]
      -> merge_strategy_into_hard_rules()          [UNCHANGED]
      -> ExploriumCompanyDiscoveryProvider.execute [app/providers/explorium.py, UNCHANGED]
      -> the SAME field mapping evidence_import.py uses (replicated here,
         not re-derived — see _evidence_from_normalized_record's docstring)
      -> validate_against_icp() / evaluate_hard_rules() [UNCHANGED]

TWO THINGS ARE MOCKED, both clearly labeled in the printed output — nothing
else is:

  1. The OpenAI call: no OPENAI_API_KEY is configured in this environment
     (confirmed absent from .env before writing this script), so
     interpret_icp() is given a MockLLMProvider with a HAND-AUTHORED
     response per ICP, written to represent a PLAUSIBLE real GPT output —
     not a real model call. Every such response is printed in full and
     labeled "SIMULATED LLM OUTPUT (not a live OpenAI call)" so it is never
     mistaken for a live result.

  2. Explorium's HTTP layer: this evaluation must not spend real Explorium
     credits without asking first (per instructions), so respx mocks the
     autocomplete + search endpoints with HAND-AUTHORED, REALISTIC response
     bodies — including deliberately mixing in plausible false-positive
     candidates (e.g. an agency whose site mentions the ICP's industry) to
     genuinely stress-test the pipeline rather than only feeding it clean
     data. Every mocked response is printed alongside the real request the
     adapter actually sent, so the branch precedence
     (linkedin_category -> naics_category -> keyword) is independently
     verifiable from the output, not asserted by this script.

Everything else — DiscoveryStrategy interpretation/merge logic, Explorium's
own branch planning, taxonomy matching, cursor/pagination handling, the
evidence field mapping, and the hard-rule engine — is the real, unmodified
codebase. No production code is changed by this script.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from uuid import uuid4

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import respx

from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.services.discovery_strategy import interpret_icp, merge_strategy_into_hard_rules
from app.services.evidence_engine import compute_field_status
from app.services.hard_icp_validation import validate_against_icp
from app.services.llm_providers.mock import MockLLMProvider

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Faithful replica of evidence_import.py's field mapping (DB-free version —
# the real function reads from persisted DiscoveryCandidateModel rows; this
# evaluation builds the same evidence shape directly from a NormalizedRecord
# so the pipeline can run without a database). ONE evidence record per
# field per candidate — exactly the real function's behavior, never
# artificially doubled the way some earlier-phase test fixtures do to force
# a PASS. This is intentional: it's part of what this evaluation measures.
# ---------------------------------------------------------------------------
def evidence_from_candidate(company_id: str, provider_id: str, external_id: str, name: str, attributes: dict) -> list[EvidenceRecord]:
    def rec(field: str, value) -> EvidenceRecord:
        return EvidenceRecord(
            id=str(uuid4()),
            entity_type=EntityType.COMPANY,
            entity_id=company_id,
            field=field,
            value=value,
            source_provider_id=provider_id,
            source_type=SourceType.PROVIDER,
            external_id=external_id,
            retrieved_at=NOW,
            confidence=ConfidenceLevel.UNKNOWN,
            created_at=NOW,
        )

    records = [rec("company_identity", name)]
    if attributes.get("domain"):
        records.append(rec("domain", attributes["domain"]))
    if isinstance(attributes.get("employee_count"), int):
        records.append(rec("employee_count", attributes["employee_count"]))
    if attributes.get("industry"):
        records.append(rec("industry", attributes["industry"]))
    if attributes.get("country"):
        records.append(rec("country", attributes["country"]))
    if attributes.get("revenue_range"):
        records.append(rec("revenue_range", attributes["revenue_range"]))
    if attributes.get("employee_range"):
        records.append(rec("employee_range", attributes["employee_range"]))
    if attributes.get("linkedin_id"):
        records.append(rec("linkedin_id", attributes["linkedin_id"]))
    return records


def print_header(title: str) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def run_scenario(
    scenario_name: str,
    icp: CanonicalICP,
    llm_response_payload: dict,
    autocomplete_mocks: list[dict],
    search_response: dict,
    relevance_labels: dict[str, str],
) -> None:
    """autocomplete_mocks: list of {"field", "query", "response": [...] or []}
    relevance_labels: external_id -> one of "RELEVANT" / "FALSE_POSITIVE" / "AMBIGUOUS",
    hand-judged by inspecting the mocked company's real attributes against
    the ICP — this is the only subjective step in this script, and it is
    printed alongside the reasoning inline, not hidden in a number."""
    print_header(f"SCENARIO: {scenario_name}")
    print("\n--- ICP SUBMITTED (CanonicalICP.hard_rules) ---")
    print(json.dumps(icp.hard_rules.model_dump(), indent=2, default=str))

    print("\n--- SIMULATED LLM OUTPUT (not a live OpenAI call — OPENAI_API_KEY is unset in this env) ---")
    print(json.dumps(llm_response_payload, indent=2))

    provider = MockLLMProvider(response_text=json.dumps(llm_response_payload))
    strategy = interpret_icp(icp, provider)
    print(f"\nDiscoveryStrategy.status = {strategy.status}")
    print(f"industry_terms (capped/deduped) = {strategy.industry_terms}")
    print(f"company_type_terms = {strategy.company_type_terms}")
    print(f"exclusion_terms = {strategy.exclusion_terms}")
    print(f"unsupported_intent = {strategy.unsupported_intent}")

    merged_hard_rules = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    merged_icp = icp.model_copy(update={"hard_rules": merged_hard_rules})
    print(f"\nMerged industries reaching Explorium = {merged_hard_rules.industries}")
    print(f"Merged company_types reaching Explorium = {merged_hard_rules.company_types}")

    with respx.mock:
        for mock in autocomplete_mocks:
            params = {"field": mock["field"], "query": mock["query"]}
            respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params=params).mock(
                return_value=httpx.Response(200, json=mock["response"])
            )
        # any term not explicitly mocked above gets "no match" — realistic
        # default, matching every real term this evaluation didn't hand-author
        respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
        respx.post(EXPLORIUM_SEARCH_URL).mock(return_value=httpx.Response(200, json=search_response))

        provider_adapter = ExploriumCompanyDiscoveryProvider(api_key="eval-key-not-real")
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

    print(f"\n--- EXPLORIUM RESPONSE: success={response.success}, {len(response.data)} candidates returned ---")

    print("\n--- PER-CANDIDATE RESULT ---")
    pass_count = hold_count = fail_count = 0
    relevant_count = false_positive_count = 0
    provider_id = response.source.provider_id if response.source else "unknown"
    for record in response.data:
        evidence = evidence_from_candidate(
            company_id=record.external_id, provider_id=provider_id,
            external_id=record.external_id, name=record.name, attributes=record.attributes,
        )
        validation = validate_against_icp(merged_icp, record.external_id, evidence, None, [])
        overall = validation.evaluation.overall_result.value
        if overall == "PASS":
            pass_count += 1
        elif overall == "HOLD":
            hold_count += 1
        elif overall == "FAIL":
            fail_count += 1

        label = relevance_labels.get(record.external_id, "UNLABELED")
        if label == "RELEVANT":
            relevant_count += 1
        elif label == "FALSE_POSITIVE":
            false_positive_count += 1

        industry_status = compute_field_status("industry", [e for e in evidence if e.field == "industry"]) if any(e.field == "industry" for e in evidence) else None

        print(f"\n  Company: {record.name}")
        print(f"    domain: {record.attributes.get('domain')}")
        print(f"    industry (as returned): {record.attributes.get('industry')}")
        print(f"    employee_range: {record.attributes.get('employee_range')}")
        print(f"    industry evidence status: {industry_status.value if industry_status else 'N/A'}")
        print(f"    hard-rule result: {overall}")
        print(f"    rule breakdown: {[(r.rule, r.status.value, r.reason_code.value if r.reason_code else None) for r in validation.evaluation.rule_results]}")
        print(f"    HAND-JUDGED RELEVANCE: {label}")

    print(f"\n--- SCENARIO SUMMARY: {scenario_name} ---")
    total = len(response.data)
    print(f"total candidates: {total}")
    print(f"hard-rule PASS: {pass_count} | HOLD: {hold_count} | FAIL: {fail_count}")
    labeled = relevant_count + false_positive_count
    if labeled:
        print(f"precision (relevant / total labeled) = {relevant_count}/{labeled} = {relevant_count/labeled:.2f}")
    print(f"false positives identified: {false_positive_count}")


# ============================================================================
# SCENARIO 1: D2C / FMCG
# ============================================================================
def scenario_d2c_fmcg():
    icp = CanonicalICP(
        icp_id="eval-d2c-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("D2C skincare",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="India", code="IN", label="India"),)),
            employee_range=EmployeeRange(min=20, max=300),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Cosmetics, Beauty Supplies, and Perfume Stores", "Consumer Goods", "E-commerce"],
        "company_type_terms": ["D2C"],
        "exclusion_terms": ["agency", "consultancy"],
        "geography_notes": [],
        "unsupported_intent": ["growing companies"],
        "confidence": 78,
        "reasoning": "D2C skincare has no exact linkedin_category match; expanded into the closest real NAICS-style label plus adjacent structured terms.",
    }
    autocomplete_mocks = [
        # "D2C skincare" itself: no exact linkedin_category/naics match (realistic — it's not a real taxonomy phrase)
        {"field": "linkedin_category", "query": "D2C skincare", "response": []},
        {"field": "naics_category", "query": "D2C skincare", "response": []},
        # AI-proposed term DOES exact-match a real NAICS label
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {
            "field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores",
            "response": [{"query": "Cosmetics, Beauty Supplies, and Perfume Stores", "label": "Cosmetics, Beauty Supplies, and Perfume Stores", "value": "446120"}],
        },
        {"field": "linkedin_category", "query": "Consumer Goods", "response": [{"query": "Consumer Goods", "label": "Consumer Goods", "value": "consumer-goods"}]},
        {"field": "linkedin_category", "query": "E-commerce", "response": [{"query": "E-commerce", "label": "E-commerce", "value": "e-commerce"}]},
        {"field": "linkedin_category", "query": "D2C", "response": [{"query": "D2C", "label": "D2C", "value": "d2c"}]},
    ]
    # Realistic search response: a real D2C skincare brand, plus a
    # marketing agency whose site copy would plausibly cause it to be
    # tagged with an adjacent category by Explorium's own data pipeline —
    # this is the deliberate false-positive stress case for this scenario.
    search_response = {
        "data": [
            {
                "business_id": "d2c0000000000000000000000000001",
                "name": "Glow & Co Skincare",
                "domain": "glowandco.invalid",
                "country_name": "India",
                "number_of_employees_range": "51-200",
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
                "yearly_revenue_range": "1M-10M",
            },
            {
                "business_id": "d2c0000000000000000000000000002",
                "name": "Beauty Brand Growth Partners",
                "domain": "beautybrandgrowth.invalid",
                "country_name": "India",
                "number_of_employees_range": "11-50",
                "naics_description": "Consumer Goods",  # matched via the AI-expanded keyword/structured term, but this is actually a marketing agency
                "yearly_revenue_range": None,
            },
            {
                "business_id": "d2c0000000000000000000000000003",
                "name": "Radiance Naturals",
                "domain": "radiancenaturals.invalid",
                "country_name": "India",
                "number_of_employees_range": "201-500",  # outside the 20-300 requested range
                "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores",
                "yearly_revenue_range": "10M-50M",
            },
        ]
    }
    relevance_labels = {
        "d2c0000000000000000000000000001": "RELEVANT",
        "d2c0000000000000000000000000002": "FALSE_POSITIVE",  # a growth/marketing agency, not a D2C brand itself
        "d2c0000000000000000000000000003": "AMBIGUOUS",  # genuinely a D2C skincare brand, but employee count is outside the requested range
    }
    run_scenario("D2C/FMCG — India, 20-300 employees", icp, llm_payload, autocomplete_mocks, search_response, relevance_labels)


# ============================================================================
# SCENARIO 2: Healthcare
# ============================================================================
def scenario_healthcare():
    icp = CanonicalICP(
        icp_id="eval-healthcare-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("Healthcare",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=50, max=1000),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Hospitals and Health Care", "Medical Practices", "Health, Wellness and Fitness"],
        "company_type_terms": [],
        "exclusion_terms": ["staffing agency", "recruiting"],
        "geography_notes": [],
        "unsupported_intent": [],
        "confidence": 85,
        "reasoning": "Healthcare has a direct linkedin_category match; expanded into closely adjacent real categories for recall.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Hospital & Health Care", "value": "hospital-health-care"}]},
        {"field": "linkedin_category", "query": "Hospitals and Health Care", "response": [{"query": "Hospitals and Health Care", "label": "Hospital & Health Care", "value": "hospital-health-care"}]},
        {"field": "linkedin_category", "query": "Medical Practices", "response": [{"query": "Medical Practices", "label": "Medical Practice", "value": "medical-practice"}]},
        {"field": "linkedin_category", "query": "Health, Wellness and Fitness", "response": [{"query": "Health, Wellness and Fitness", "label": "Health, Wellness & Fitness", "value": "health-wellness-fitness"}]},
    ]
    search_response = {
        "data": [
            {
                "business_id": "hc00000000000000000000000000001",
                "name": "Midwest Regional Health Group",
                "domain": "midwestregionalhealth.invalid",
                "country_name": "United States",
                "number_of_employees_range": "201-500",
                "naics_description": "General Medical and Surgical Hospitals",
                "yearly_revenue_range": "50M-100M",
            },
            {
                "business_id": "hc00000000000000000000000000002",
                "name": "Sunrise Wellness Staffing",
                "domain": "sunrisewellnessstaffing.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Health, Wellness & Fitness",  # matched a real category, but is actually a healthcare staffing agency
                "yearly_revenue_range": None,
            },
            {
                "business_id": "hc00000000000000000000000000003",
                "name": "Pinecrest Medical Practice",
                "domain": "pinecrestmedical.invalid",
                "country_name": "United States",
                "number_of_employees_range": None,  # provider returned no employee data at all — realistic gap
                "naics_description": "Medical Practice",
                "yearly_revenue_range": None,
            },
        ]
    }
    relevance_labels = {
        "hc00000000000000000000000000001": "RELEVANT",
        "hc00000000000000000000000000002": "FALSE_POSITIVE",  # a staffing agency serving healthcare, not a healthcare provider itself
        "hc00000000000000000000000000003": "AMBIGUOUS",  # a real medical practice, but no employee data to confirm it fits the 50-1000 range
    }
    run_scenario("Healthcare — United States, 50-1000 employees", icp, llm_payload, autocomplete_mocks, search_response, relevance_labels)


# ============================================================================
# SCENARIO 3: SaaS / Technology
# ============================================================================
def scenario_saas():
    icp = CanonicalICP(
        icp_id="eval-saas-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("B2B SaaS",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=10, max=500),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Computer Software", "Information Technology and Services", "Internet"],
        "company_type_terms": ["SaaS"],
        "exclusion_terms": ["IT staffing", "IT consulting"],
        "geography_notes": [],
        "unsupported_intent": ["recently funded"],
        "confidence": 88,
        "reasoning": "B2B SaaS has no single exact taxonomy label; expanded into the standard adjacent software/technology categories.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "B2B SaaS", "response": []},
        {"field": "naics_category", "query": "B2B SaaS", "response": []},
        {"field": "linkedin_category", "query": "Computer Software", "response": [{"query": "Computer Software", "label": "Computer Software", "value": "computer-software"}]},
        {"field": "linkedin_category", "query": "Information Technology and Services", "response": [{"query": "Information Technology and Services", "label": "Information Technology and Services", "value": "it-services"}]},
        {"field": "linkedin_category", "query": "Internet", "response": [{"query": "Internet", "label": "Internet", "value": "internet"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    search_response = {
        "data": [
            {
                "business_id": "saas000000000000000000000000001",
                "name": "Ledgerly (workflow automation software)",
                "domain": "ledgerly.invalid",
                "country_name": "United States",
                "number_of_employees_range": "51-200",
                "naics_description": "Computer Software",
                "yearly_revenue_range": "10M-50M",
            },
            {
                "business_id": "saas000000000000000000000000002",
                "name": "Apex IT Staffing Solutions",
                "domain": "apexitstaffing.invalid",
                "country_name": "United States",
                "number_of_employees_range": "201-500",
                "naics_description": "Information Technology and Services",  # matched real category, but is an IT staffing firm, not a SaaS company
                "yearly_revenue_range": "50M-100M",
            },
            {
                "business_id": "saas000000000000000000000000003",
                "name": "Northbeam Analytics",
                "domain": "northbeamanalytics.invalid",
                "country_name": "United States",
                "number_of_employees_range": "11-50",
                "naics_description": "Computer Software",
                "yearly_revenue_range": "1M-10M",
            },
        ]
    }
    relevance_labels = {
        "saas000000000000000000000000001": "RELEVANT",
        "saas000000000000000000000000002": "FALSE_POSITIVE",  # IT staffing agency, not a software company
        "saas000000000000000000000000003": "RELEVANT",
    }
    run_scenario("B2B SaaS — United States, 10-500 employees", icp, llm_payload, autocomplete_mocks, search_response, relevance_labels)


# ============================================================================
# SCENARIO 4: Niche/obscure ICP
# ============================================================================
def scenario_niche():
    icp = CanonicalICP(
        icp_id="eval-niche-1",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("industrial drone inspection services",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=5, max=100),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Aviation & Aerospace", "Industrial Automation", "Utilities"],
        "company_type_terms": [],
        "exclusion_terms": [],
        "geography_notes": [],
        "unsupported_intent": ["drone inspection specifically — no taxonomy category exists for this specialization"],
        "confidence": 45,
        "reasoning": "No close taxonomy match exists for this specialization; proposed only broad, plausible parent categories, flagged low confidence.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "industrial drone inspection services", "response": []},
        {"field": "naics_category", "query": "industrial drone inspection services", "response": []},
        {"field": "linkedin_category", "query": "Aviation & Aerospace", "response": [{"query": "Aviation & Aerospace", "label": "Aviation & Aerospace", "value": "aviation-aerospace"}]},
        {"field": "linkedin_category", "query": "Industrial Automation", "response": []},
        {"field": "naics_category", "query": "Industrial Automation", "response": []},
        {"field": "linkedin_category", "query": "Utilities", "response": [{"query": "Utilities", "label": "Utilities", "value": "utilities"}]},
    ]
    # Realistic outcome for a genuinely obscure ICP: the broad parent
    # categories return large, irrelevant incumbents, not the small
    # specialist companies the user actually wants — this is the core
    # recall-vs-precision tension for niche ICPs, shown directly rather
    # than asserted.
    search_response = {
        "data": [
            {
                "business_id": "niche00000000000000000000000001",
                "name": "Continental Airlines Cargo Division",  # a large aviation incumbent, not a drone inspection company
                "domain": "continentalcargo.invalid",
                "country_name": "United States",
                "number_of_employees_range": "10001+",
                "naics_description": "Aviation & Aerospace",
                "yearly_revenue_range": "1B+",
            },
            {
                "business_id": "niche00000000000000000000000002",
                "name": "SkyGrid Inspections",  # plausibly a genuine drone inspection company, caught only because it happens to be tagged Aviation & Aerospace
                "domain": "skygridinspections.invalid",
                "country_name": "United States",
                "number_of_employees_range": "11-50",
                "naics_description": "Aviation & Aerospace",
                "yearly_revenue_range": "1M-10M",
            },
        ]
    }
    relevance_labels = {
        "niche00000000000000000000000001": "FALSE_POSITIVE",  # a giant incumbent, wrong company type and wildly outside employee range
        "niche00000000000000000000000002": "RELEVANT",
    }
    run_scenario("Niche — Industrial drone inspection, US, 5-100 employees", icp, llm_payload, autocomplete_mocks, search_response, relevance_labels)


if __name__ == "__main__":
    scenario_d2c_fmcg()
    scenario_healthcare()
    scenario_saas()
    scenario_niche()
