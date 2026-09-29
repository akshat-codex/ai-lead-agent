"""GET /api/v1/provider-usage — honest, per-vendor usage/balance
visibility. See app/api/provider_usage.py's own module docstring: Tavily
is the only vendor with a real, documented usage-check endpoint; the
other three are reported as unavailable, never guessed or omitted.
"""
import httpx
import respx
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

client = TestClient(app)


def test_all_four_providers_always_listed_regardless_of_configuration():
    response = client.get("/api/v1/provider-usage")
    assert response.status_code == 200
    body = response.json()
    provider_ids = {entry["provider_id"] for entry in body}
    assert provider_ids == {
        "tavily-company-discovery-v1",
        "explorium-company-discovery-v1",
        "gemini-llm-v1",
        "serper-company-discovery-v1",
    }


def test_explorium_gemini_serper_always_reported_unavailable_with_a_reason():
    body = client.get("/api/v1/provider-usage").json()
    by_id = {entry["provider_id"]: entry for entry in body}
    for provider_id in ("explorium-company-discovery-v1", "gemini-llm-v1", "serper-company-discovery-v1"):
        entry = by_id[provider_id]
        assert entry["available"] is False
        assert entry["usage"] is None
        assert entry["unavailable_reason"]


def test_tavily_unavailable_when_key_not_configured():
    body = client.get("/api/v1/provider-usage").json()
    tavily = next(e for e in body if e["provider_id"] == "tavily-company-discovery-v1")
    assert not get_settings().tavily_api_key  # test env has no real key configured (see conftest.py's blanking)
    assert tavily["available"] is False
    assert tavily["usage"] is None
    assert "TAVILY_API_KEY" in tavily["unavailable_reason"]


@respx.mock
def test_tavily_available_and_returns_raw_usage_body_when_key_configured(monkeypatch):
    from app.core import config as config_module

    monkeypatch.setattr(config_module, "get_settings", lambda: config_module.Settings(tavily_api_key="test-key", database_url="sqlite:///:memory:"))
    import app.api.provider_usage as provider_usage_module

    monkeypatch.setattr(provider_usage_module, "get_settings", config_module.get_settings)

    respx.get("https://api.tavily.com/usage").mock(
        return_value=httpx.Response(200, json={"key": {"usage": 42, "limit": 1000}, "account": {"current_plan": "free"}})
    )

    body = client.get("/api/v1/provider-usage").json()
    tavily = next(e for e in body if e["provider_id"] == "tavily-company-discovery-v1")
    assert tavily["available"] is True
    assert tavily["usage"] == {"key": {"usage": 42, "limit": 1000}, "account": {"current_plan": "free"}}


@respx.mock
def test_tavily_reports_unavailable_honestly_on_http_failure(monkeypatch):
    from app.core import config as config_module

    monkeypatch.setattr(config_module, "get_settings", lambda: config_module.Settings(tavily_api_key="bad-key", database_url="sqlite:///:memory:"))
    import app.api.provider_usage as provider_usage_module

    monkeypatch.setattr(provider_usage_module, "get_settings", config_module.get_settings)

    respx.get("https://api.tavily.com/usage").mock(return_value=httpx.Response(401, json={"error": "invalid key"}))

    body = client.get("/api/v1/provider-usage").json()
    tavily = next(e for e in body if e["provider_id"] == "tavily-company-discovery-v1")
    assert tavily["available"] is False
    assert tavily["usage"] is None
    assert tavily["unavailable_reason"]
