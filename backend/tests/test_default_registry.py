"""Tests for app/providers/default_registry.py's conditional registration —
Phase 7A (corrected): ExploriumCompanyDiscoveryProvider gates on
EXPLORIUM_API_KEY for COMPANY_DISCOVERY, completely independent of
ApolloPersonEnrichmentProvider's own APOLLO_API_KEY gate for
PERSON_ENRICHMENT. Apollo must never appear under COMPANY_DISCOVERY.

Phase 7B: UnipileProvider gates on all three of UNIPILE_API_KEY/
UNIPILE_DSN/UNIPILE_ACCOUNT_ID together, for PEOPLE_DISCOVERY (replacing,
never merging with, MockPeopleDataProvider) and COMPANY_ENRICHMENT
(fallback for company LinkedIn, additive alongside the mocks ONLY when
Explorium is not configured — see Phase 22 below).

Phase 22: EXPLORIUM_API_KEY being configured now also drops the mocks'
COMPANY_ENRICHMENT role (not just COMPANY_DISCOVERY) — both mock
enrichment providers return the exact same fixed fake payload for every
company regardless of its real domain, which conflicts with and drowns
out Explorium's own real evidence rather than merely padding it.

Exercises build_default_registry() directly (not the module-level
default_registry singleton) so each test can vary Settings without mutating
global state other tests depend on.
"""
from app.core.config import Settings
from app.providers.apollo import ApolloPersonEnrichmentProvider
from app.providers.contracts import ProviderCapability
from app.providers.default_registry import build_default_registry
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.providers.mocks import MockCompanyDataProvider, MockCompanyRegistryProvider, MockPeopleDataProvider
from app.providers.unipile import UnipileProvider


def _settings(
    apollo_api_key: str | None = None,
    explorium_api_key: str | None = None,
    unipile_api_key: str | None = None,
    unipile_dsn: str | None = None,
    unipile_account_id: str | None = None,
    tavily_api_key: str | None = None,
    serper_api_key: str | None = None,
    abstract_email_api_key: str | None = None,
    abstract_phone_api_key: str | None = None,
) -> Settings:
    # Every credential field is explicitly passed (never left to Settings'
    # own env_file=".env" default) so these registry tests stay hermetic
    # regardless of what a developer's local .env actually has configured
    # — a real TAVILY_API_KEY/SERPER_API_KEY (or any other provider key)
    # present in .env for live manual testing must never change what a
    # test asserts is registered.
    return Settings(
        apollo_api_key=apollo_api_key,
        explorium_api_key=explorium_api_key,
        unipile_api_key=unipile_api_key,
        unipile_dsn=unipile_dsn,
        unipile_account_id=unipile_account_id,
        tavily_api_key=tavily_api_key,
        serper_api_key=serper_api_key,
        abstract_email_api_key=abstract_email_api_key,
        abstract_phone_api_key=abstract_phone_api_key,
        database_url="sqlite:///:memory:",
    )


def _unipile_settings() -> Settings:
    return _settings(unipile_api_key="u-key", unipile_dsn="https://api8.unipile.com:13111", unipile_account_id="acct-1")


def test_no_explorium_key_registers_only_mock_company_discovery():
    registry = build_default_registry(_settings())
    providers = registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)

    assert len(providers) == 1
    assert isinstance(providers[0], MockCompanyDataProvider)


def test_explorium_key_replaces_not_merges_mock_for_company_discovery():
    """Phase 22 correction: a real, paying discovery call must never be
    silently mixed with the mock's 2 fixed fake companies — this now
    mirrors Phase 7B's own PEOPLE_DISCOVERY replace-not-merge rule, not
    the original (incorrect) additive design."""
    registry = build_default_registry(_settings(explorium_api_key="real-key"))
    providers = registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
    provider_ids = {p.provider_id for p in providers}

    assert provider_ids == {"explorium-company-discovery-v1"}
    assert any(isinstance(p, ExploriumCompanyDiscoveryProvider) for p in providers)
    assert not any(isinstance(p, MockCompanyDataProvider) for p in providers)


def test_apollo_never_appears_under_company_discovery_regardless_of_config():
    """The corrected architecture rule: Apollo is PERSON_ENRICHMENT only.
    Configuring APOLLO_API_KEY (with or without Explorium) must never
    register anything under COMPANY_DISCOVERY."""
    with_both = build_default_registry(_settings(apollo_api_key="a-key", explorium_api_key="e-key"))
    with_apollo_only = build_default_registry(_settings(apollo_api_key="a-key"))

    for registry in (with_both, with_apollo_only):
        providers = registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
        assert not any(isinstance(p, ApolloPersonEnrichmentProvider) for p in providers)
        assert not any("apollo" in p.provider_id for p in providers)


def test_explorium_key_alone_does_not_register_person_enrichment():
    registry = build_default_registry(_settings(explorium_api_key="e-key"))
    providers = registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)

    assert not any(isinstance(p, ApolloPersonEnrichmentProvider) for p in providers)


def test_apollo_key_registers_person_enrichment_independently_of_explorium():
    registry = build_default_registry(_settings(apollo_api_key="a-key"))
    providers = registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)

    assert any(isinstance(p, ApolloPersonEnrichmentProvider) for p in providers)


def test_no_apollo_key_leaves_person_enrichment_mock_only():
    registry = build_default_registry(_settings())
    providers = registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)

    assert not any(isinstance(p, ApolloPersonEnrichmentProvider) for p in providers)


def test_both_keys_configured_independently_gate_their_own_capability():
    registry = build_default_registry(_settings(apollo_api_key="a-key", explorium_api_key="e-key"))

    discovery_providers = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)}
    enrichment_providers = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)}

    assert "explorium-company-discovery-v1" in discovery_providers
    assert "apollo-person-enrichment-v1" in enrichment_providers
    # Neither provider crosses into the other's capability.
    assert "apollo-person-enrichment-v1" not in discovery_providers
    assert "explorium-company-discovery-v1" not in enrichment_providers


def test_no_explorium_key_leaves_company_enrichment_mock_only():
    registry = build_default_registry(_settings())
    providers = registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)
    provider_ids = {p.provider_id for p in providers}

    assert provider_ids == {"mock-company-data-v1", "mock-company-registry-v1"}


def test_explorium_key_replaces_not_merges_mock_for_company_enrichment_too():
    """Phase 22 correction (part 2): both mock enrichment providers return
    the exact same fixed fake payload regardless of the real company's
    domain — enriching a real Explorium company with it doesn't just add
    noise, it conflicts with Explorium's own real evidence and silently
    turns a resolvable industry/country signal into a permanent HOLD. So
    this is replace-not-merge too, exactly like COMPANY_DISCOVERY, not
    "unaffected" as an earlier version of this test asserted."""
    registry = build_default_registry(_settings(explorium_api_key="e-key"))
    providers = registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)

    assert providers == ()
    assert not any(isinstance(p, MockCompanyDataProvider) for p in providers)
    assert not any(isinstance(p, MockCompanyRegistryProvider) for p in providers)


def test_explorium_plus_unipile_leaves_only_unipile_for_company_enrichment():
    """With both Explorium and Unipile configured, the mocks are excluded
    (per the test above) and Unipile's real LinkedIn-fallback enrichment is
    the only COMPANY_ENRICHMENT provider left — never merged with fake
    mock data."""
    registry = build_default_registry(
        _settings(
            explorium_api_key="e-key",
            unipile_api_key="u-key",
            unipile_dsn="https://api8.unipile.com:13111",
            unipile_account_id="acct-1",
        )
    )
    providers = registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)
    provider_ids = {p.provider_id for p in providers}

    assert provider_ids == {"unipile-linkedin-v1"}


# --- Phase 7B: Unipile ------------------------------------------------


def test_no_unipile_config_registers_only_mock_people_discovery():
    registry = build_default_registry(_settings())
    providers = registry.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)

    assert len(providers) == 1
    assert isinstance(providers[0], MockPeopleDataProvider)


def test_full_unipile_config_replaces_mock_never_merges_for_people_discovery():
    """Explicit Phase 7B rule: real Unipile and mock people data must never
    run together for PEOPLE_DISCOVERY — unlike Explorium's additive
    registration for COMPANY_DISCOVERY."""
    registry = build_default_registry(_unipile_settings())
    providers = registry.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)

    assert len(providers) == 1
    assert isinstance(providers[0], UnipileProvider)
    assert not any(isinstance(p, MockPeopleDataProvider) for p in providers)


def test_partial_unipile_config_falls_back_to_mock_never_a_broken_real_provider():
    """All three of api_key/dsn/account_id must be set together — any one
    missing means the mock still runs, never a half-configured real
    provider that would fail every call."""
    partial_configs = [
        _settings(unipile_api_key="u-key"),
        _settings(unipile_api_key="u-key", unipile_dsn="https://api8.unipile.com:13111"),
        _settings(unipile_dsn="https://api8.unipile.com:13111", unipile_account_id="acct-1"),
        _settings(unipile_account_id="acct-1"),
    ]
    for settings in partial_configs:
        registry = build_default_registry(settings)
        providers = registry.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)
        assert len(providers) == 1
        assert isinstance(providers[0], MockPeopleDataProvider)
        assert not any(isinstance(p, UnipileProvider) for p in providers)


def test_unipile_additively_registers_company_enrichment_alongside_mocks():
    """Unlike PEOPLE_DISCOVERY, COMPANY_ENRICHMENT is additive — Unipile is
    only a fallback for company LinkedIn, never a replacement for the
    existing mock enrichment providers. This holds only when Explorium is
    NOT configured (an all-mock environment) — see
    test_explorium_plus_unipile_leaves_only_unipile_for_company_enrichment
    for the Explorium-configured case, where the mocks are dropped."""
    registry = build_default_registry(_unipile_settings())
    providers = registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)
    provider_ids = {p.provider_id for p in providers}

    assert "mock-company-data-v1" in provider_ids
    assert "mock-company-registry-v1" in provider_ids
    assert "unipile-linkedin-v1" in provider_ids


def test_no_unipile_config_leaves_company_enrichment_mock_only():
    registry = build_default_registry(_settings())
    providers = registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)

    assert not any(isinstance(p, UnipileProvider) for p in providers)


def test_unipile_never_appears_under_company_discovery_or_person_enrichment():
    registry = build_default_registry(_unipile_settings())

    discovery_providers = registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
    person_enrichment_providers = registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)

    assert not any(isinstance(p, UnipileProvider) for p in discovery_providers)
    assert not any(isinstance(p, UnipileProvider) for p in person_enrichment_providers)


def test_apollo_person_enrichment_provider_itself_is_completely_unaffected_by_unipile_config():
    """Apollo PERSON_ENRICHMENT ("Unlock Contacts") must remain untouched by
    Phase 7B — configuring Unipile must not change whether/how Apollo is
    registered. (MockPeopleDataProvider is also a PERSON_ENRICHMENT provider,
    per Phase 9 scaffolding — see app/services/person_enrichment.py's own
    real-provider filter, which already excludes it from actually running.
    It is present without Unipile and absent with Unipile purely because
    Phase 7B's PEOPLE_DISCOVERY replace-not-merge rule removes the whole
    MockPeopleDataProvider registration, not because Apollo's own
    registration changed — this test asserts the Apollo-specific invariant
    directly instead of comparing the full set.)"""
    with_unipile = build_default_registry(
        _settings(
            apollo_api_key="a-key",
            unipile_api_key="u-key",
            unipile_dsn="https://api8.unipile.com:13111",
            unipile_account_id="acct-1",
        )
    )
    without_unipile = build_default_registry(_settings(apollo_api_key="a-key"))

    for registry in (with_unipile, without_unipile):
        providers = registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)
        assert any(isinstance(p, ApolloPersonEnrichmentProvider) for p in providers)
        assert sum(isinstance(p, ApolloPersonEnrichmentProvider) for p in providers) == 1


def test_unipile_and_explorium_gate_independently_of_each_other():
    unipile_only = build_default_registry(_unipile_settings())
    explorium_only = build_default_registry(_settings(explorium_api_key="e-key"))

    assert any(
        isinstance(p, UnipileProvider) for p in unipile_only.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)
    )
    assert not any(
        isinstance(p, ExploriumCompanyDiscoveryProvider)
        for p in unipile_only.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
    )
    assert any(
        isinstance(p, ExploriumCompanyDiscoveryProvider)
        for p in explorium_only.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)
    )
    assert not any(
        isinstance(p, UnipileProvider) for p in explorium_only.find_by_capability(ProviderCapability.PEOPLE_DISCOVERY)
    )


# --- Abstract API email verification (second PERSON_ENRICHMENT provider) ---


def test_abstract_email_verification_is_absent_without_its_own_key():
    registry = build_default_registry(_settings(apollo_api_key="a-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)}
    assert "abstract-email-verification-v1" not in provider_ids


def test_abstract_email_verification_registers_alongside_apollo_never_replacing_it():
    registry = build_default_registry(_settings(apollo_api_key="a-key", abstract_email_api_key="ab-key"))
    provider_ids = [p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)]
    assert "apollo-person-enrichment-v1" in provider_ids
    assert "abstract-email-verification-v1" in provider_ids
    # Registration order matters (see app/services/person_enrichment.py's
    # own carry-forward docstring) — Abstract must come after Apollo so it
    # can see an email Apollo just found in the same enrichment pass.
    assert provider_ids.index("apollo-person-enrichment-v1") < provider_ids.index("abstract-email-verification-v1")


def test_abstract_email_verification_works_without_apollo_too():
    """No hard dependency on Apollo being configured — it simply verifies
    whatever query.email it's given, which may come from a prior run's
    already-persisted evidence instead (see app/api/people.py)."""
    registry = build_default_registry(_settings(abstract_email_api_key="ab-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)}
    assert "abstract-email-verification-v1" in provider_ids


# --- Abstract API phone verification (third PERSON_ENRICHMENT provider) ---


def test_abstract_phone_verification_is_absent_without_its_own_key():
    registry = build_default_registry(_settings(apollo_api_key="a-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)}
    assert "abstract-phone-verification-v1" not in provider_ids


def test_abstract_phone_verification_registers_alongside_apollo_and_email_verification():
    registry = build_default_registry(
        _settings(apollo_api_key="a-key", abstract_email_api_key="ab-e-key", abstract_phone_api_key="ab-p-key")
    )
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)}
    assert "apollo-person-enrichment-v1" in provider_ids
    assert "abstract-email-verification-v1" in provider_ids
    assert "abstract-phone-verification-v1" in provider_ids


def test_abstract_phone_verification_works_standalone_too():
    registry = build_default_registry(_settings(abstract_phone_api_key="ab-p-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)}
    assert "abstract-phone-verification-v1" in provider_ids
