"""Real, honest visibility into which COMPANY_DISCOVERY providers are
actually configured right now — deliberately separate from
app/api/provider_usage.py (that module reports real remaining
usage/credit-balance for an already-configured provider; this module
reports configuration STATUS, independent of usage, for the provider-
picker UI so a user can see which of Explorium/Tavily/Serper/Hermes their
own .env has actually activated before choosing a discovery mode).

No fabricated "accuracy %" anywhere in this module or its response shape.
This codebase has no labeled ground-truth dataset and no feedback loop
that scores a provider's contribution as correct/incorrect, so there is no
real number to report — inventing one would be exactly the kind of guess
this project's own quality contract (docs/quality-contract.md) forbids.
What IS real and reportable: (a) whether a provider is configured, (b)
what KIND of evidence it can contribute (structured database record vs.
free-text search result — a genuine, load-bearing distinction for hard-
rule evaluation, see app/services/hard_rule_engine.py), and (c) whether
the two web-search providers benefit from being combined for corroboration
(see app/services/company_resolution.py's own domain-match merge).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry

router = APIRouter(prefix="/api/v1/discovery-providers", tags=["discovery-providers"])

# Mirrors app/services/person_enrichment.py::_MOCK_PROVIDER_PREFIX's own
# convention for "this is a mock, not a real configured source."
_MOCK_PROVIDER_PREFIX = "mock-"

EvidenceKind = str  # "structured" | "web_search" | "research_agent"


class DiscoveryProviderStatus(BaseModel):
    provider_id: str
    provider_name: str
    configured: bool
    evidence_kind: EvidenceKind
    description: str
    # The env var name(s) a user would set to configure this — surfaced so
    # the UI can tell someone exactly what to add to backend/.env, never a
    # vague "contact support."
    env_vars: list[str]


# Static, factual descriptions — never a measured/fabricated number. Order
# matches README.md's own provider table for consistency.
_PROVIDER_CATALOG: list[dict] = [
    {
        "provider_id": "explorium-company-discovery-v1",
        "provider_name": "Explorium",
        "evidence_kind": "structured",
        "description": (
            "Structured company database — returns real employee count, resolved country, and a real "
            "taxonomy category. The only source whose candidates can cleanly pass every hard rule on "
            "its own evidence."
        ),
        "env_vars": ["EXPLORIUM_API_KEY"],
    },
    {
        "provider_id": "tavily-company-discovery-v1",
        "provider_name": "Tavily",
        "evidence_kind": "web_search",
        "description": (
            "Open web search — covers industries a fixed taxonomy can't name. Returns free-text search "
            "results, not a database record; a free-text bridge lets industry/company_type still pass "
            "the hard-rule gate when the candidate's own description states the required terms, but "
            "employee count and geography have no such bridge yet."
        ),
        "env_vars": ["TAVILY_API_KEY"],
    },
    {
        "provider_id": "serper-company-discovery-v1",
        "provider_name": "Serper",
        "evidence_kind": "web_search",
        "description": (
            "Google search — an independent second web-search source. Combining it with Tavily gives "
            "cross-provider corroboration (the same real company sighted by two independent sources) "
            "without changing what either one individually contributes."
        ),
        "env_vars": ["SERPER_API_KEY"],
    },
    {
        "provider_id": "hermes-icp-search-v1",
        "provider_name": "Hermes",
        "evidence_kind": "research_agent",
        "description": (
            "An async, browser-driven research agent — a secondary, slower (multi-minute) discovery "
            "source, consulted only when the faster sources don't produce enough candidates for a batch."
        ),
        "env_vars": ["HERMES_API_TOKEN"],
    },
]


def _is_configured(registry: ProviderRegistry, provider_id: str) -> bool:
    """A provider counts as configured only if a REAL (non-mock) adapter
    with this exact id is currently registered — mirrors
    app/api/batch.py::_used_mock_company_data's own "mock-" prefix check,
    applied here to registry membership instead of a discovery candidate's
    provenance."""
    for adapter in registry.list_providers():
        if adapter.provider_id == provider_id and not adapter.provider_id.startswith(_MOCK_PROVIDER_PREFIX):
            return True
    return False


@router.get("", response_model=list[DiscoveryProviderStatus])
def list_discovery_providers(registry: ProviderRegistry = Depends(get_provider_registry)) -> list[DiscoveryProviderStatus]:
    """One entry per COMPANY_DISCOVERY-capable provider this codebase
    knows how to integrate, regardless of whether it's currently
    configured — same "always a consistent row count" discipline as
    app/api/provider_usage.py::list_provider_usage."""
    return [
        DiscoveryProviderStatus(
            provider_id=entry["provider_id"],
            provider_name=entry["provider_name"],
            configured=_is_configured(registry, entry["provider_id"]),
            evidence_kind=entry["evidence_kind"],
            description=entry["description"],
            env_vars=entry["env_vars"],
        )
        for entry in _PROVIDER_CATALOG
    ]
