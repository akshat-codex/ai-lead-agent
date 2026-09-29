"""Phase 34 — regression tests proving tests/conftest.py's session-wide
credential blanking (see its own module docstring) actually works: a test
that does not explicitly mock/override a provider must never end up
talking to a real, credentialed Explorium/Gemini/OpenAI/Apollo/Unipile
provider just because this machine's .env happens to have a real key
configured for manual/live use of the app."""
from app.core.config import get_settings
from app.providers.contracts import ProviderCapability
from app.providers.default_registry import get_provider_registry
from app.services.llm_providers.default_registry import get_discovery_strategy_llm_provider, get_llm_provider


def test_settings_carry_no_provider_credentials_during_tests():
    s = get_settings()
    assert not s.explorium_api_key
    assert not s.gemini_api_key
    assert not s.openai_api_key
    assert not s.apollo_api_key
    assert not s.unipile_api_key
    assert not s.unipile_dsn
    assert not s.unipile_account_id
    # Forced empty, never left at its real .env value — a stray "gemini"
    # here combined with a re-added GEMINI_API_KEY elsewhere in a test
    # would silently reintroduce the exact live-call risk this file exists
    # to close off.
    assert s.discovery_strategy_llm_provider != "gemini"


def test_discovery_strategy_llm_provider_defaults_to_the_deterministic_mock():
    provider = get_discovery_strategy_llm_provider()
    assert type(provider).__name__ == "_DefaultMockProvider"


def test_qualification_llm_provider_defaults_to_the_deterministic_mock():
    provider = get_llm_provider()
    assert type(provider).__name__ == "_DefaultMockProvider"


def test_default_company_discovery_registry_is_mock_only_unless_a_test_overrides_it():
    """The exact scenario that broke before this phase: a test calling an
    endpoint with no app.dependency_overrides[get_provider_registry]
    active at all (see tests/test_provider_routing_api.py's
    test_provider_with_no_signal_linkage_still_included_and_called) must
    see only mock providers, never a real Explorium/Unipile/Apollo
    instance this machine's .env happens to have credentials for."""
    registry = get_provider_registry()
    discovery_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)}
    assert discovery_ids == {"mock-company-data-v1"}

    people_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)}
    assert people_ids == {"mock-people-data-v1"}

    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "explorium-company-discovery-v1" not in enrichment_ids
    assert "unipile-v1" not in enrichment_ids
