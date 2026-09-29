"""Phase 22 — OpenAI cost-only test.

Calls the REAL, UNMODIFIED interpret_icp() exactly once, against the real
OpenAIProvider, for exactly one representative universal ICP. No Explorium,
no Unipile, no discovery, no second OpenAI call. A single httpx event hook
observes the SAME network response interpret_icp() already triggers (it
does not add a second request) so this script can report token usage,
which OpenAIProvider._call() itself discards (it only reads
choices[0].message.content, never the `usage` field) without touching any
production code.
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
from app.services.llm_providers.default_registry import get_llm_provider

_captured_response: dict = {}


def _capture_hook(response: httpx.Response) -> None:
    # Fires on the SAME response interpret_icp()'s own httpx.post() call
    # receives — this is an httpx event hook, not a second request.
    if "/chat/completions" in str(response.request.url):
        response.read()  # must read the body before it can be accessed in a hook
        try:
            _captured_response["body"] = response.json()
        except ValueError:
            _captured_response["body"] = None
        _captured_response["status_code"] = response.status_code


def main():
    get_settings.cache_clear()
    provider = get_llm_provider()
    print(f"Resolved LLM provider: {type(provider).__name__} ({provider.provider_id}, model={provider.model_id})", file=sys.stderr)

    if type(provider).__name__ != "OpenAIProvider":
        print("OPENAI_API_KEY not configured (or provider not resolved to OpenAIProvider) — aborting, no call made.", file=sys.stderr)
        sys.exit(1)

    # A representative UNIVERSAL ICP — mid-market B2B SaaS, not D2C-specific.
    icp = CanonicalICP(
        icp_id="phase22-cost-probe",
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

    # Monkeypatch-free capture: httpx supports per-client event_hooks, but
    # interpret_icp() constructs its own httpx.post() call with no client
    # object exposed to inject hooks into. Instead, wrap httpx.post itself
    # for the duration of this ONE call only, then restore it immediately
    # — this changes no production file and affects no other call site;
    # it only observes the single request/response interpret_icp() makes
    # on its own, unchanged code path.
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
        "captured_usage": (_captured_response.get("body") or {}).get("usage"),
        "captured_model_from_response": (_captured_response.get("body") or {}).get("model"),
    }

    out_path = Path(__file__).resolve().parent / "phase22_results.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str), file=sys.stderr)
    print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
