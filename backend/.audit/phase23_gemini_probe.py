"""Phase 23 — Gemini A/B test, discovery-strategy interpretation only.

Calls the REAL, UNMODIFIED interpret_icp() exactly once, resolved via the
new get_discovery_strategy_llm_provider() (Gemini, per
DISCOVERY_STRATEGY_LLM_PROVIDER=gemini in .env). No Explorium, no Unipile,
no second call. Captures token usage from Gemini's own response body
(usageMetadata), which GeminiProvider._call() itself discards (mirrors the
OpenAI probe's httpx.post wrapping — observes the same network response,
does not add a second request).
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
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.services.discovery_strategy import interpret_icp
from app.services.llm_providers.default_registry import get_discovery_strategy_llm_provider

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
    print(f"Resolved discovery-strategy LLM provider: {type(provider).__name__} ({provider.provider_id}, model={provider.model_id})", file=sys.stderr)
    print(f"discovery_strategy_llm_provider setting = {settings.discovery_strategy_llm_provider!r}", file=sys.stderr)

    if type(provider).__name__ != "GeminiProvider":
        print("Provider did not resolve to GeminiProvider (check GEMINI_API_KEY / DISCOVERY_STRATEGY_LLM_PROVIDER in .env) — aborting, no call made.", file=sys.stderr)
        sys.exit(1)

    icp = CanonicalICP(
        icp_id="phase23-gemini-probe",
        version=1,
        hard_rules=CanonicalHardRules(
            industries=("B2B SaaS",),
            geography=CanonicalGeography(countries=(GeographyEntry(raw="United States", code="US", label="United States"),)),
            employee_range=EmployeeRange(min=50, max=500),
            allowed_titles=("VP of Sales", "Head of Revenue Operations"),
            company_types=(),
            exclusions=("agencies",),
        ),
        soft_preferences=CanonicalSoftPreferences(
            business_models=("B2B",),
            growth_signals=("recently raised Series B",),
        ),
    )

    original_post = httpx.post

    def _wrapped_post(*args, **kwargs):
        response = original_post(*args, **kwargs)
        _capture_hook(response)
        return response

    httpx.post = _wrapped_post
    try:
        strategy = interpret_icp(icp, provider)
    finally:
        httpx.post = original_post

    body = _captured_response.get("body") or {}
    usage = body.get("usageMetadata")

    result = {
        "provider_id": provider.provider_id,
        "model_id": provider.model_id,
        "strategy_status": strategy.status,
        "industry_terms": list(strategy.industry_terms),
        "company_type_terms": list(strategy.company_type_terms),
        "exclusion_terms": list(strategy.exclusion_terms),
        "geography_notes": list(strategy.geography_notes),
        "unsupported_intent": list(strategy.unsupported_intent),
        "confidence": strategy.confidence,
        "reasoning": strategy.reasoning,
        "error_message": strategy.error_message,
        "captured_http_status": _captured_response.get("status_code"),
        "captured_usage_metadata": usage,
        "captured_model_version": body.get("modelVersion"),
    }

    out_path = Path(__file__).resolve().parent / "phase23_results.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str), file=sys.stderr)
    print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
