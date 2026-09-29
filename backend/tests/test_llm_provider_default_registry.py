"""Phase 9 — tests for app/services/llm_providers/default_registry.py's
conditional registration: OPENAI_API_KEY gates OpenAIProvider, mirroring
app/providers/default_registry.py's EXPLORIUM_API_KEY gate (see
tests/test_default_registry.py).

Exercises build_default_llm_provider() directly (not the module-level
default_llm_provider singleton) so each test can vary Settings without
mutating global state other tests depend on.
"""
from app.core.config import Settings
from app.services.llm_providers.default_registry import _DefaultMockProvider, build_default_llm_provider
from app.services.llm_providers.openai_provider import OpenAIProvider


def _settings(openai_api_key: str | None = None) -> Settings:
    return Settings(openai_api_key=openai_api_key, database_url="sqlite:///:memory:")


def test_no_openai_key_returns_the_deterministic_mock():
    provider = build_default_llm_provider(_settings())
    assert isinstance(provider, _DefaultMockProvider)


def test_openai_key_configured_returns_the_real_provider():
    provider = build_default_llm_provider(_settings(openai_api_key="sk-test"))
    assert isinstance(provider, OpenAIProvider)


def test_openai_provider_uses_the_configured_model_and_base_url():
    settings = Settings(
        openai_api_key="sk-test",
        openai_base_url="https://example.invalid/v1",
        openai_model="gpt-custom",
        database_url="sqlite:///:memory:",
    )
    provider = build_default_llm_provider(settings)
    assert isinstance(provider, OpenAIProvider)
    assert provider.model_id == "gpt-custom"


def test_empty_string_openai_key_behaves_like_unset():
    provider = build_default_llm_provider(_settings(openai_api_key=""))
    assert isinstance(provider, _DefaultMockProvider)
