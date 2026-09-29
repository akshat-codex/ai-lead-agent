import pytest

from app.providers.base import ProviderAdapter, ProviderErrorCode
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.mocks import MockWebSearchProvider
from app.providers.registry import ProviderNotFoundError, ProviderRegistry
from datetime import datetime, timezone


class _StubProvider(ProviderAdapter):
    """A minimal adapter for exercising the registry/base plumbing directly,
    independent of the real mocks' fake business data."""

    def __init__(self, provider_id: str, capability: ProviderCapability, label: str):
        super().__init__(provider_id=provider_id, provider_name=label, capabilities={capability})
        self.label = label
        self.call_count = 0

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(NormalizedRecord(external_id="1", name=self.label, attributes={}),),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc),
                is_mock=True,
            ),
        )


class _BrokenProvider(ProviderAdapter):
    """Raises unconditionally, to prove run() isolates callers from a
    misbehaving adapter instead of propagating the exception."""

    def __init__(self, provider_id: str = "broken-provider"):
        super().__init__(provider_id=provider_id, provider_name="Broken", capabilities={ProviderCapability.WEB_SEARCH})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("vendor SDK exploded")


# --- registration ----------------------------------------------------------


def test_register_and_get_provider():
    registry = ProviderRegistry()
    provider = _StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A")
    registry.register(provider)
    assert registry.get("stub-a") is provider


def test_duplicate_registration_raises():
    registry = ProviderRegistry()
    registry.register(_StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A"))
    with pytest.raises(ValueError):
        registry.register(_StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A duplicate"))


def test_get_unknown_provider_raises():
    registry = ProviderRegistry()
    with pytest.raises(ProviderNotFoundError):
        registry.get("does-not-exist")


def test_unregister_removes_provider():
    registry = ProviderRegistry()
    registry.register(_StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A"))
    registry.unregister("stub-a")
    with pytest.raises(ProviderNotFoundError):
        registry.get("stub-a")


def test_list_providers_returns_everything_registered():
    registry = ProviderRegistry()
    registry.register(_StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A"))
    registry.register(_StubProvider("stub-b", ProviderCapability.COMPANY_DISCOVERY, "Stub B"))
    ids = {p.provider_id for p in registry.list_providers()}
    assert ids == {"stub-a", "stub-b"}


# --- capability lookup -------------------------------------------------


def test_find_by_capability_returns_only_matching_providers():
    registry = ProviderRegistry()
    web = _StubProvider("stub-web", ProviderCapability.WEB_SEARCH, "Web")
    company = _StubProvider("stub-company", ProviderCapability.COMPANY_DISCOVERY, "Company")
    registry.register(web)
    registry.register(company)

    result = registry.find_by_capability(ProviderCapability.WEB_SEARCH)
    assert result == (web,)


def test_find_by_capability_with_no_match_returns_empty_tuple_not_error():
    registry = ProviderRegistry()
    registry.register(_StubProvider("stub-web", ProviderCapability.WEB_SEARCH, "Web"))
    result = registry.find_by_capability(ProviderCapability.PERSON_ENRICHMENT)
    assert result == ()


def test_multiple_providers_can_support_the_same_capability():
    registry = ProviderRegistry()
    a = _StubProvider("a", ProviderCapability.WEB_SEARCH, "A")
    b = _StubProvider("b", ProviderCapability.WEB_SEARCH, "B")
    registry.register(a)
    registry.register(b)
    result = registry.find_by_capability(ProviderCapability.WEB_SEARCH)
    assert set(result) == {a, b}


# --- unsupported capability handling ------------------------------------


def test_run_with_unsupported_capability_returns_clean_error_without_calling_execute():
    provider = _StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A")
    request = ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY)

    response = provider.run(request)

    assert response.success is False
    assert response.error.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert provider.call_count == 0  # execute() was never reached


# --- provider isolation ----------------------------------------------------


def test_a_broken_provider_returns_clean_error_instead_of_raising():
    provider = _BrokenProvider()
    request = ProviderRequest(capability=ProviderCapability.WEB_SEARCH)

    response = provider.run(request)  # must not raise

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR
    assert "vendor SDK exploded" in response.error.message


def test_a_broken_provider_does_not_affect_other_registered_providers():
    registry = ProviderRegistry()
    broken = _BrokenProvider("broken")
    healthy = _StubProvider("healthy", ProviderCapability.WEB_SEARCH, "Healthy")
    registry.register(broken)
    registry.register(healthy)

    broken_response = registry.get("broken").run(ProviderRequest(capability=ProviderCapability.WEB_SEARCH))
    healthy_response = registry.get("healthy").run(ProviderRequest(capability=ProviderCapability.WEB_SEARCH))

    assert broken_response.success is False
    assert healthy_response.success is True


def test_run_measures_and_attaches_latency():
    provider = _StubProvider("stub-a", ProviderCapability.WEB_SEARCH, "Stub A")
    response = provider.run(ProviderRequest(capability=ProviderCapability.WEB_SEARCH))
    assert response.latency_ms is not None
    assert response.latency_ms >= 0


# --- provider replacement without core-code changes ------------------------


def _call_registered_provider(registry: ProviderRegistry, provider_id: str) -> ProviderResponse:
    """Stand-in for "core pipeline code" that only ever knows a provider_id
    and a capability — never a concrete adapter class."""
    return registry.get(provider_id).run(ProviderRequest(capability=ProviderCapability.WEB_SEARCH))


def test_replace_swaps_behavior_for_callers_using_only_the_provider_id():
    registry = ProviderRegistry()
    registry.register(_StubProvider("web-provider", ProviderCapability.WEB_SEARCH, "Old Vendor"))

    before = _call_registered_provider(registry, "web-provider")
    assert before.data[0].name == "Old Vendor"

    registry.replace(_StubProvider("web-provider", ProviderCapability.WEB_SEARCH, "New Vendor"))

    after = _call_registered_provider(registry, "web-provider")
    assert after.data[0].name == "New Vendor"


def test_replace_can_swap_a_mock_provider_for_a_different_implementation():
    registry = ProviderRegistry()
    registry.register(MockWebSearchProvider("web-provider"))
    first = _call_registered_provider(registry, "web-provider")
    assert first.source.is_mock is True

    registry.replace(_StubProvider("web-provider", ProviderCapability.WEB_SEARCH, "Replacement"))
    second = _call_registered_provider(registry, "web-provider")
    assert second.data[0].name == "Replacement"
