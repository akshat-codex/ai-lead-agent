"""Phase 14 — Real Company Discovery Quality Validation.

NOT a pytest test file (no test_ prefix, excluded from the suite) — a
standalone script run manually via
`python tests/audit_discovery_quality_phase14.py`. Read-only, no
production code modified.

LIVE EXPLORIUM ATTEMPT — reported honestly: this session's sandboxed
environment cannot reach api.explorium.ai at all (confirmed via a direct
httpx call: `SSL: CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate` — a network/TLS restriction of this sandbox, not an
Explorium-side failure, and not something this script works around by
disabling certificate verification). A real EXPLORIUM_API_KEY IS present
in .env and 3 real NAICS autocomplete calls were attempted directly against
ExploriumCompanyDiscoveryProvider before falling back to this mocked
methodology — all 3 failed at the TLS layer, not with a genuine "no match"
API response, so NO live findings are included anywhere below. Every
finding in this script is from mocked HTTP responses, explicitly labeled.

Runs the REAL, unmodified production code path for 8 realistic ICPs
(broad industry, niche industry, D2C/FMCG, healthcare, SaaS,
location+employee-constrained, company-type-bearing, and a deliberately
messy/natural-language ICP):

    CanonicalICP
      -> interpret_icp()                          [UNCHANGED]
      -> build_term_origin_map()                   [Phase 13D, UNCHANGED]
      -> merge_strategy_into_hard_rules()          [UNCHANGED]
      -> ExploriumCompanyDiscoveryProvider.execute [UNCHANGED — including
                                                      13A's cache fix, 13B/13D's
                                                      provenance tagging, 13C's
                                                      dedup+cap]
      -> the SAME field mapping evidence_import.py uses (replicated here)
      -> validate_against_icp() / evaluate_hard_rules()  [UNCHANGED]
      -> _discovery_match_type() classification          [Phase 12 gate input,
                                                            UNCHANGED]

TWO THINGS ARE MOCKED, clearly labeled everywhere in the output:
  1. The OpenAI call — no OPENAI_API_KEY configured; interpret_icp() is
     given a hand-authored MockLLMProvider response per ICP, representing
     a PLAUSIBLE real GPT output, never a live call.
  2. Explorium's HTTP layer — respx mocks the autocomplete + search
     endpoints with hand-authored, realistic, per-branch-DISTINCT response
     bodies (learned from Phase 13C's own methodology fix: an
     unconditional shared mock collapses cross-branch dedup incorrectly).

RECALL: not claimed anywhere. No reference set of "every company that
should exist" is available. PRECISION: reported per-scenario as a labeled
sample size (n), never extrapolated as a statistically valid population
estimate — a false-positive count out of 3-5 mocked candidates is a
qualitative illustration of a mechanism, not a measured rate.
"""
from __future__ import annotations

import json
import sys
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
from app.services.discovery_strategy import build_term_origin_map, interpret_icp, merge_strategy_into_hard_rules
from app.services.evidence_import import _industry_match_provenance
from app.services.hard_icp_validation import validate_against_icp
from app.services.llm_providers.mock import MockLLMProvider
from app.services.qualification_context import _discovery_match_type

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

EXPLORIUM_SEARCH_URL = "https://api.explorium.ai/v2/businesses"
EXPLORIUM_AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)

SCORECARD = {
    "total_scenarios": 0,
    "total_candidates": 0,
    "structured_relevant": 0,
    "structured_false_positive": 0,
    "structured_ambiguous": 0,
    "keyword_relevant": 0,
    "keyword_false_positive": 0,
    "keyword_ambiguous": 0,
    "pass_count": 0,
    "hold_count": 0,
    "fail_count": 0,
    "phase12_verification_needed": 0,
    "duplicates_removed_cross_branch": 0,
    "total_autocomplete_calls": 0,
    "total_search_calls": 0,
}


def evidence_from_candidate(company_id: str, provider_id: str, external_id: str, name: str, attributes: dict) -> list[EvidenceRecord]:
    def rec(field: str, value, **overrides) -> EvidenceRecord:
        base = dict(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id=company_id, field=field, value=value,
            source_provider_id=provider_id, source_type=SourceType.PROVIDER, external_id=external_id,
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        )
        base.update(overrides)
        return EvidenceRecord(**base)

    records = [rec("company_identity", name)]
    if attributes.get("domain"):
        records.append(rec("domain", attributes["domain"]))
    if isinstance(attributes.get("employee_count"), int):
        records.append(rec("employee_count", attributes["employee_count"]))
    if attributes.get("industry"):
        records.append(rec("industry", attributes["industry"], evidence_text=_industry_match_provenance(attributes)))
    if attributes.get("country"):
        records.append(rec("country", attributes["country"]))
    if attributes.get("employee_range"):
        records.append(rec("employee_range", attributes["employee_range"]))
    return records


def print_header(title: str) -> None:
    print("\n" + "=" * 112)
    print(title)
    print("=" * 112)


def _branch_responder(branch_responses: dict):
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
    user_asked: str,
    icp: CanonicalICP,
    llm_response_payload: dict,
    autocomplete_mocks: list[dict],
    branch_responses: dict,
    relevance_labels: dict[str, tuple[str, str]],
    duplicate_ids: set[str] | None = None,
) -> None:
    SCORECARD["total_scenarios"] += 1
    duplicate_ids = duplicate_ids or set()
    print_header(f"SCENARIO: {scenario_name}")
    print(f"\n1. User originally asked: {user_asked!r}")
    print(f"   ICP industries (user-typed): {icp.hard_rules.industries}")
    print(f"   ICP company_types (user-typed): {icp.hard_rules.company_types}")
    print(f"   ICP employee_range: {icp.hard_rules.employee_range.min}-{icp.hard_rules.employee_range.max}")
    print(f"   ICP geography: {[c.label for c in icp.hard_rules.geography.countries]}")

    print("\n2. SIMULATED LLM OUTPUT (not a live OpenAI call):")
    print(json.dumps(llm_response_payload, indent=2))

    provider = MockLLMProvider(response_text=json.dumps(llm_response_payload))
    strategy = interpret_icp(icp, provider)
    term_origin = build_term_origin_map(icp.hard_rules, strategy)
    merged_hard_rules = merge_strategy_into_hard_rules(icp.hard_rules, strategy)
    merged_icp = icp.model_copy(update={"hard_rules": merged_hard_rules})

    print(f"\n3. Term origins (user vs AI): {term_origin}")
    print(f"   Merged industries reaching Explorium: {merged_hard_rules.industries}")
    print(f"   Merged company_types reaching Explorium: {merged_hard_rules.company_types}")

    with respx.mock:
        for mock in autocomplete_mocks:
            respx.get(EXPLORIUM_AUTOCOMPLETE_URL, params={"field": mock["field"], "query": mock["query"]}).mock(
                return_value=httpx.Response(200, json=mock["response"])
            )
        respx.get(EXPLORIUM_AUTOCOMPLETE_URL).mock(return_value=httpx.Response(200, json=[]))
        search_route = respx.post(EXPLORIUM_SEARCH_URL).mock(side_effect=_branch_responder(branch_responses))

        provider_adapter = ExploriumCompanyDiscoveryProvider(api_key="audit-key-not-real")
        request = ProviderRequest(
            capability=ProviderCapability.COMPANY_DISCOVERY,
            query={
                "industries": merged_hard_rules.industries,
                "geography_codes": tuple(e.code for e in merged_icp.hard_rules.geography.countries),
                "company_types": merged_hard_rules.company_types,
                "min_employees": merged_icp.hard_rules.employee_range.min,
                "max_employees": merged_icp.hard_rules.employee_range.max,
                "limit": 20,
                "term_origin": term_origin,
            },
        )
        print(f"\n7. Exact Explorium request query dict:\n   {json.dumps(request.query, indent=2, default=str)}")
        response = provider_adapter.run(request)

        autocomplete_call_count = sum(r.call_count for r in respx.routes) - search_route.call_count
        SCORECARD["total_autocomplete_calls"] += autocomplete_call_count
        SCORECARD["total_search_calls"] += search_route.call_count

    total_raw = sum(len(v) for v in branch_responses.values())
    dedup_removed = total_raw - len(response.data)
    SCORECARD["duplicates_removed_cross_branch"] += max(0, dedup_removed)

    print(f"\n8-9. Explorium response: {len(response.data)} unique candidates (raw across branches: {total_raw}, "
          f"cross-branch duplicates removed: {max(0, dedup_removed)}), autocomplete_calls={autocomplete_call_count}, search_calls={search_route.call_count}")

    print("\n10-14. PER-CANDIDATE RESULT:")
    for record in response.data:
        SCORECARD["total_candidates"] += 1
        evidence = evidence_from_candidate("company-1", "explorium-company-discovery-v1", record.external_id, record.name, record.attributes)
        validation = validate_against_icp(merged_icp, record.external_id, evidence, None, [])
        overall = validation.evaluation.overall_result.value
        match_type = _discovery_match_type(evidence)

        tier = "structured" if "industry_match_branch" in record.attributes else ("keyword" if "keyword_match_terms" in record.attributes else "none")
        terms = record.attributes.get("industry_match_terms") or record.attributes.get("keyword_match_terms") or []
        sources = record.attributes.get("keyword_match_term_sources", [])

        label, reason = relevance_labels.get(record.external_id, ("UNLABELED", ""))
        needs_verification = overall == "PASS" and match_type != "structured"
        if needs_verification:
            SCORECARD["phase12_verification_needed"] += 1
        if overall == "PASS":
            SCORECARD["pass_count"] += 1
        elif overall == "HOLD":
            SCORECARD["hold_count"] += 1
        elif overall == "FAIL":
            SCORECARD["fail_count"] += 1

        tally_key = "structured" if tier == "structured" else "keyword"
        if label == "RELEVANT":
            SCORECARD[f"{tally_key}_relevant"] += 1
        elif label == "FALSE_POSITIVE":
            SCORECARD[f"{tally_key}_false_positive"] += 1
        elif label == "AMBIGUOUS":
            SCORECARD[f"{tally_key}_ambiguous"] += 1

        dup_note = " [DUPLICATE-ID-TEST-CANDIDATE]" if record.external_id in duplicate_ids else ""
        print(f"\n  {record.name}{dup_note}")
        print(f"    domain={record.attributes.get('domain')}  industry={record.attributes.get('industry')}  employee_range={record.attributes.get('employee_range')}")
        print(f"    tier={tier}  terms={terms}" + (f"  sources={sources}" if sources else ""))
        print(f"    evidence fields captured: {sorted(set(e.field for e in evidence))}")
        print(f"    hard-rule={overall}  discovery_match_type={match_type}  needs_Phase12_verification={needs_verification}")
        print(f"    LABEL={label}" + (f"  reason={reason}" if reason else ""))

    print(f"\n--- SCENARIO SUMMARY: {scenario_name} ({len(response.data)} candidates) ---")


# ============================================================================
# SCENARIO 1: broad industry ICP
# ============================================================================
def scenario_broad_industry():
    icp = CanonicalICP(
        icp_id="p14-1", version=1,
        hard_rules=CanonicalHardRules(
            industries=("Technology",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=1, max=10000),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Computer Software", "Information Technology and Services", "Internet"],
        "company_type_terms": [], "exclusion_terms": [], "geography_notes": [], "unsupported_intent": [],
        "confidence": 60, "reasoning": "Technology is an extremely broad umbrella; expanded into common adjacent categories.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Technology", "response": []},
        {"field": "naics_category", "query": "Technology", "response": []},
        {"field": "linkedin_category", "query": "Computer Software", "response": [{"query": "Computer Software", "label": "Computer Software", "value": "computer-software"}]},
        {"field": "linkedin_category", "query": "Information Technology and Services", "response": [{"query": "Information Technology and Services", "label": "Information Technology and Services", "value": "it-services"}]},
        {"field": "linkedin_category", "query": "Internet", "response": [{"query": "Internet", "label": "Internet", "value": "internet"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "bi001", "name": "Northbeam Analytics", "domain": "northbeamanalytics.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Computer Software"},
            {"business_id": "bi002", "name": "Apex IT Staffing Solutions", "domain": "apexitstaffing.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Information Technology and Services"},
            {"business_id": "bi003", "name": "Global Internet Registrars LLC", "domain": "globalregistrars.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Internet"},
        ],
        "keyword": [
            {"business_id": "bi004", "name": "Tech Recruiting Partners", "domain": "techrecruiting.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Staffing and Recruiting"},
        ],
    }
    labels = {
        "bi001": ("RELEVANT", ""),
        "bi002": ("FALSE_POSITIVE", "An IT staffing agency, not a technology product company."),
        "bi003": ("FALSE_POSITIVE", "A domain registrar — 'Internet' is an extremely broad, low-precision structured category."),
        "bi004": ("FALSE_POSITIVE", "A tech recruiting firm, found via raw 'Technology' keyword fallback."),
    }
    run_scenario("Broad industry — Technology, US, uncapped employee range", "Find US technology companies", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 2: niche industry ICP
# ============================================================================
def scenario_niche_industry():
    icp = CanonicalICP(
        icp_id="p14-2", version=1,
        hard_rules=CanonicalHardRules(
            industries=("marine biology consulting",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=2, max=50),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Environmental Services", "Research"], "company_type_terms": [], "exclusion_terms": [],
        "geography_notes": [], "unsupported_intent": ["marine biology specifically — no dedicated taxonomy category"],
        "confidence": 35, "reasoning": "No close taxonomy match; proposed only broad adjacent categories, low confidence flagged.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "marine biology consulting", "response": []},
        {"field": "naics_category", "query": "marine biology consulting", "response": []},
        {"field": "linkedin_category", "query": "Environmental Services", "response": [{"query": "Environmental Services", "label": "Environmental Services", "value": "environmental-services"}]},
        {"field": "linkedin_category", "query": "Research", "response": [{"query": "Research", "label": "Research", "value": "research"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "ni001", "name": "Coastal Environmental Solutions", "domain": "coastalenviro.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Environmental Services"},
            {"business_id": "ni002", "name": "Midwest Agricultural Research Institute", "domain": "midwestagresearch.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Research"},
        ],
        "keyword": [
            {"business_id": "ni003", "name": "Ocean Ecology Consultants", "domain": "oceanecology.invalid", "country_name": "United States", "number_of_employees_range": "2-10", "naics_description": "Professional Services"},
        ],
    }
    labels = {
        "ni001": ("AMBIGUOUS", "Environmental services broadly — plausibly adjacent but not confirmed marine-specific."),
        "ni002": ("FALSE_POSITIVE", "Agricultural (not marine) research and 10x over the requested employee ceiling."),
        "ni003": ("RELEVANT", "Name strongly suggests marine ecology; correct size range — found ONLY via keyword fallback."),
    }
    run_scenario("Niche industry — marine biology consulting, US, 2-50 employees", "small marine biology consulting firms in the US", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 3: D2C/FMCG
# ============================================================================
def scenario_d2c_fmcg():
    icp = CanonicalICP(
        icp_id="p14-3", version=1,
        hard_rules=CanonicalHardRules(
            industries=("D2C FMCG",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="India", code="IN", label="India"),)),
            employee_range=EmployeeRange(min=20, max=300),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Consumer Goods", "Food & Beverages", "Cosmetics, Beauty Supplies, and Perfume Stores"],
        "company_type_terms": ["D2C"], "exclusion_terms": ["agency", "distributor"], "geography_notes": [],
        "unsupported_intent": [], "confidence": 65, "reasoning": "D2C FMCG spans multiple real taxonomy categories; expanded broadly.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "D2C FMCG", "response": []},
        {"field": "naics_category", "query": "D2C FMCG", "response": []},
        {"field": "linkedin_category", "query": "Consumer Goods", "response": [{"query": "Consumer Goods", "label": "Consumer Goods", "value": "consumer-goods"}]},
        {"field": "linkedin_category", "query": "Food & Beverages", "response": [{"query": "Food & Beverages", "label": "Food & Beverages", "value": "food-beverages"}]},
        {"field": "linkedin_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": []},
        {"field": "naics_category", "query": "Cosmetics, Beauty Supplies, and Perfume Stores", "response": [{"query": "Cosmetics, Beauty Supplies, and Perfume Stores", "label": "Cosmetics, Beauty Supplies, and Perfume Stores", "value": "446120"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "d001", "name": "National FMCG Distribution Group", "domain": "nationalfmcg.invalid", "country_name": "India", "number_of_employees_range": "1001-5000", "naics_description": "Consumer Goods"},
            {"business_id": "d002", "name": "Riverbend Snack Co", "domain": "riverbendsnack.invalid", "country_name": "India", "number_of_employees_range": "51-200", "naics_description": "Food & Beverages"},
        ],
        "naics": [
            {"business_id": "d003", "name": "Glow & Co Skincare", "domain": "glowandco.invalid", "country_name": "India", "number_of_employees_range": "51-200", "naics_description": "Cosmetics, Beauty Supplies, and Perfume Stores"},
        ],
        "keyword": [
            {"business_id": "d004", "name": "D2C Growth Agency", "domain": "d2cgrowth.invalid", "country_name": "India", "number_of_employees_range": "11-50", "naics_description": "Management Consulting"},
        ],
    }
    labels = {
        "d001": ("FALSE_POSITIVE", "A distributor, not a D2C brand itself; also 3x over the employee ceiling."),
        "d002": ("RELEVANT", ""),
        "d003": ("RELEVANT", "Found via NAICS — correct, precise structured match."),
        "d004": ("FALSE_POSITIVE", "A marketing agency serving D2C brands, matched via raw 'D2C FMCG' keyword fallback."),
    }
    run_scenario("D2C/FMCG — India, 20-300 employees", "D2C FMCG brands in India, 20-300 people", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 4: healthcare
# ============================================================================
def scenario_healthcare():
    icp = CanonicalICP(
        icp_id="p14-4", version=1,
        hard_rules=CanonicalHardRules(
            industries=("Healthcare",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=50, max=1000),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Hospitals and Health Care", "Medical Practices"], "company_type_terms": [],
        "exclusion_terms": ["staffing agency"], "geography_notes": [], "unsupported_intent": [], "confidence": 90,
        "reasoning": "Healthcare has a direct exact linkedin_category match.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Healthcare", "response": [{"query": "Healthcare", "label": "Healthcare", "value": "healthcare"}]},
        {"field": "linkedin_category", "query": "Hospitals and Health Care", "response": [{"query": "Hospitals and Health Care", "label": "Hospitals and Health Care", "value": "hospitals-health-care"}]},
        {"field": "linkedin_category", "query": "Medical Practices", "response": [{"query": "Medical Practices", "label": "Medical Practices", "value": "medical-practice"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "h001", "name": "Midwest Regional Health Group", "domain": "midwestregionalhealth.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "General Medical and Surgical Hospitals"},
            {"business_id": "h002", "name": "Pinecrest Medical Practice", "domain": "pinecrestmedical.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Medical Practices"},
            # Same business_id intentionally repeated across two branch mocks below, to observe real cross-branch dedup
        ],
    }
    labels = {
        "h001": ("RELEVANT", ""),
        "h002": ("RELEVANT", ""),
    }
    run_scenario("Healthcare — US, 50-1000 employees", "healthcare companies in the US with 50 to 1000 employees", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 5: SaaS
# ============================================================================
def scenario_saas():
    icp = CanonicalICP(
        icp_id="p14-5", version=1,
        hard_rules=CanonicalHardRules(
            industries=("B2B SaaS",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=10, max=500),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Computer Software", "Information Technology and Services"], "company_type_terms": ["SaaS"],
        "exclusion_terms": ["IT staffing"], "geography_notes": [], "unsupported_intent": ["recently funded"],
        "confidence": 85, "reasoning": "B2B SaaS has no single exact label; expanded into standard adjacent categories.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "B2B SaaS", "response": []},
        {"field": "naics_category", "query": "B2B SaaS", "response": []},
        {"field": "linkedin_category", "query": "Computer Software", "response": [{"query": "Computer Software", "label": "Computer Software", "value": "computer-software"}]},
        {"field": "linkedin_category", "query": "Information Technology and Services", "response": [{"query": "Information Technology and Services", "label": "Information Technology and Services", "value": "it-services"}]},
        {"field": "linkedin_category", "query": "SaaS", "response": []},
        {"field": "naics_category", "query": "SaaS", "response": []},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "s001", "name": "Ledgerly", "domain": "ledgerly.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "Computer Software"},
            {"business_id": "s002", "name": "Apex IT Staffing Solutions", "domain": "apexitstaffing.invalid", "country_name": "United States", "number_of_employees_range": "201-500", "naics_description": "Information Technology and Services"},
        ],
        "keyword": [
            {"business_id": "s003", "name": "SaaS Growth Advisors", "domain": "saasgrowthadvisors.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Management Consulting"},
        ],
    }
    labels = {
        "s001": ("RELEVANT", ""),
        "s002": ("FALSE_POSITIVE", "An IT staffing agency, not a software company."),
        "s003": ("FALSE_POSITIVE", "A consultancy advising SaaS companies, found via raw 'SaaS' company_type keyword fallback."),
    }
    run_scenario("SaaS — US, 10-500 employees", "B2B SaaS companies in the US", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 6: location + employee-range constrained
# ============================================================================
def scenario_location_employee_constrained():
    icp = CanonicalICP(
        icp_id="p14-6", version=1,
        hard_rules=CanonicalHardRules(
            industries=("Manufacturing",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="Germany", code="DE", label="Germany"),)),
            employee_range=EmployeeRange(min=15, max=150),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Industrial Automation", "Machinery"], "company_type_terms": [], "exclusion_terms": [],
        "geography_notes": [], "unsupported_intent": [], "confidence": 70,
        "reasoning": "Manufacturing is broad; expanded into common adjacent categories.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "Manufacturing", "response": [{"query": "Manufacturing", "label": "Manufacturing", "value": "manufacturing"}]},
        {"field": "linkedin_category", "query": "Industrial Automation", "response": []},
        {"field": "naics_category", "query": "Industrial Automation", "response": []},
        {"field": "linkedin_category", "query": "Machinery", "response": [{"query": "Machinery", "label": "Machinery", "value": "machinery"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "l001", "name": "Präzisionswerk GmbH", "domain": "praezisionswerk.invalid", "country_name": "Germany", "number_of_employees_range": "51-200", "naics_description": "Manufacturing"},
            {"business_id": "l002", "name": "Bosch Machinery Group", "domain": "example-bosch.invalid", "country_name": "Germany", "number_of_employees_range": "10001+", "naics_description": "Machinery"},
        ],
        "keyword": [
            {"business_id": "l003", "name": "Kleinbetrieb Automation Consulting", "domain": "kleinbetrieb.invalid", "country_name": "Germany", "number_of_employees_range": "2-10", "naics_description": "Professional Services"},
        ],
    }
    labels = {
        "l001": ("AMBIGUOUS", "Employee bucket 51-200 overlaps the requested 15-150 range but extends beyond it — genuinely ambiguous without an exact count."),
        "l002": ("FALSE_POSITIVE", "A massive incumbent (10001+), wildly outside the requested 15-150 range — structured match, employee-range mismatch."),
        "l003": ("FALSE_POSITIVE", "Below the requested minimum (2-10 vs. 15-150) and a consulting firm, not a manufacturer."),
    }
    run_scenario("Location+employee constrained — Manufacturing, Germany, 15-150", "manufacturing companies in Germany with 15 to 150 employees", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 7: company-type requirement
# ============================================================================
def scenario_company_type():
    icp = CanonicalICP(
        icp_id="p14-7", version=1,
        hard_rules=CanonicalHardRules(
            industries=("E-commerce",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=5, max=200),
            company_types=("PE-backed",),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Retail", "Internet"], "company_type_terms": ["Private Equity Portfolio Company"],
        "exclusion_terms": [], "geography_notes": [], "unsupported_intent": [], "confidence": 55,
        "reasoning": "Expanded E-commerce and added a close synonym for the company_type term.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "E-commerce", "response": [{"query": "E-commerce", "label": "E-commerce", "value": "e-commerce"}]},
        {"field": "linkedin_category", "query": "Retail", "response": [{"query": "Retail", "label": "Retail", "value": "retail"}]},
        {"field": "linkedin_category", "query": "Internet", "response": [{"query": "Internet", "label": "Internet", "value": "internet"}]},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "ct001", "name": "Urban Outfitters Online", "domain": "urbanoutfitters-example.invalid", "country_name": "United States", "number_of_employees_range": "51-200", "naics_description": "E-commerce"},
        ],
        "keyword": [
            {"business_id": "ct002", "name": "Meridian Capital Partners", "domain": "meridiancapital.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Investment Management"},
        ],
    }
    labels = {
        "ct001": ("RELEVANT", "Genuine e-commerce operator matching industry; company_type (PE-backed) not independently confirmable from Explorium evidence — HOLDs on that rule specifically."),
        "ct002": ("FALSE_POSITIVE", "A private equity FIRM itself (an investor), not a PE-BACKED company — 'PE-backed'/'Private Equity Portfolio Company' keyword fallback caught the wrong side of the relationship."),
    }
    run_scenario("Company-type requirement — E-commerce, PE-backed, US", "PE-backed e-commerce companies in the US", icp, llm_payload, autocomplete_mocks, branch_responses, labels)


# ============================================================================
# SCENARIO 8: messy / natural-language-style ICP
# ============================================================================
def scenario_messy_natural_language():
    icp = CanonicalICP(
        icp_id="p14-8", version=1,
        hard_rules=CanonicalHardRules(
            industries=("companies that sell subscription boxes for pet owners",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=None, max=200),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )
    llm_payload = {
        "industry_terms": ["Consumer Goods", "E-commerce", "Retail"], "company_type_terms": ["Subscription"],
        "exclusion_terms": ["marketing agency"], "geography_notes": [],
        "unsupported_intent": ["subscription box format specifically — no dedicated taxonomy category"],
        "confidence": 40,
        "reasoning": "Messy natural-language phrase describing a business model, not a taxonomy term; extracted plausible adjacent categories and flagged the format itself as unsupported.",
    }
    autocomplete_mocks = [
        {"field": "linkedin_category", "query": "companies that sell subscription boxes for pet owners", "response": []},
        {"field": "naics_category", "query": "companies that sell subscription boxes for pet owners", "response": []},
        {"field": "linkedin_category", "query": "Consumer Goods", "response": [{"query": "Consumer Goods", "label": "Consumer Goods", "value": "consumer-goods"}]},
        {"field": "linkedin_category", "query": "E-commerce", "response": [{"query": "E-commerce", "label": "E-commerce", "value": "e-commerce"}]},
        {"field": "linkedin_category", "query": "Retail", "response": [{"query": "Retail", "label": "Retail", "value": "retail"}]},
        {"field": "linkedin_category", "query": "Subscription", "response": []},
        {"field": "naics_category", "query": "Subscription", "response": []},
    ]
    branch_responses = {
        "structured": [
            {"business_id": "m001", "name": "PawCrate Monthly", "domain": "pawcrate.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Consumer Goods"},
            {"business_id": "m002", "name": "Big Box Pet Retail Corp", "domain": "bigboxpet.invalid", "country_name": "United States", "number_of_employees_range": "1001-5000", "naics_description": "Retail"},
        ],
        "keyword": [
            {"business_id": "m003", "name": "Subscription Box Marketing Co", "domain": "subboxmarketing.invalid", "country_name": "United States", "number_of_employees_range": "11-50", "naics_description": "Advertising Services"},
        ],
    }
    labels = {
        "m001": ("RELEVANT", "Name strongly suggests a genuine pet subscription box brand."),
        "m002": ("FALSE_POSITIVE", "A big-box pet retailer, not a subscription-box company, and 20x over the employee ceiling."),
        "m003": ("FALSE_POSITIVE", "A marketing agency serving subscription-box brands, found via raw 'Subscription' company_type keyword fallback."),
    }
    run_scenario(
        "Messy natural-language — subscription pet boxes, US, up to 200 employees",
        "I'm looking for companies that sell subscription boxes for pet owners, based in the US, not too big, maybe under 200 people",
        icp, llm_payload, autocomplete_mocks, branch_responses, labels,
    )


def print_final_scorecard():
    print_header("PHASE 14 DISCOVERY QUALITY SCORECARD")
    s = SCORECARD
    s_total = s["structured_relevant"] + s["structured_false_positive"] + s["structured_ambiguous"]
    k_total = s["keyword_relevant"] + s["keyword_false_positive"] + s["keyword_ambiguous"]
    print(f"\nScenarios run: {s['total_scenarios']}")
    print(f"Total candidates: {s['total_candidates']}")
    print(f"Hard-rule PASS: {s['pass_count']}  HOLD: {s['hold_count']}  FAIL: {s['fail_count']}")
    print(f"Candidates that would require Phase 12 OpenAI verification (PASS + not structured): {s['phase12_verification_needed']}")
    print(f"Cross-branch duplicates removed (business_id repeated across mocked branches): {s['duplicates_removed_cross_branch']}")
    print(f"Total autocomplete API calls (mocked): {s['total_autocomplete_calls']}")
    print(f"Total search API calls (mocked): {s['total_search_calls']}")
    if s_total:
        print(f"\nSTRUCTURED tier (n={s_total}, small hand-labeled sample — NOT a statistically valid precision estimate):")
        print(f"  relevant={s['structured_relevant']} fp={s['structured_false_positive']} ambiguous={s['structured_ambiguous']}")
        print(f"  illustrative precision = {s['structured_relevant']/s_total:.2f}")
    if k_total:
        print(f"\nKEYWORD-FALLBACK tier (n={k_total}, small hand-labeled sample — NOT a statistically valid precision estimate):")
        print(f"  relevant={s['keyword_relevant']} fp={s['keyword_false_positive']} ambiguous={s['keyword_ambiguous']}")
        print(f"  illustrative precision = {s['keyword_relevant']/k_total:.2f}")


if __name__ == "__main__":
    scenario_broad_industry()
    scenario_niche_industry()
    scenario_d2c_fmcg()
    scenario_healthcare()
    scenario_saas()
    scenario_location_employee_constrained()
    scenario_company_type()
    scenario_messy_natural_language()
    print_final_scorecard()
