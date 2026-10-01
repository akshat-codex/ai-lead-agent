"""The application's shared provider registry.

Populated once, at import time, with the Phase 5 mock providers. Swapping a
mock for a real provider later means changing only this file — nothing in
app/services/company_discovery.py or app/api/discovery.py needs to change,
which is the entire point of the Phase 5 abstraction.

Part B Phase 5 (Contact Enrichment): ApolloPersonEnrichmentProvider is
registered only when APOLLO_API_KEY is actually configured, so an
environment without credentials never silently ends up with a provider that
would fail every real call — PERSON_ENRICHMENT requests then honestly report
UNAVAILABLE (see app/services/person_enrichment.py) rather than looking like
a working integration. Apollo is scoped to PERSON_ENRICHMENT ONLY — it is the
"Unlock Contacts" flow, triggered explicitly per selected person, and must
never be used for company discovery (see the approved provider architecture:
Explorium = COMPANY_DISCOVERY, Unipile = LinkedIn/PEOPLE_DISCOVERY (not yet
implemented), Apollo = PERSON_ENRICHMENT only).

AbstractEmailVerificationProvider (see app/providers/abstract_email_verification.py)
is a SECOND, independent PERSON_ENRICHMENT provider, registered only when
ABSTRACT_EMAIL_API_KEY is configured, and always AFTER Apollo when both are
present — it verifies an already-known email's live deliverability rather
than discovering one, and app/services/person_enrichment.py's own
run_person_enrichment carries a newly-found email forward within one pass
so this provider can act on an email Apollo just found, not only one a
prior run already persisted as evidence.

Part B Phase 7A (Real Company Discovery, corrected): ExploriumCompanyDiscoveryProvider
is registered only when EXPLORIUM_API_KEY is configured.

Phase 22 correction: MockCompanyDataProvider's COMPANY_DISCOVERY role is now
REPLACE-not-merge when Explorium is configured — mirroring Phase 7B's own
PEOPLE_DISCOVERY rule below, not the original (incorrect) design. A real,
paying production discovery call must never have its results silently mixed
with the mock's 2 fixed fake companies ("Example Test Co"/"Sample Widgets
Inc") — a user searching real Explorium data has no way to tell a fake
result from a real one once merged.

Phase 22 correction (part 2): MockCompanyDataProvider's and
MockCompanyRegistryProvider's COMPANY_ENRICHMENT role is ALSO
REPLACE-not-merge when Explorium is configured, for the same reason —
verified live: both mocks return the exact same fixed fake payload
("Skincare"/"D2C"/"Subscription") for every company regardless of its real
domain (see app/providers/mocks.py's own docstring: MockCompanyRegistryProvider
exists "to exercise multi-provider enrichment and conflicting-value
handling," a test fixture, not real data). Enriching a real Explorium
company with this fixed fake data doesn't just add noise — it actively
CONFLICTS with Explorium's own real industry/country evidence in
app/services/evidence_engine.py (two disagreeing values -> CONFLICT, which
Phase 12's hard-rule engine reads as unresolved), silently turning a
resolvable real industry/country signal into a permanent HOLD. When
Explorium is configured, real COMPANY_ENRICHMENT for LinkedIn fallback
still comes from UnipileProvider (below) when configured — the mocks are
excluded entirely, not replaced with a different mock; there is no real
alternative for company enrichment beyond Unipile today, and that's an
honest state (COMPANY_ENRICHMENT may simply return nothing for company
LinkedIn until Unipile is configured), not one requiring a fake fallback.

Part B Phase 7B (Real LinkedIn/People Discovery): UnipileProvider is
registered only when ALL THREE of UNIPILE_API_KEY, UNIPILE_DSN, and
UNIPILE_ACCOUNT_ID are configured together (Unipile requires a pre-connected
LinkedIn account, unlike Apollo/Explorium's simple API-key auth). Unlike
Explorium's additive-alongside-the-mock registration, MockPeopleDataProvider
is explicitly NOT registered for PEOPLE_DISCOVERY when Unipile is active —
real and mock people data must never be merged into one discovery run. The
mock is still registered (for PEOPLE_DISCOVERY only) when Unipile is not
configured, so tests and unconfigured environments keep exactly today's
behavior. UnipileProvider additionally supplies COMPANY_ENRICHMENT as a
fallback for company LinkedIn when Explorium doesn't provide one — per the
Phase 22 correction above, this runs alongside the mocks only when
Explorium is NOT configured (an all-mock environment); once Explorium is
configured, the mocks' COMPANY_ENRICHMENT role is dropped and Unipile (if
also configured) is the only COMPANY_ENRICHMENT provider left.
"""
from __future__ import annotations

from app.core.config import Settings, get_settings
from app.providers.abstract_email_verification import AbstractEmailVerificationProvider
from app.providers.abstract_phone_verification import AbstractPhoneVerificationProvider
from app.providers.apollo import ApolloPersonEnrichmentProvider
from app.providers.contracts import ProviderCapability
from app.providers.explorium import ExploriumCompanyDiscoveryProvider
from app.providers.mocks import (
    MockCompanyDataProvider,
    MockCompanyRegistryProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)
from app.providers.registry import ProviderRegistry
from app.providers.sec_edgar import SecEdgarCompanyEnrichmentProvider
from app.providers.serper import SerperCompanyDiscoveryProvider
from app.providers.signal_check import SignalCheckProvider
from app.providers.tavily import TavilyCompanyDiscoveryProvider
from app.providers.tech_stack_detector import TechStackDetectorProvider
from app.providers.unipile import UnipileProvider
from app.providers.wikidata import WikidataCompanyEnrichmentProvider


def build_default_registry(settings: Settings | None = None) -> ProviderRegistry:
    """`settings` defaults to the real app.core.config.get_settings() singleton;
    an explicit value is accepted so tests can exercise the conditional
    registration logic below without mutating global settings state."""
    registry = ProviderRegistry()
    mock_company_data_provider = MockCompanyDataProvider()
    mock_company_registry_provider = MockCompanyRegistryProvider()
    registry.register(mock_company_data_provider)
    registry.register(mock_company_registry_provider)
    registry.register(MockWebSearchProvider())

    settings = settings or get_settings()

    if settings.explorium_api_key:
        registry.register(
            ExploriumCompanyDiscoveryProvider(
                api_key=settings.explorium_api_key,
                base_url=settings.explorium_base_url,
            )
        )
        # Real discovery results must never be silently mixed with the
        # mocks' fixed fake data — drop COMPANY_DISCOVERY from
        # MockCompanyDataProvider (see module docstring).
        mock_company_data_provider.capabilities = frozenset(
            mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_DISCOVERY}
        )

    # Discovery-mode web-search sources (see app/providers/tavily.py /
    # app/providers/serper.py's own docstrings). Registered independently
    # of Explorium — a batch's discovery_mode (see app/models/batch.py)
    # decides which registered COMPANY_DISCOVERY providers actually get
    # called each round (app/services/batch_orchestration.py::
    # _run_one_discovery_round), not this registry. Real web-search
    # results must never be mixed with the mock's fixed fake companies
    # for the same reason Explorium's registration strips the mock's
    # COMPANY_DISCOVERY role above — if any real provider is configured,
    # the mock's COMPANY_DISCOVERY role is dropped exactly once (a no-op
    # frozenset subtraction if another real provider already dropped it).
    if settings.tavily_api_key:
        registry.register(TavilyCompanyDiscoveryProvider(api_key=settings.tavily_api_key, base_url=settings.tavily_base_url))
        mock_company_data_provider.capabilities = frozenset(mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_DISCOVERY})

    if settings.serper_api_key:
        registry.register(SerperCompanyDiscoveryProvider(api_key=settings.serper_api_key, base_url=settings.serper_base_url))
        mock_company_data_provider.capabilities = frozenset(mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_DISCOVERY})

    # Live-test bug found (2026-09-03 Safe-mode E2E): before this fix,
    # COMPANY_ENRICHMENT's mock-drop only ran inside the `if
    # settings.explorium_api_key:` branch above — a batch running Safe/
    # Hard mode with Tavily/Serper configured but NO Explorium key still
    # left both mock enrichment providers fully active. They ALWAYS
    # return the exact same fixed fake payload regardless of the real
    # company (see app/providers/mocks.py's own docstring:
    # industry="Skincare", company_type="D2C", business_model=
    # "Subscription" for every company), confirmed live: real Tavily
    # discovery results for Xbox Cloud Gaming / iCloud / OneDrive all
    # came back stamped with industry="Skincare" evidence from
    # mock-company-data-v1/mock-company-registry-v1. This must run
    # whenever ANY real COMPANY_DISCOVERY provider is active — Explorium,
    # Tavily, or Serper — not just Explorium; enriching a real company
    # (from any real discovery source) with fixed fake data doesn't just
    # add noise, it actively conflicts with that company's own real
    # evidence and can silently turn a resolvable industry/country signal
    # into a permanent HOLD (see module docstring, Phase 22 part 2, the
    # original version of this same fix for Explorium alone).
    if settings.explorium_api_key or settings.tavily_api_key or settings.serper_api_key:
        mock_company_data_provider.capabilities = frozenset(
            mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )
        mock_company_registry_provider.capabilities = frozenset(
            mock_company_registry_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )

    # SEC EDGAR + Wikidata are free, no-API-key COMPANY_ENRICHMENT sources
    # (see their own module docstrings for the honest "narrow coverage"
    # scope: public companies only / Wikidata-notable companies only) —
    # gated on their own explicit opt-in flag
    # (settings.enable_free_company_enrichment_providers), not "always on,"
    # because unlike every other provider in this file they have no API
    # key to be naturally absent in a test/default environment; making
    # them unconditional would mean every test that builds the default
    # registry silently makes real network calls (see that setting's own
    # comment in app/core/config.py). Same root cause as the fix above once
    # enabled: two disagreeing values for one field become an unresolved
    # CONFLICT (see app/services/evidence_engine.py), silently turning a
    # resolvable signal into a permanent HOLD — so the mock-drop for THESE
    # two providers only runs when they are actually registered, never
    # unconditionally, and is independent of (additional to) the
    # Explorium/Tavily/Serper-triggered drop directly above.
    if settings.enable_free_company_enrichment_providers:
        registry.register(SecEdgarCompanyEnrichmentProvider())
        registry.register(WikidataCompanyEnrichmentProvider())
        mock_company_data_provider.capabilities = frozenset(
            mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )
        mock_company_registry_provider.capabilities = frozenset(
            mock_company_registry_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )

    # Signal Check (see app/providers/signal_check.py's own module
    # docstring) — an additional, independent COMPANY_ENRICHMENT provider
    # that reuses settings.tavily_api_key, so it is only ever registered
    # when BOTH Tavily is configured AND the user has explicitly opted
    # into its extra per-company search cost via
    # settings.enable_signal_check_provider (see that setting's own
    # comment in app/core/config.py for why this needs its own flag even
    # though Tavily's key already gates every other Tavily-backed
    # provider). Same mock-drop rule as every other real COMPANY_ENRICHMENT
    # source in this file, for the identical reason (Phase 22 part 2):
    # this provider's real search-derived evidence must never be merged
    # with the mocks' fixed fake payload.
    if settings.tavily_api_key and settings.enable_signal_check_provider:
        registry.register(SignalCheckProvider(api_key=settings.tavily_api_key, base_url=settings.tavily_base_url))
        mock_company_data_provider.capabilities = frozenset(
            mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )
        mock_company_registry_provider.capabilities = frozenset(
            mock_company_registry_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )

    # Tech Stack Detector (see app/providers/tech_stack_detector.py's own
    # module docstring) — free, no API key, gated on its own explicit
    # opt-in flag for the identical reason as
    # enable_free_company_enrichment_providers above (no key to naturally
    # gate on; tests must never make a real network call unconditionally).
    # Same mock-drop rule as every other real COMPANY_ENRICHMENT source.
    if settings.enable_tech_stack_detector:
        registry.register(TechStackDetectorProvider())
        mock_company_data_provider.capabilities = frozenset(
            mock_company_data_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )
        mock_company_registry_provider.capabilities = frozenset(
            mock_company_registry_provider.capabilities - {ProviderCapability.COMPANY_ENRICHMENT}
        )

    unipile_configured = bool(settings.unipile_api_key and settings.unipile_dsn and settings.unipile_account_id)
    if unipile_configured:
        registry.register(
            UnipileProvider(
                api_key=settings.unipile_api_key,
                dsn=settings.unipile_dsn,
                account_id=settings.unipile_account_id,
            )
        )
    else:
        registry.register(MockPeopleDataProvider())

    if settings.apollo_api_key:
        registry.register(
            ApolloPersonEnrichmentProvider(
                api_key=settings.apollo_api_key,
                base_url=settings.apollo_base_url,
            )
        )

    # Registered AFTER Apollo, deliberately — see app/services/
    # person_enrichment.py::run_person_enrichment's own carry-forward
    # docstring: providers run in registration order, and this provider
    # verifies an email rather than discovering one, so it must run after
    # whatever provider(s) might supply a fresh email in this same pass.
    if settings.abstract_email_api_key:
        registry.register(
            AbstractEmailVerificationProvider(
                api_key=settings.abstract_email_api_key,
                base_url=settings.abstract_email_base_url,
            )
        )

    # No carry-forward ordering constraint (unlike email above) — no
    # provider in this codebase ever discovers a phone number in the same
    # pass (see PersonEnrichmentQuery.phone's own docstring), so this only
    # ever verifies a number a human already recorded as evidence.
    if settings.abstract_phone_api_key:
        registry.register(
            AbstractPhoneVerificationProvider(
                api_key=settings.abstract_phone_api_key,
                base_url=settings.abstract_phone_base_url,
            )
        )

    return registry


default_registry = build_default_registry()


def get_provider_registry() -> ProviderRegistry:
    """FastAPI dependency — overridable in tests via app.dependency_overrides,
    the same pattern used for get_db in app/db/session.py."""
    return default_registry
