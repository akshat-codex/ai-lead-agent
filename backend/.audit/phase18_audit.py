"""Phase 18 — Company Shortlist Quality Audit driver.

Runs a set of realistic ICPs through the REAL, unmodified pipeline (via
the FastAPI TestClient, exactly the production code path) and dumps a
structured JSON report per ICP: discovery counts, hard-rule distribution,
company-quality label/score distribution, ranking output, and how many
companies reached the people-discovery (Unipile-position) stage.

LIVE vs MOCK is explicit per run via the `live` flag passed to run_one().
No pipeline code is modified by this script — it only calls existing
endpoints (ICP create, batch create, company-quality list, rankings) and
inspects the results.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import truststore

truststore.inject_into_ssl()

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.main import app
from app.providers.default_registry import get_provider_registry, build_default_registry
from app.providers.registry import ProviderRegistry
from app.providers.mocks import (
    MockCompanyDataProvider,
    MockCompanyRegistryProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)
from app.core.config import get_settings


def _fresh_client() -> TestClient:
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
    return TestClient(app)


def _mock_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())
    registry.register(MockPeopleDataProvider())
    registry.register(MockWebSearchProvider())
    return registry


def _live_registry() -> ProviderRegistry:
    get_settings.cache_clear()
    return build_default_registry(get_settings())


def _icp_payload(name: str, hard: dict) -> dict:
    base = {
        "industry": [], "geography": [], "min_employees": None, "max_employees": None,
        "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
    }
    base.update(hard)
    return {
        "name": name,
        "hard_rules": base,
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [],
            "growth_signals": [], "marketing_signals": [], "other_preferences": [],
        },
    }


def run_one(icp_name: str, hard_rules: dict, target_count: int, live: bool) -> dict:
    client = _fresh_client()
    registry = _live_registry() if live else _mock_registry()
    app.dependency_overrides[get_provider_registry] = lambda: registry

    try:
        icp = client.post("/api/v1/icps", json=_icp_payload(icp_name, hard_rules)).json()
        if "id" not in icp:
            return {"icp_name": icp_name, "live": live, "error": f"ICP creation failed: {icp}"}

        batch_resp = client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": target_count})
        batch = batch_resp.json()
        if "items" not in batch:
            return {"icp_name": icp_name, "live": live, "error": f"batch creation failed: {batch}", "status_code": batch_resp.status_code}

        items = batch["items"]
        hard_rule_counts = {"PASS": 0, "HOLD": 0, "FAIL": 0, None: 0}
        for item in items:
            hard_rule_counts[item.get("hard_rule_result")] = hard_rule_counts.get(item.get("hard_rule_result"), 0) + 1

        quality_rows = []
        for item in items:
            if item.get("company_id"):
                rows = client.get("/api/v1/company-quality", params={"icp_id": icp["id"], "company_id": item["company_id"]}).json()
                if rows:
                    quality_rows.append(rows[-1])

        label_counts = {"STRONG": 0, "REVIEW": 0, "REJECT": 0}
        scores_by_label: dict[str, list[float]] = {"STRONG": [], "REVIEW": [], "REJECT": []}
        provenance_by_label: dict[str, list[str]] = {"STRONG": [], "REVIEW": [], "REJECT": []}
        for row in quality_rows:
            label = row["label"]
            label_counts[label] = label_counts.get(label, 0) + 1
            if row.get("score") is not None:
                scores_by_label[label].append(row["score"])
            signals = row.get("signals", [])
            provenance_signal = next((s for s in signals if s["name"] == "discovery_provenance"), None)
            if provenance_signal:
                if "structured" in provenance_signal["explanation"]:
                    provenance_by_label[label].append("structured")
                elif "keyword" in provenance_signal["explanation"]:
                    provenance_by_label[label].append("keyword")
                else:
                    provenance_by_label[label].append("unknown")

        people_reached = sum(1 for item in items if item.get("person_id") is not None)
        eligible_for_unipile = sum(1 for row in quality_rows if row["label"] != "REJECT")

        ranking_resp = client.get("/api/v1/rankings", params={"icp_id": icp["id"], "batch_id": batch["id"]})
        ranking = ranking_resp.json()
        top5 = []
        if "ranked_leads" in ranking:
            for rl in ranking["ranked_leads"][:5]:
                top5.append({
                    "rank": rl["rank"], "tier": rl["tier"], "company_id": rl["company_id"],
                    "final_score": rl["signals"].get("final_score"),
                    "company_quality_score": rl["signals"].get("company_quality_score"),
                    "company_quality_label": rl["signals"].get("company_quality_label"),
                })

        outcome_counts = {}
        for item in items:
            outcome_counts[item.get("outcome")] = outcome_counts.get(item.get("outcome"), 0) + 1

        return {
            "icp_name": icp_name,
            "live": live,
            "hard_rules": hard_rules,
            "target_count": target_count,
            "discovered_count": batch.get("discovered_count"),
            "batch_status": batch.get("status"),
            "discovery_error_code": batch.get("discovery_error_code"),
            "discovery_error_message": batch.get("discovery_error_message"),
            "hard_rule_counts": hard_rule_counts,
            "outcome_counts": outcome_counts,
            "quality_label_counts": label_counts,
            "quality_scores_by_label": {k: v for k, v in scores_by_label.items()},
            "provenance_by_label": provenance_by_label,
            "people_discovery_reached_count": people_reached,
            "eligible_for_people_discovery_count": eligible_for_unipile,
            "total_items": len(items),
            "top5_ranked": top5,
            "raw_items": items,
            "raw_quality_rows": quality_rows,
        }
    finally:
        app.dependency_overrides.clear()


ICP_SCENARIOS = [
    # (name, hard_rules, target_count, live)
    ("Healthcare SaaS (US, 50-500)", {
        "industry": ["Healthcare"], "geography": ["United States"],
        "min_employees": 50, "max_employees": 500,
    }, 10, True),
    ("Fintech Series B+ (US, 100-1000)", {
        "industry": ["Financial Services"], "geography": ["United States"],
        "min_employees": 100, "max_employees": 1000,
    }, 10, True),
    ("D2C Skincare (broad, keyword-heavy)", {
        "industry": ["D2C skincare"], "geography": [], "min_employees": 1, "max_employees": 10000,
    }, 8, True),
]

MOCK_SCENARIOS = [
    ("Manufacturing Mid-Market [MOCK]", {"industry": ["Manufacturing"], "geography": ["United States"], "min_employees": 200, "max_employees": 2000}, 5, False),
    ("Legal Services Small Firms [MOCK]", {"industry": ["Legal Services"], "geography": ["United States"], "min_employees": 5, "max_employees": 50}, 5, False),
    ("EdTech Series A [MOCK]", {"industry": ["Education Technology"], "geography": ["United States"], "min_employees": 20, "max_employees": 200}, 5, False),
    ("Real Estate Tech Broad [MOCK]", {"industry": ["Real Estate"], "geography": [], "min_employees": None, "max_employees": None}, 5, False),
    ("Marketing Agencies Excl. Freelancers [MOCK]", {"industry": ["Marketing"], "geography": ["United States"], "min_employees": 10, "max_employees": 100, "exclusions": ["freelance"]}, 5, False),
    ("No Industry Specified (pure geo+size) [MOCK]", {"industry": [], "geography": ["United States"], "min_employees": 50, "max_employees": 500}, 5, False),
]


def main():
    results = []
    for name, hard, target, live in ICP_SCENARIOS + MOCK_SCENARIOS:
        print(f"Running: {name} (live={live})...", file=sys.stderr)
        try:
            result = run_one(name, hard, target, live)
        except Exception as exc:
            result = {"icp_name": name, "live": live, "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(f"  -> discovered={result.get('discovered_count')}, hard_rules={result.get('hard_rule_counts')}, quality={result.get('quality_label_counts')}", file=sys.stderr)

    out_path = Path(__file__).resolve().parent / "phase18_results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
