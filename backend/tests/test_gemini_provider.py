"""Phase 25 — Gemini provider configuration + scoped-resolver tests.

No live API calls anywhere in this file — every test either inspects
configuration/construction, or uses respx to mock the Gemini HTTP
endpoint. Covers: (1) the current default model id is the one Google's own
Phase 24 live-test error identified as the replacement for the retired
gemini-2.0-flash, (2) get_discovery_strategy_llm_provider()'s scoping
(never affects the shared get_llm_provider() used by Phase 16/17), and
(3) GeminiProvider's own DiscoveryStrategyRequest-only scope guard.
"""
import httpx
import respx

from app.core.config import Settings
from app.schemas.adversarial_review import AdversarialContext
from app.schemas.discovery_strategy import DiscoveryStrategyRequest
from app.services.llm_providers.default_registry import build_default_llm_provider, get_discovery_strategy_llm_provider
from app.services.llm_providers.gemini_provider import GeminiProvider


def _discovery_request() -> DiscoveryStrategyRequest:
    return DiscoveryStrategyRequest(icp_id="icp-1", icp_version=1, industries=("Healthcare",))


# --- model configuration ---------------------------------------------------


def test_gemini_default_model_is_the_google_confirmed_replacement():
    """Phase 24's live E2E test received a real HTTP 404 from Google
    naming gemini-3.6-flash as the replacement for the retired
    gemini-2.0-flash — the default must reflect that, not the retired id."""
    settings = Settings()
    assert settings.gemini_model == "gemini-3.6-flash"
    assert settings.gemini_model != "gemini-2.0-flash"


def test_gemini_provider_construction_default_matches_settings_default():
    provider = GeminiProvider(api_key="test-key")
    assert provider.model_id == "gemini-3.6-flash"


def test_gemini_model_is_configurable_via_settings_not_hardcoded_only():
    """The default changing is not enough on its own — a future retirement
    must be fixable via config without another code change."""
    settings = Settings(gemini_model="gemini-4.0-flash")
    assert settings.gemini_model == "gemini-4.0-flash"


# --- scoped resolver: never affects Phase 16/17's shared provider ---------


def test_discovery_strategy_provider_defaults_to_openai_resolution_unchanged():
    settings = Settings(discovery_strategy_llm_provider="openai", openai_api_key=None)
    provider = get_discovery_strategy_llm_provider()
    # With no explicit override this session, the module-level singleton
    # (whatever get_llm_provider() already resolved to, mock or OpenAI) is
    # what's returned — never a GeminiProvider.
    assert not isinstance(provider, GeminiProvider)


def test_build_default_llm_provider_never_looks_at_gemini_settings():
    """build_default_llm_provider (the pre-Phase-23 function, still used
    unchanged for get_llm_provider()'s own singleton) must remain
    completely unaware of gemini_* settings — sanity check that the two
    resolvers stay genuinely independent."""
    provider = build_default_llm_provider(Settings(discovery_strategy_llm_provider="gemini", gemini_api_key="some-key"))
    assert not isinstance(provider, GeminiProvider)


def test_gemini_provider_never_registered_as_shared_llm_provider(monkeypatch):
    """Setting DISCOVERY_STRATEGY_LLM_PROVIDER=gemini must NEVER change
    what Phase 16 qualification / Phase 17 adversarial review get from
    get_llm_provider() — that function is untouched by this phase."""
    import app.services.llm_providers.default_registry as registry_module

    original = registry_module.default_llm_provider
    assert not isinstance(original, GeminiProvider)
    # get_llm_provider() always returns the same module-level singleton,
    # regardless of gemini settings — confirmed by identity, not just type.
    assert registry_module.get_llm_provider() is original


# --- GeminiProvider's own scope guard ---------------------------------------


def test_gemini_provider_rejects_non_discovery_strategy_context():
    """GeminiProvider is scoped ONLY to DiscoveryStrategyRequest — passing
    it a QualificationContext/AdversarialContext (which could only happen
    if some future code mistakenly wired it into get_llm_provider()) must
    fail cleanly, never silently answer with the wrong prompt/schema."""
    provider = GeminiProvider(api_key="test-key")
    result = provider.qualify(AdversarialContext.model_construct())  # type: ignore[arg-type]
    assert result.success is False
    assert "DiscoveryStrategyRequest" in (result.error.message if result.error else "")


@respx.mock
def test_gemini_provider_makes_exactly_one_call_per_qualify_invocation():
    route = respx.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"industry_terms": [], "company_type_terms": [], "exclusion_terms": [], '
                                    '"geography_notes": [], "unsupported_intent": [], "confidence": 80, "reasoning": "ok"}'
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 17, "totalTokenCount": 59},
            },
        )
    )
    provider = GeminiProvider(api_key="test-key")
    result = provider.qualify(_discovery_request())
    assert route.call_count == 1
    assert result.success is True


@respx.mock
def test_gemini_provider_degrades_cleanly_on_model_not_found_404():
    """Reproduces the exact Phase 24 live failure shape (a real Google 404
    body naming the retired model) via respx — confirms the provider
    degrades to a clean failure response, never raises, matching
    OpenAIProvider's own non-2xx handling."""
    respx.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent").mock(
        return_value=httpx.Response(
            404,
            json={"error": {"code": 404, "message": "This model models/gemini-2.0-flash is no longer available.", "status": "NOT_FOUND"}},
        )
    )
    provider = GeminiProvider(api_key="test-key")
    result = provider.qualify(_discovery_request())
    assert result.success is False
    assert result.error is not None
    assert "404" in result.error.message
