"""Phase 21 — Explorium taxonomy autocomplete probe (credit-safe, measurement only).

ONLY calls GET /v1/businesses/autocomplete. Never /v2/businesses (search),
never enrichment, never Unipile, never OpenAI. Aborts the entire probe on
the FIRST sign of a credit-related error, non-200 status, or empty-body
irregularity — never retries a failure, never continues past a suspicious
response.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import truststore

truststore.inject_into_ssl()

import httpx
from dotenv import load_dotenv
import os

load_dotenv()

API_KEY = os.environ.get("EXPLORIUM_API_KEY")
AUTOCOMPLETE_URL = "https://api.explorium.ai/v1/businesses/autocomplete"

# (icp_archetype, phrase) — deliberately small, deliberately spanning
# archetypes this session has NO prior real data for (FMCG, OTT, SaaS,
# emerging/niche), plus one Healthcare/D2C phrase each as a known-good/
# known-hard control pair from Phase 18/19's already-collected results.
PROBE_SET = [
    ("Healthcare (control, known match)", "Healthcare"),
    ("D2C (control, known no-match)", "D2C skincare"),
    ("FMCG", "FMCG"),
    ("FMCG", "Consumer Packaged Goods"),
    ("OTT/Streaming", "OTT streaming"),
    ("OTT/Streaming", "Video Streaming"),
    ("SaaS", "SaaS"),
    ("SaaS", "Software as a Service"),
    ("Emerging/Niche", "Microdrama"),
    ("Emerging/Niche", "AI Agents"),
]


def _looks_credit_related(status_code: int, body_text: str) -> bool:
    if status_code in (401, 402, 403, 429):
        return True
    if "credit" in body_text.lower() or "quota" in body_text.lower() or "insufficient" in body_text.lower():
        return True
    return False


def probe_one(field: str, query: str) -> dict:
    response = httpx.get(
        AUTOCOMPLETE_URL,
        params={"field": field, "query": query},
        headers={"API_KEY": API_KEY},
        timeout=15,
    )
    result = {
        "field": field,
        "query": query,
        "status_code": response.status_code,
        "raw_body": None,
        "credit_flag": False,
    }
    if _looks_credit_related(response.status_code, response.text):
        result["credit_flag"] = True
        result["raw_body"] = response.text[:500]
        return result

    if response.status_code != 200:
        result["raw_body"] = response.text[:500]
        return result

    try:
        result["raw_body"] = response.json()
    except ValueError:
        result["raw_body"] = response.text[:500]
    return result


def main():
    if not API_KEY:
        print("EXPLORIUM_API_KEY not set — aborting.", file=sys.stderr)
        sys.exit(1)

    all_results = []
    call_count = 0
    aborted = False

    for archetype, phrase in PROBE_SET:
        for field in ("linkedin_category", "naics_category"):
            call_count += 1
            r = probe_one(field, phrase)
            r["archetype"] = archetype
            all_results.append(r)
            print(f"[{call_count}] {archetype} | {field} | {phrase!r} -> status={r['status_code']} credit_flag={r['credit_flag']}", file=sys.stderr)

            if r["credit_flag"]:
                print(f"CREDIT-RELATED SIGNAL DETECTED — ABORTING IMMEDIATELY after call #{call_count}.", file=sys.stderr)
                print(f"Raw body: {r['raw_body']}", file=sys.stderr)
                aborted = True
                break
        if aborted:
            break

    out_path = Path(__file__).resolve().parent / "phase21_results.json"
    out_path.write_text(json.dumps({"aborted": aborted, "total_calls": call_count, "results": all_results}, indent=2, default=str), encoding="utf-8")
    print(f"\nTotal autocomplete calls made: {call_count}", file=sys.stderr)
    print(f"Aborted early due to credit signal: {aborted}", file=sys.stderr)
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
