"""Phase 26 — Gemini-only ICP interpretation test, real ICP row, zero
Explorium involvement anywhere in this script.

Loads the REAL ICPModel row just created via the actual POST /api/v1/icps
endpoint (the same request shape the real frontend sends for "Healthcare
SaaS companies in USA, 50-500 employees"), normalizes it with the real
normalize_icp(), and calls the REAL, UNMODIFIED interpret_icp() exactly
once via get_discovery_strategy_llm_provider() (resolves to GeminiProvider
per DISCOVERY_STRATEGY_LLM_PROVIDER=gemini in .env). Never imports or
touches app/providers/explorium.py, app/services/company_discovery.py, or
run_batch/run_company_discovery — there is no code path in this script
that could reach Explorium.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import truststore

truststore.inject_into_ssl()

import httpx

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.icp import ICPModel
from app.services.discovery_strategy import interpret_icp, merge_strategy_into_hard_rules
from app.services.icp_normalization import normalize_icp
from app.services.llm_providers.default_registry import get_discovery_strategy_llm_provider

ICP_ID = "c8e81952-d28c-4258-b74d-b0c5052e30d5"

_captured_response: dict = {}


def _capture_hook(response: httpx.Response) -> None:
    if "generateContent" in str(response.request.url):
        response.read()
        try:
            _captured_response["body"] = response.json()
        except ValueError:
            _captured_response["body"] = None
        _captured_response["status_code"] = response.status_code


def main():
    get_settings.cache_clear()
    settings = get_settings()
    provider = get_discovery_strategy_llm_provider()
    print(f"Resolved provider: {type(provider).__name__} ({provider.provider_id}, model={provider.model_id})", file=sys.stderr)
    print(f"discovery_strategy_llm_provider={settings.discovery_strategy_llm_provider!r}", file=sys.stderr)

    if type(provider).__name__ != "GeminiProvider":
        print("Did not resolve to GeminiProvider — aborting, no call made.", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        icp_record = db.get(ICPModel, ICP_ID)
        if icp_record is None:
            print(f"ICP {ICP_ID} not found — aborting, no call made.", file=sys.stderr)
            sys.exit(1)
        canonical = normalize_icp(icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences)
    finally:
        db.close()

    print(f"Real ICP loaded: industries={canonical.hard_rules.industries!r} "
          f"geography={[e.label for e in canonical.hard_rules.geography.countries] + list(canonical.hard_rules.geography.unrecognized)!r} "
          f"employee_range={canonical.hard_rules.employee_range.min}-{canonical.hard_rules.employee_range.max}", file=sys.stderr)

    original_post = httpx.post

    def _wrapped_post(*args, **kwargs):
        response = original_post(*args, **kwargs)
        _capture_hook(response)
        return response

    httpx.post = _wrapped_post
    try:
        strategy = interpret_icp(canonical, provider)  # THE ONE CALL
    finally:
        httpx.post = original_post

    # Never calls Explorium — merge_strategy_into_hard_rules is pure,
    # in-memory, no network I/O of any kind. Shows what WOULD be passed to
    # Explorium's own discovery query, without ever constructing or
    # calling the Explorium provider.
    merged_hard_rules = merge_strategy_into_hard_rules(canonical.hard_rules, strategy)

    body = _captured_response.get("body") or {}
    usage = body.get("usageMetadata")

    result = {
        "provider_id": provider.provider_id,
        "model_id": provider.model_id,
        "captured_http_status": _captured_response.get("status_code"),
        "captured_usage_metadata": usage,
        "captured_model_version": body.get("modelVersion"),
        "strategy_status": strategy.status,
        "industry_terms": list(strategy.industry_terms),
        "company_type_terms": list(strategy.company_type_terms),
        "exclusion_terms": list(strategy.exclusion_terms),
        "geography_notes": list(strategy.geography_notes),
        "unsupported_intent": list(strategy.unsupported_intent),
        "confidence": strategy.confidence,
        "reasoning": strategy.reasoning,
        "error_message": strategy.error_message,
        "final_merged_industries": list(merged_hard_rules.industries),
        "final_merged_company_types": list(merged_hard_rules.company_types),
        "final_merged_exclusions": list(merged_hard_rules.exclusions),
    }

    out_path = Path(__file__).resolve().parent / "phase28_results.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str), file=sys.stderr)
    print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
