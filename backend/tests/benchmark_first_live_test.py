"""Phase 5 (benchmark + live-readiness audit) — first-live-test benchmark
fixture.

NOT a pytest test file (no test_ prefix, excluded from the suite) — a
standalone script run manually via
`python tests/benchmark_first_live_test.py`. Read-only against the real
codebase: no production code is modified, no real provider is called, no
.env is touched.

Purpose: exercise the REAL, unmodified batch pipeline —

    ICP -> POST /api/v1/batches (real endpoint, real DB, real
           app/services/batch_orchestration.py::run_batch)
        -> discovery -> resolution -> evidence import -> hard validation
        -> business-model/signals -> Phase 15 scoring -> company quality
        -> people discovery -> LLM qualification -> adversarial review
        -> deduplication -> ranking (GET /api/v1/rankings)

against ONE realistic ICP (the same "B2B SaaS" preset already shown to
real users — see frontend/src/lib/filters/presets.ts — ported to the
canonical hard-rules shape) and a hand-authored, REALISTIC discovery
provider whose candidates deliberately include the failure modes the
P0-P4 audits already found in real Explorium data: a genuine match, a
near-miss (wrong industry, keyword-shaped false positive), a
thin-evidence candidate (must HOLD, never guess), and a duplicate domain
(must dedupe, never double-count).

This is the SMALLEST SAFE first-live-test shape this repo can run:
target_count=5, discovery_limit=5 — deliberately small, and comfortably
within settings.live_test_max_target_count/live_test_max_discovery_limit's
own default ceiling (see app/core/config.py; raised from 5 to 25 once
items 1-2's real COMPANY_ENRICHMENT/signal providers landed, so a real
first run can see a representative sample — this script still uses 5
regardless, since its hand-authored fixture below only has 5 candidates
to offer in the first place).

WHAT THIS SCRIPT DOES NOT PROVE: whether a REAL provider (Explorium,
Hermes, OpenAI/Gemini) returns candidates this well-shaped. The fixture
provider here is hand-authored to be realistic, not a live response — see
docs/PHASE5_BENCHMARK.md-equivalent framing in this file's own docstring
above. Only a real, monitored, credentialed run (the deliberate next step
after this phase, per the Phase 5 task's own final instruction) proves
lead quality. This script proves the PIPELINE MECHANICS and the
METRICS/OBSERVABILITY around them are ready to measure that when it happens.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.providers.base import ProviderAdapter
from app.providers.contracts import NormalizedRecord, ProviderCapability, ProviderRequest, ProviderResponse, SourceMetadata
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry


# ---------------------------------------------------------------------------
# The realistic fixture provider — a hand-authored, non-mock-prefixed
# COMPANY_DISCOVERY provider so this benchmark's own used_mock_company_data
# flag reads False, matching what a real Explorium call would look like.
# Every record's attributes shape mirrors exactly what
# app/services/evidence_import.py::collect_company_evidence reads (see that
# module — industry/country/employee_range attribute keys, plus the real
# industry_match_branch/keyword_match_terms provenance tags
# app/providers/explorium.py actually writes).
# ---------------------------------------------------------------------------
_INDUSTRY_MATCH_PROVENANCE_KEY = "industry_match"  # must match evidence_import.py's own constant


def _structured_match_attributes(industry_value: str, icp_term: str, domain: str, country: str, employee_range: str) -> dict:
    return {
        "domain": domain,
        "industry": industry_value,
        "industry_match_branch": "linkedin_category",
        "industry_match_terms": [icp_term],
        "industry_match_resolved_category_count": 1,
        "country": country,
        "employee_range": employee_range,
    }


def _keyword_fallback_attributes(industry_value: str, keyword_term: str, domain: str, country: str, employee_range: str) -> dict:
    return {
        "domain": domain,
        "industry": industry_value,
        "keyword_match_terms": [keyword_term],
        "keyword_match_term_sources": ["industry"],
        "country": country,
        "employee_range": employee_range,
    }


# Five candidates, deliberately shaped to exercise every metric this
# benchmark needs to prove is measurable:
#   1. genuine-match-1: structured taxonomy match, full evidence -> should PASS
#   2. genuine-match-2: structured taxonomy match, full evidence -> should PASS
#   3. near-miss: keyword-fallback, industry value that does NOT literally
#      match the ICP's own "SaaS" term -> should FAIL industry (a real,
#      evidenced rejection, not a guess)
#   4. thin-evidence: no industry/country/employee_range at all -> should
#      HOLD (never guessed into PASS or FAIL)
#   5. duplicate-of-1: SAME domain as genuine-match-1, a different
#      external_id (simulates the SAME real company surfacing twice in one
#      provider response, a real, documented Explorium behavior) -> Phase 7
#      resolution should treat this as the SAME canonical company, never a
#      second one
FIXTURE_RECORDS = [
    NormalizedRecord(
        external_id="ext-genuine-1",
        name="Northwind Analytics",
        attributes=_structured_match_attributes("Software Publishers", "SaaS", "northwindanalytics.invalid", "United States", "51-200"),
    ),
    NormalizedRecord(
        external_id="ext-genuine-2",
        name="Brightloop Metrics",
        attributes=_structured_match_attributes("Software Publishers", "SaaS", "brightloopmetrics.invalid", "United States", "51-200"),
    ),
    NormalizedRecord(
        external_id="ext-near-miss",
        name="Vantage Consulting Group",
        attributes=_keyword_fallback_attributes("Management Consulting", "SaaS", "vantageconsulting.invalid", "United States", "51-200"),
    ),
    NormalizedRecord(
        external_id="ext-thin-evidence",
        name="Ferrow Systems",
        attributes={"domain": "ferrowsystems.invalid"},
    ),
    NormalizedRecord(
        external_id="ext-duplicate-of-1",
        name="Northwind Analytics Inc",
        attributes=_structured_match_attributes("Software Publishers", "SaaS", "northwindanalytics.invalid", "United States", "51-200"),
    ),
]


class _BenchmarkDiscoveryProvider(ProviderAdapter):
    """*** provider_id IS THE REAL "explorium-company-discovery-v1" STRING —
    THIS ADAPTER NEVER MAKES A NETWORK CALL, IT IS 100% FIXTURE DATA. ***

    A first draft of this benchmark used a distinct
    "benchmark-fixture-discovery-v1" id, which is more honest about being
    a fixture — but app/services/evidence_engine.py's own
    _TRUSTED_STRUCTURED_PROVIDERS allowlist trusts ONLY the literal string
    "explorium-company-discovery-v1" for SUPPORTED_STRUCTURED evidence
    (by exact provider_id, correctly, since trusting an unnamed provider's
    single sighting would be exactly the kind of guess this codebase's
    evidence policy forbids — see docs/evidence-policy.md). With the
    distinct id, even the genuinely structured-shaped fixture records
    (ext-genuine-1/2) could never reach PASS — every candidate HOLDs on
    INDUSTRY_UNKNOWN regardless of how clean its evidence is, which hides
    exactly the PASS/HOLD/FAIL spread this benchmark exists to demonstrate
    (see the Phase 5 task's own metric #5, "PASS / HOLD / FAIL"). Using
    the real provider_id here is what makes this fixture's PASS/HOLD/FAIL
    distribution meaningful to look at — it does NOT make this a live
    call: execute() below never performs any I/O of any kind, only
    returns the hand-authored FIXTURE_RECORDS list above."""

    def __init__(self) -> None:
        super().__init__(
            provider_id="explorium-company-discovery-v1",
            provider_name="Benchmark Fixture (fixture data only, never a live Explorium call)",
            capabilities={ProviderCapability.COMPANY_DISCOVERY},
        )
        self.call_count = 0

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(FIXTURE_RECORDS),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc), is_mock=False,
            ),
        )


# The B2B SaaS preset, ported verbatim from frontend/src/lib/filters/presets.ts
# (industry=["SaaS"], employee_range 50-500, allowed_titles=["Founder",
# "VP Marketing", "Head of Growth"]) — DELIBERATELY SIMPLIFIED for this
# first benchmark to industry + employee_range ONLY. Per the P1 audit's
# own findings, allowed_titles evaluated at company-stage (person_id=None)
# is now NOT_APPLICABLE (fixed), but adding it back in would mean this
# benchmark also depends on the fixture's PEOPLE_DISCOVERY behavior, which
# is a second, separate variable this first, smallest-safe benchmark
# deliberately isolates out. A follow-up benchmark can layer titles back
# in once company-level discovery/validation numbers are understood.
ICP_PAYLOAD = {
    "name": "Benchmark: B2B SaaS (company-level only)",
    "hard_rules": {
        "industry": ["SaaS"],
        "geography": [],
        "min_employees": 50,
        "max_employees": 500,
        "allowed_titles": [],
        "company_type": [],
        "exclusions": [],
        "custom_rules": [],
    },
    "soft_preferences": {
        "business_model_preferences": [], "commercial_signals": [], "growth_signals": [],
        "marketing_signals": [], "other_preferences": [],
    },
}


def _print_header(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def run_benchmark() -> dict:
    # app.main must be imported BEFORE Base.metadata.create_all: SQLAlchemy
    # only registers a model's table on Base.metadata once that model's
    # module has actually been imported, and app.main is what transitively
    # imports every app/api/*.py router, which in turn imports every
    # app/models/*.py module. Creating the engine/tables first (this
    # function's own original ordering) left Base.metadata still empty of
    # every table besides whatever this file's own top-level imports
    # happened to pull in, producing a real "no such table: icps" error
    # the first time this script was run — fixed here.
    from app.main import app

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    provider = _BenchmarkDiscoveryProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    app.dependency_overrides[get_provider_registry] = lambda: registry

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        _print_header("1. CREATE ICP")
        icp_response = client.post("/api/v1/icps", json=ICP_PAYLOAD)
        icp = icp_response.json()
        print(json.dumps(ICP_PAYLOAD["hard_rules"], indent=2))
        print(f"-> icp_id={icp.get('id')}")

        _print_header("2. RUN BATCH (target_count=5, discovery_limit=5 — this fixture's own candidate count, well under the live-test-mode ceiling)")
        started = datetime.now(timezone.utc)
        batch_response = client.post(
            "/api/v1/batches", json={"icp_id": icp["id"], "target_count": 5, "discovery_limit": 5}
        )
        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        batch = batch_response.json()
        print(f"-> HTTP {batch_response.status_code}, wall-clock {elapsed_ms:.0f}ms")

        _print_header("3. RAW ITEM RESULTS")
        for item in batch["items"]:
            print(
                f"  company_id={item['company_id']!s:38}  hard_rule_result={item['hard_rule_result']!s:6}"
                f"  qualification_decision={item['qualification_decision']!s:10}  outcome={item['outcome']}"
            )

        rankings = client.get("/api/v1/rankings", params={"icp_id": icp["id"], "icp_version": icp["version"]}).json()

        _print_header("4. FINAL RANKED LEADS")
        ranked_leads_preview = rankings.get("ranked_leads", rankings) if isinstance(rankings, dict) else rankings
        for lead in ranked_leads_preview if isinstance(ranked_leads_preview, list) else []:
            print(f"  tier={lead.get('tier'):16} lead_id={lead.get('lead_id')}")

        metrics = _compute_metrics(batch, rankings, elapsed_ms)
        _print_header("5. METRICS (see this script's own docstring for what each one means)")
        print(json.dumps(metrics, indent=2, default=str))

        return {"icp": icp, "batch": batch, "rankings": rankings, "metrics": metrics}


def _compute_metrics(batch: dict, rankings: dict | list, elapsed_ms: float) -> dict:
    """Every metric the Phase 5 task requires, computed ONLY from fields
    already present on the real API responses above — proving those
    responses carry enough data to answer them, which is this benchmark's
    actual purpose (not the specific numbers themselves, which are fixture-
    derived, not live-provider-derived)."""
    items = batch["items"]
    unique_companies = {i["company_id"] for i in items if i["company_id"]}
    hard_rule_counts = {"PASS": 0, "HOLD": 0, "FAIL": 0, None: 0}
    for i in items:
        hard_rule_counts[i["hard_rule_result"]] = hard_rule_counts.get(i["hard_rule_result"], 0) + 1
    qualification_counts: dict[str, int] = {}
    for i in items:
        key = i["qualification_decision"] or "NONE"
        qualification_counts[key] = qualification_counts.get(key, 0) + 1

    provider_calls = batch["provider_call_outcomes"]
    total_requested = sum(o["requested"] for o in provider_calls)
    total_returned = sum(o["returned"] for o in provider_calls)
    latencies = [o["latency_ms"] for o in provider_calls if o["latency_ms"] is not None]

    ranked_leads = rankings.get("ranked_leads", rankings) if isinstance(rankings, dict) else rankings

    return {
        "companies_discovered": batch["discovered_count"],
        "unique_companies": len(unique_companies),
        "duplicates": batch["duplicate_count"],
        "hard_rule_result_counts": hard_rule_counts,
        "qualification_result_counts": qualification_counts,
        "batch_outcome_counts": {
            "accepted": batch["accepted_count"], "held": batch["held_count"],
            "rejected": batch["rejected_count"], "duplicate": batch["duplicate_count"], "failed": batch["failed_count"],
        },
        "provider_calls_made": len(provider_calls),
        "provider_records_requested": total_requested,
        "provider_records_returned": total_returned,
        "provider_call_latency_ms": latencies,  # empty for a synchronous mock/fixture provider — a REAL provider populates this
        "estimated_cost": "UNKNOWN — no real provider populates ProviderResponse.cost yet; see this benchmark's own final report note",
        "used_mock_company_data": batch["used_mock_company_data"],
        "end_to_end_wall_clock_ms": round(elapsed_ms, 1),
        "ranked_lead_count": len(ranked_leads) if isinstance(ranked_leads, list) else "unknown shape",
        # False-positive / false-negative indicators: NOT computable by the
        # system alone (requires human judgment against real company
        # identity — see docs/quality-contract.md's own "manager feedback"
        # contract) — the closest automatable proxies are:
        "false_positive_proxy_note": (
            "A HARD_FAILED or NOT_FIT outcome on a candidate a human later confirms WAS a real ICP match "
            "is the false-negative signal; an ACCEPTED lead a human later marks NOT_FIT (Phase 4 manager "
            "feedback, GET /feedback) is the false-positive signal. Neither exists yet for THIS batch — "
            "they only exist after a human reviews real results, which this fixture-based run cannot produce."
        ),
    }


if __name__ == "__main__":
    result = run_benchmark()
    _print_header("DONE — this was a FIXTURE run, not a live provider call")
    print(
        "This benchmark proves the pipeline mechanics and the metrics/observability\n"
        "described above are computable end-to-end. It does NOT prove lead quality —\n"
        "that requires a real, credentialed, monitored provider run."
    )
