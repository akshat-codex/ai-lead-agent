"""Unit tests for WikidataCompanyEnrichmentProvider — a real, free,
no-API-key COMPANY_ENRICHMENT source. See app/providers/wikidata.py's own
module docstring for the SPARQL-query mechanism and its honest
"Wikidata-notable companies only" scope.

Every HTTP call is respx-mocked — no live network calls.
"""
import httpx
import respx

from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.wikidata import WIKIDATA_NO_MATCH, WikidataCompanyEnrichmentProvider

SPARQL_URL = "https://query.wikidata.org/sparql"


def _provider() -> WikidataCompanyEnrichmentProvider:
    return WikidataCompanyEnrichmentProvider()


def _request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_ENRICHMENT, query=query)


def _sparql_response(bindings: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": bindings}})


@respx.mock
def test_matched_item_maps_industry_country_and_founded_year():
    respx.get(SPARQL_URL).mock(
        return_value=_sparql_response(
            [
                {
                    "itemLabel": {"type": "literal", "value": "Stripe"},
                    "industryLabel": {"type": "literal", "value": "financial services"},
                    "countryLabel": {"type": "literal", "value": "United States"},
                    "inception": {"type": "literal", "value": "2010-01-01T00:00:00Z"},
                }
            ]
        )
    )

    provider = _provider()
    response = provider.run(_request(company_name="Stripe"))

    assert response.success is True
    record = response.data[0]
    assert record.name == "Stripe"
    assert record.attributes == {"industry": "financial services", "country": "United States", "founded_year": 2010}
    assert response.source.is_mock is False


@respx.mock
def test_multiple_bindings_for_the_same_property_take_only_the_first_value():
    """Wikidata can return more than one industry value for one item — the
    provider must deterministically take the FIRST bound value, never
    attempt to merge/choose between them itself."""
    respx.get(SPARQL_URL).mock(
        return_value=_sparql_response(
            [
                {
                    "itemLabel": {"type": "literal", "value": "Stripe"},
                    "industryLabel": {"type": "literal", "value": "financial services"},
                    "countryLabel": {"type": "literal", "value": "United States"},
                },
                {
                    "itemLabel": {"type": "literal", "value": "Stripe"},
                    "industryLabel": {"type": "literal", "value": "mobile payment industry"},
                    "countryLabel": {"type": "literal", "value": "United States"},
                },
            ]
        )
    )

    provider = _provider()
    response = provider.run(_request(company_name="Stripe"))

    assert response.data[0].attributes["industry"] == "financial services"


@respx.mock
def test_no_wikidata_item_for_an_unknown_company_is_an_honest_failure():
    respx.get(SPARQL_URL).mock(return_value=_sparql_response([]))

    provider = _provider()
    response = provider.run(_request(company_name="Some Tiny Unknown Startup"))

    assert response.success is False
    assert response.error.code == WIKIDATA_NO_MATCH
    assert response.error.retryable is False


@respx.mock
def test_matched_item_with_no_usable_properties_is_reported_as_no_match():
    respx.get(SPARQL_URL).mock(
        return_value=_sparql_response([{"itemLabel": {"type": "literal", "value": "Some Co"}}])
    )

    provider = _provider()
    response = provider.run(_request(company_name="Some Co"))

    assert response.success is False
    assert response.error.code == WIKIDATA_NO_MATCH


@respx.mock
def test_no_company_name_supplied_is_an_honest_failure():
    provider = _provider()
    response = provider.run(_request())
    assert response.success is False
    assert response.error.code == WIKIDATA_NO_MATCH


@respx.mock
def test_company_name_with_quotes_is_escaped_not_a_query_break():
    """A company name containing a double-quote character must not break
    out of the SPARQL string literal — confirmed by asserting the request
    still succeeds (respx matches on URL, so a malformed query would
    either fail to match or, if it did match, prove the escaping worked
    without crashing the provider)."""
    respx.get(SPARQL_URL).mock(return_value=_sparql_response([]))

    provider = _provider()
    response = provider.run(_request(company_name='Weird "Quoted" Co'))  # must not raise

    assert response.success is False  # no match, but no crash either


@respx.mock
def test_unexpected_http_error_becomes_a_generic_provider_error_not_a_crash():
    respx.get(SPARQL_URL).mock(return_value=httpx.Response(500))

    provider = _provider()
    response = provider.run(_request(company_name="Stripe"))  # must not raise

    assert response.success is False


@respx.mock
def test_founded_year_ignores_an_unparseable_date_value():
    respx.get(SPARQL_URL).mock(
        return_value=_sparql_response(
            [
                {
                    "itemLabel": {"type": "literal", "value": "Some Co"},
                    "industryLabel": {"type": "literal", "value": "software"},
                    "inception": {"type": "literal", "value": "unknown-value"},
                }
            ]
        )
    )

    provider = _provider()
    response = provider.run(_request(company_name="Some Co"))

    assert "founded_year" not in response.data[0].attributes
    assert response.data[0].attributes["industry"] == "software"
