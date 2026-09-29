"""provider_call_counts / estimated_explorium_credits — real, honest
per-batch cost visibility (no fabricated dollar amounts; see
app/schemas/batch.py::BatchDetailRead's own docstrings for both fields).
"""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.providers.contracts import ProviderCapability, ProviderRequest, ProviderResponse
from app.providers.base import ProviderAdapter
from app.providers.contracts import NormalizedRecord, SourceMetadata
from app.providers.registry import ProviderRegistry
from app.providers.default_registry import get_provider_registry
from app.core.config import get_settings

client = TestClient(app)


def _icp_payload(name: str) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Healthcare"], "geography": ["United States"], "min_employees": 1, "max_employees": 10000,
            "allowed_titles": [], "company_type": [], "exclusions": [], "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [], "growth_signals": [],
            "marketing_signals": [], "other_preferences": [],
        },
    }


def _create_icp(name: str) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name)).json()


class _FakeExploriumProvider(ProviderAdapter):
    """Stands in for the real Explorium adapter with the SAME provider_id
    it uses in production — provider_call_counts/estimated_explorium_credits
    key off that id, so this must match exactly for the estimate to apply."""

    def __init__(self, records):
        super().__init__(
            provider_id="explorium-company-discovery-v1",
            provider_name="Explorium",
            capabilities={ProviderCapability.COMPANY_DISCOVERY},
        )
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
            exhausted=True,
        )


def _with_registry(registry, fn):
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        return fn()
    finally:
        app.dependency_overrides.pop(get_provider_registry, None)


def test_provider_call_counts_reflects_real_explorium_calls():
    icp = _create_icp("Cost Visibility A")
    registry = ProviderRegistry()
    registry.register(_FakeExploriumProvider([NormalizedRecord(external_id="e1", name="Real Health Co", attributes={"domain": "realhealth.com"})]))

    body = _with_registry(registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 1})).json()

    assert body["provider_call_counts"].get("explorium-company-discovery-v1") == 1


def test_estimated_explorium_credits_uses_returned_count_and_configured_rate():
    icp = _create_icp("Cost Visibility B")
    registry = ProviderRegistry()
    registry.register(
        _FakeExploriumProvider(
            [
                NormalizedRecord(external_id="e1", name="Co One", attributes={"domain": "coone.com"}),
                NormalizedRecord(external_id="e2", name="Co Two", attributes={"domain": "cotwo.com"}),
            ]
        )
    )

    body = _with_registry(registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 2})).json()

    settings = get_settings()
    expected = 2 * settings.explorium_estimated_credits_per_company
    assert body["estimated_explorium_credits"] == expected


def test_estimated_explorium_credits_is_none_when_explorium_never_called():
    icp = _create_icp("Cost Visibility C")
    registry = ProviderRegistry()  # no COMPANY_DISCOVERY provider registered at all

    body = _with_registry(registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 1})).json()

    assert body["estimated_explorium_credits"] is None
