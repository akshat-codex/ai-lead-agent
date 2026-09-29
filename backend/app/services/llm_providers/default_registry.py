"""The application's shared LLM provider.

Populated once, at import time: the deterministic mock by default, or the
real OpenAIProvider when settings.openai_api_key is configured — the exact
same conditional-registration shape as app/providers/default_registry.py's
Phase 5 pattern (e.g. ExploriumCompanyDiscoveryProvider registered only when
EXPLORIUM_API_KEY is set). app/api/llm_qualification.py and
app/api/adversarial_review.py only ever depend on `get_llm_provider()`,
never on a concrete provider class, so this file is the only thing that
changes to swap providers.

The SAME provider instance is deliberately reused for both the Phase 16
qualification call and the Phase 17 adversarial call — Phase 17 must not
stand up a second, incompatible LLM infrastructure. `_call` distinguishes
which request shape it received the same way a real vendor call would (by
inspecting the request's own content/context object), not by any special
per-endpoint wiring. OpenAIProvider follows this exactly (see
openai_provider.py's own `_prompt_for`/`_response_format_for`).

Credentials/configuration for the real provider are read once, here, from
app.core.config.Settings, never hard-coded and never read from the
environment again inside the provider itself — mirroring how
app/providers/default_registry.py hands Explorium/Apollo/Unipile their keys.
"""
from __future__ import annotations

from app.core.config import Settings, get_settings
from app.schemas.adversarial_review import AdversarialContext
from app.schemas.deep_prescreen import DeepPrescreenContext
from app.schemas.discovery_strategy import DiscoveryStrategyRequest
from app.services.llm_providers.base import LLMProvider, LLMProviderResponse
from app.services.llm_providers.gemini_provider import GeminiProvider
from app.services.llm_providers.mock import (
    MockLLMProvider,
    build_good_fit_response,
    build_no_expansion_discovery_strategy_response,
    build_relevant_deep_prescreen_response,
)
from app.services.llm_providers.mock_adversarial import build_survives_response
from app.services.llm_providers.openai_provider import OpenAIProvider


class _DefaultMockProvider(MockLLMProvider):
    """The out-of-the-box provider: always produces a well-formed response
    so the API works end-to-end without any external configuration —
    GOOD_FIT for a Phase 16 qualification call, SURVIVES for a Phase 17
    adversarial call, a no-op (no proposed terms) discovery strategy for a
    Phase 23 discovery-strategy interpretation call, and RELEVANT for a
    deep-mode pre-screen call — never assumes every non-adversarial context
    is a QualificationContext, since DiscoveryStrategyRequest/
    DeepPrescreenContext carry no `.evidence` field at all. Tests that need
    a different behavior register their own MockLLMProvider via dependency
    override, exactly like Phase 5's provider registry override pattern."""

    def _call(self, context):  # type: ignore[override]
        if isinstance(context, AdversarialContext):
            raw_text = build_survives_response(context)
        elif isinstance(context, DiscoveryStrategyRequest):
            raw_text = build_no_expansion_discovery_strategy_response()
        elif isinstance(context, DeepPrescreenContext):
            raw_text = build_relevant_deep_prescreen_response()
        else:
            raw_text = build_good_fit_response(context)

        return LLMProviderResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            success=True,
            raw_text=raw_text,
        )


def build_default_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """`settings` defaults to the real app.core.config.get_settings()
    singleton; an explicit value is accepted so tests can exercise the
    conditional-registration branch without mutating global settings state
    — mirrors app/providers/default_registry.py::build_default_registry."""
    settings = settings or get_settings()
    if settings.openai_api_key:
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model_id=settings.openai_model,
        )
    return _DefaultMockProvider()


default_llm_provider: LLMProvider = build_default_llm_provider()


def get_llm_provider() -> LLMProvider:
    """FastAPI dependency — overridable in tests via app.dependency_overrides,
    the same pattern used for get_provider_registry in app/providers/default_registry.py."""
    return default_llm_provider


def get_discovery_strategy_llm_provider() -> LLMProvider:
    """Phase 23 — the ONE place discovery-strategy interpretation
    (app/services/discovery_strategy.py::interpret_icp, called only from
    app/services/batch_orchestration.py) resolves its provider from,
    SEPARATE from get_llm_provider() (which Phase 16 qualification and
    Phase 17 adversarial review still use, completely unaffected by this
    function or by GEMINI_API_KEY being set).

    settings.discovery_strategy_llm_provider selects explicitly
    ("openai" | "gemini") — merely setting GEMINI_API_KEY does not switch
    anything on its own, so adding a Gemini key to test with never
    silently changes production behavior. For "openai" (the default) or
    any unrecognized value, this returns get_llm_provider()'s own
    default_llm_provider SINGLETON — not a freshly rebuilt provider —
    which matters for two reasons: (1) it is byte-for-byte the same
    provider Phase 16/17 already use, so nothing about today's discovery-
    strategy behavior changes when Gemini isn't selected, and (2) tests
    that monkeypatch llm_registry.default_llm_provider directly (the
    existing, established override pattern — see
    tests/test_batch_discovery_strategy.py's own _install_llm_provider)
    keep working unchanged, since this function reads that same module
    attribute rather than rebuilding a provider from Settings. Only
    constructs GeminiProvider when explicitly selected AND
    gemini_api_key is actually configured — an explicit "gemini"
    selection with no key configured falls back to the default provider
    rather than constructing a GeminiProvider that could never succeed,
    mirroring every other provider's "only register when the credential
    is present" discipline in this codebase."""
    settings = get_settings()
    if settings.discovery_strategy_llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model_id=settings.gemini_model,
        )
    return get_llm_provider()


def get_deep_prescreen_llm_provider() -> LLMProvider:
    """Deep discovery mode's own provider resolution — SEPARATE from both
    get_llm_provider() (Phase 16 qualification / Phase 17 adversarial
    review) and get_discovery_strategy_llm_provider() (Phase 10 ICP term
    expansion), mirroring exactly the same isolation discipline that
    function's own docstring describes: a change to deep_prescreen_llm_provider
    or deep_prescreen_openai_model can never affect either of those other
    two call sites, and vice versa.

    settings.deep_prescreen_llm_provider selects explicitly ("openai" |
    "gemini"); "openai" (the default) or any unrecognized value builds a
    dedicated OpenAIProvider using settings.deep_prescreen_openai_model
    when set (a cheaper/faster model than settings.openai_model), falling
    back to settings.openai_model itself when unset — so leaving
    deep_prescreen_openai_model unset costs nothing extra to configure and
    still runs on OpenAI's own cheap/fast default tier (see
    Settings.openai_model's own comment). This is a FRESH OpenAIProvider
    instance, not the shared default_llm_provider singleton
    get_discovery_strategy_llm_provider() reuses — deep mode's model choice
    is deliberately allowed to diverge from qualification's, so it cannot
    be the same singleton object. Only constructs OpenAIProvider when
    openai_api_key is actually configured; with no key configured this
    returns get_llm_provider()'s own default (the deterministic mock when
    no real provider is configured at all, or the shared OpenAIProvider
    when openai_api_key IS set but this function's own "fresh instance"
    branch was skipped for gemini) — mirroring every other provider's
    "only register a real call path when its own credential is present"
    discipline."""
    settings = get_settings()
    if settings.deep_prescreen_llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model_id=settings.gemini_model,
        )
    if settings.openai_api_key:
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model_id=settings.deep_prescreen_openai_model or settings.openai_model,
        )
    return get_llm_provider()
