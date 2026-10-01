"""Unit tests for TechStackDetectorProvider — a real, free, no-API-key
COMPANY_ENRICHMENT source that fingerprints a company's homepage against a
small, curated table of unambiguous platform signatures. See
app/providers/tech_stack_detector.py's own module docstring for the
"passive fingerprint, not a stack audit" scope.

Every HTTP call is respx-mocked — no live network calls.
"""
import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.tech_stack_detector import (
    TECH_STACK_FETCH_FAILED,
    TECH_STACK_NO_DOMAIN_SUPPLIED,
    TECH_STACK_NO_MATCH,
    TechStackDetectorProvider,
)

_PADDING = "Lorem ipsum dolor sit amet consectetur. " * 20  # clears the 200-char min-content bar


def _provider() -> TechStackDetectorProvider:
    return TechStackDetectorProvider()


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_ENRICHMENT, query=query)


@respx.mock
def test_shopify_signature_is_detected():
    respx.get("https://acme.invalid").mock(
        return_value=httpx.Response(200, text=f"<html><head><script src='https://cdn.shopify.com/s/files/1/foo.js'></script></head><body>{_PADDING}</body></html>")
    )

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.success is True
    assert response.data[0].attributes == {"ecommerce_platform": "Shopify"}
    assert response.source.is_mock is False


@respx.mock
def test_wordpress_signature_is_detected():
    respx.get("https://acme.invalid").mock(
        return_value=httpx.Response(200, text=f"<html><body>{_PADDING}<link rel='stylesheet' href='/wp-content/themes/foo/style.css'></body></html>")
    )

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.success is True
    assert response.data[0].attributes["cms_platform"] == "WordPress"


@respx.mock
def test_multiple_categories_can_match_independently():
    respx.get("https://acme.invalid").mock(
        return_value=httpx.Response(
            200,
            text=f"<html><body>{_PADDING}<script src='https://cdn.shopify.com/x.js'></script><script src='https://js.hs-scripts.com/123.js'></script></body></html>",
        )
    )

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.data[0].attributes == {
        "ecommerce_platform": "Shopify",
        "marketing_crm_platform": "HubSpot",
    }


@respx.mock
def test_bare_product_name_in_prose_is_never_a_false_positive():
    """A page that merely TALKS ABOUT a platform (e.g. a blog post
    mentioning "Shopify" in prose) must never be mistaken for a site
    actually built on it — only a real, unambiguous asset-path/script
    fingerprint counts."""
    respx.get("https://acme.invalid").mock(
        return_value=httpx.Response(200, text=f"<html><body>{_PADDING} We compare Shopify and WordPress in this article about ecommerce platforms.</body></html>")
    )

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.success is False
    assert response.error.code == TECH_STACK_NO_MATCH


@respx.mock
def test_no_signature_match_is_an_honest_no_match():
    respx.get("https://acme.invalid").mock(return_value=httpx.Response(200, text=f"<html><body>{_PADDING}</body></html>"))

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.success is False
    assert response.error.code == TECH_STACK_NO_MATCH
    assert response.error.retryable is False


@respx.mock
def test_no_domain_supplied_is_an_honest_failure():
    provider = _provider()
    response = provider.run(_request())
    assert response.success is False
    assert response.error.code == TECH_STACK_NO_DOMAIN_SUPPLIED


@respx.mock
def test_homepage_fetch_failure_is_reported_distinctly_from_no_match():
    respx.get("https://acme.invalid").mock(return_value=httpx.Response(404))

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.success is False
    assert response.error.code == TECH_STACK_FETCH_FAILED


@respx.mock
def test_block_page_is_never_scanned_for_signatures():
    """A CAPTCHA/anti-bot block page must never be mistaken for real site
    content, even if it happened to mention a platform name — reuses
    homepage_fetch.py's own block-page detection."""
    respx.get("https://acme.invalid").mock(
        return_value=httpx.Response(200, text="Access Denied. You have been blocked. " + "Shopify cdn.shopify.com " * 20)
    )

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert response.success is False
    assert response.error.code == TECH_STACK_FETCH_FAILED


@respx.mock
def test_domain_without_scheme_is_fetched_over_https():
    route = respx.get("https://acme.invalid").mock(
        return_value=httpx.Response(200, text=f"<html><body>{_PADDING}<script src='https://cdn.shopify.com/x.js'></script></body></html>")
    )

    provider = _provider()
    response = provider.run(_request(domain="acme.invalid"))

    assert route.called
    assert response.success is True
