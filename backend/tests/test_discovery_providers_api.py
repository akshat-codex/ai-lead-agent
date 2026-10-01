"""GET /api/v1/discovery-providers — real, honest COMPANY_DISCOVERY
provider configuration status for the provider-picker UI.

Deliberately never asserts a fabricated "accuracy %" anywhere — this
endpoint only ever reports (a) whether a provider is configured right now
and (b) static, factual descriptions of what kind of evidence it
contributes. See app/api/discovery_providers.py's own module docstring.
"""
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import ProviderCapability
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry

client = TestClient(app)


def _with_registry(fn, registry: ProviderRegistry):
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        return fn()
    finally:
        del app.dependency_overrides[get_provider_registry]


class _FakeDiscoveryProvider(ProviderAdapter):
    def __init__(self, provider_id: str):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})

    def execute(self, request):  # pragma: no cover - never called by these tests
        raise NotImplementedError


def test_all_four_known_providers_always_listed_regardless_of_configuration():
    response = client.get("/api/v1/discovery-providers")
    assert response.status_code == 200
    body = response.json()
    provider_ids = {entry["provider_id"] for entry in body}
    assert provider_ids == {
        "explorium-company-discovery-v1",
        "tavily-company-discovery-v1",
        "serper-company-discovery-v1",
        "hermes-icp-search-v1",
    }


def test_no_real_provider_configured_reports_every_entry_unconfigured():
    empty_registry = ProviderRegistry()
    body = _with_registry(lambda: client.get("/api/v1/discovery-providers").json(), empty_registry)
    assert all(entry["configured"] is False for entry in body)


def test_a_registered_real_provider_is_reported_configured():
    registry = ProviderRegistry()
    registry.register(_FakeDiscoveryProvider("tavily-company-discovery-v1"))
    body = _with_registry(lambda: client.get("/api/v1/discovery-providers").json(), registry)
    by_id = {entry["provider_id"]: entry for entry in body}
    assert by_id["tavily-company-discovery-v1"]["configured"] is True
    # Every OTHER provider is unaffected by one being configured.
    assert by_id["serper-company-discovery-v1"]["configured"] is False
    assert by_id["explorium-company-discovery-v1"]["configured"] is False
    assert by_id["hermes-icp-search-v1"]["configured"] is False


def test_a_mock_prefixed_provider_is_never_reported_configured():
    """A mock standing in for a COMPANY_DISCOVERY provider (e.g.
    mock-company-data-v1, no real key configured) must never be mistaken
    for the real thing this endpoint reports on."""
    registry = ProviderRegistry()
    registry.register(_FakeDiscoveryProvider("mock-company-data-v1"))
    body = _with_registry(lambda: client.get("/api/v1/discovery-providers").json(), registry)
    assert all(entry["configured"] is False for entry in body)


def test_every_entry_has_a_non_empty_description_and_env_var_list():
    response = client.get("/api/v1/discovery-providers")
    for entry in response.json():
        assert entry["description"]
        assert entry["env_vars"]
        assert entry["evidence_kind"] in {"structured", "web_search", "research_agent"}


def test_response_never_contains_an_accuracy_percentage_field():
    """Guards the explicit design decision (see module docstring): this
    endpoint must never fabricate a per-provider accuracy/quality score."""
    response = client.get("/api/v1/discovery-providers")
    for entry in response.json():
        assert "accuracy" not in entry
        assert "accuracy_percent" not in entry
