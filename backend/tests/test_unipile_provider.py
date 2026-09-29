"""Unit tests for UnipileProvider (PEOPLE_DISCOVERY + fallback
COMPANY_ENRICHMENT), following test_apollo_provider.py's HTTP-mocking
convention (respx, built for httpx) exactly.
"""
import httpx
import respx

from app.providers.base import ProviderErrorCode
from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.unipile import UNIPILE_AUTH_FAILED, UNIPILE_MALFORMED_RESPONSE, UnipileProvider

UNIPILE_SEARCH_URL = "https://api8.unipile.com:13111/api/v1/linkedin/search"


def _provider(api_key: str = "test-key-12345", account_id: str = "test-account-1") -> UnipileProvider:
    return UnipileProvider(api_key=api_key, dsn="https://api8.unipile.com:13111", account_id=account_id)


def _people_request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.PEOPLE_DISCOVERY, query=query)


def _company_request(**query) -> ProviderRequest:
    return ProviderRequest(capability=ProviderCapability.COMPANY_ENRICHMENT, query=query)


_SAMPLE_PERSON = {
    "type": "PEOPLE",
    "id": "ACwAAASNCJcBS2NERCgi0j_f7_oYqCSbTGsNYBc",
    "name": "Jane Testperson",
    "first_name": "Jane",
    "last_name": "Testperson",
    "public_identifier": "jane-testperson-b876a021",
    "public_profile_url": "https://www.linkedin.com/in/jane-testperson-b876a021",
    "profile_url": "https://www.linkedin.com/in/jane-testperson-b876a021",
    "headline": "Head of Growth at Example Test Co",
    "current_positions": [
        {"company": "Example Test Co", "company_id": "165158", "role": "Head of Growth"}
    ],
}


# --- authentication -------------------------------------------------------


@respx.mock
def test_api_key_sent_as_header_and_account_id_as_query_param():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    provider = _provider(api_key="header-only-key", account_id="acct-42")
    provider.run(_people_request(titles=["CMO"]))

    sent_request = respx.calls.last.request
    assert sent_request.headers["X-API-KEY"] == "header-only-key"
    assert dict(httpx.QueryParams(sent_request.url.query))["account_id"] == "acct-42"
    assert "header-only-key" not in str(sent_request.url)
    assert b"header-only-key" not in sent_request.content


@respx.mock
def test_auth_failure_returns_specific_error_code_not_generic_provider_error():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))

    provider = _provider(api_key="wrong-key")
    response = provider.run(_people_request(titles=["CMO"]))

    assert response.success is False
    assert response.error.code == UNIPILE_AUTH_FAILED
    assert response.error.retryable is False


@respx.mock
def test_forbidden_also_maps_to_auth_failed():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.error.code == UNIPILE_AUTH_FAILED


@respx.mock
def test_api_key_never_appears_in_response_or_error_message():
    secret_key = "sk-super-secret-unipile-key-do-not-leak"
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(500, text="internal error"))

    response = _provider(api_key=secret_key).run(_people_request(titles=["CMO"]))

    assert secret_key not in response.model_dump_json()
    if response.error:
        assert secret_key not in response.error.message


# --- successful people-search mapping --------------------------------------


@respx.mock
def test_successful_people_search_maps_documented_fields():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [_SAMPLE_PERSON]}))

    response = _provider().run(_people_request(titles=["Head of Growth"], company_name="Example Test Co"))

    assert response.success is True
    assert len(response.data) == 1
    record = response.data[0]
    assert record.external_id == "ACwAAASNCJcBS2NERCgi0j_f7_oYqCSbTGsNYBc"
    assert record.name == "Jane Testperson"
    assert response.source is not None
    assert response.source.is_mock is False
    assert response.source.provider_id == "unipile-linkedin-v1"


def test_name_mapping():
    with respx.mock:
        respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [_SAMPLE_PERSON]}))
        response = _provider().run(_people_request(titles=["Head of Growth"]))
    assert response.data[0].name == "Jane Testperson"


def test_title_mapping_from_current_positions():
    with respx.mock:
        respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [_SAMPLE_PERSON]}))
        response = _provider().run(_people_request(titles=["Head of Growth"]))
    assert response.data[0].attributes["title"] == "Head of Growth"


def test_company_association_mapping_from_current_positions():
    with respx.mock:
        respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [_SAMPLE_PERSON]}))
        response = _provider().run(_people_request(titles=["Head of Growth"]))
    assert response.data[0].attributes["company association"] == "Example Test Co"


def test_linkedin_url_and_id_mapping_from_public_profile_url():
    with respx.mock:
        respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [_SAMPLE_PERSON]}))
        response = _provider().run(_people_request(titles=["Head of Growth"]))
    attrs = response.data[0].attributes
    assert attrs["linkedin_id"] == "in/jane-testperson-b876a021"
    assert attrs["linkedin_url"] == "https://www.linkedin.com/in/jane-testperson-b876a021"


# --- honest handling of missing/partial LinkedIn fields --------------------


@respx.mock
def test_missing_public_profile_url_falls_back_to_public_identifier():
    person = {**_SAMPLE_PERSON, "public_profile_url": None, "profile_url": None}
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [person]}))

    response = _provider().run(_people_request(titles=["Head of Growth"]))

    assert response.data[0].attributes["linkedin_id"] == "in/jane-testperson-b876a021"


@respx.mock
def test_missing_linkedin_fields_entirely_are_absent_never_fabricated():
    person = {
        "id": "some-id",
        "name": "No LinkedIn Person",
        "public_profile_url": None,
        "profile_url": None,
        "public_identifier": None,
        "current_positions": [],
    }
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [person]}))

    response = _provider().run(_people_request(titles=["CMO"]))

    attrs = response.data[0].attributes
    assert "linkedin_id" not in attrs
    assert "linkedin_url" not in attrs
    assert None not in attrs.values()


@respx.mock
def test_sales_navigator_style_profile_url_is_never_used_as_a_public_linkedin_url():
    """profile_url can point at an internal /sales/lead/... page — this must
    never be presented as a public linkedin.com/in/ profile."""
    person = {
        "id": "sn-id",
        "name": "Sales Nav Person",
        "public_profile_url": None,
        "public_identifier": None,
        "profile_url": "https://www.linkedin.com/sales/lead/ACwAAASNCJcB,NAME_SEARCH",
        "current_positions": [],
    }
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [person]}))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert "linkedin_id" not in response.data[0].attributes


@respx.mock
def test_missing_title_is_absent_never_fabricated():
    person = {**_SAMPLE_PERSON, "current_positions": []}
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": [person]}))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert "title" not in response.data[0].attributes


@respx.mock
def test_person_with_no_name_or_id_is_skipped_not_fabricated():
    respx.post(UNIPILE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"items": [{"id": None, "name": None}, _SAMPLE_PERSON]})
    )

    response = _provider().run(_people_request(titles=["CMO"]))

    assert len(response.data) == 1
    assert response.data[0].name == "Jane Testperson"


@respx.mock
def test_empty_results_returns_success_with_no_candidates():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.success is True
    assert response.data == ()


# --- malformed response / network errors ------------------------------


@respx.mock
def test_malformed_json_response_is_a_clean_failure_not_an_exception():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, text="not json{"))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.success is False
    assert response.error.code == UNIPILE_MALFORMED_RESPONSE


@respx.mock
def test_response_missing_items_key_is_a_clean_failure():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.success is False
    assert response.error.code == UNIPILE_MALFORMED_RESPONSE


@respx.mock
def test_response_body_not_a_json_object_is_a_clean_failure():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.success is False
    assert response.error.code == UNIPILE_MALFORMED_RESPONSE


@respx.mock
def test_server_error_is_caught_by_run_as_generic_provider_error():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(500, text="internal error"))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR


@respx.mock
def test_network_timeout_is_caught_by_run_as_generic_provider_error():
    respx.post(UNIPILE_SEARCH_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    response = _provider().run(_people_request(titles=["CMO"]))

    assert response.success is False
    assert response.error.code == ProviderErrorCode.PROVIDER_ERROR


# --- ICP titles are supplied by the caller, never hardcoded ---------------


@respx.mock
def test_multiple_icp_titles_are_forwarded_as_keyword_search_never_dropped():
    route = respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    _provider().run(_people_request(titles=["CMO", "VP Marketing", "Head of Growth"]))

    import json as _json

    sent_body = _json.loads(route.calls.last.request.content)
    assert sent_body["keywords"] == "CMO OR VP Marketing OR Head of Growth"


@respx.mock
def test_no_titles_supplied_omits_keywords_entirely():
    route = respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    _provider().run(_people_request())

    import json as _json

    sent_body = _json.loads(route.calls.last.request.content)
    assert "keywords" not in sent_body


@respx.mock
def test_company_name_is_forwarded_to_scope_the_search():
    route = respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    _provider().run(_people_request(titles=["CMO"], company_name="Example Test Co"))

    import json as _json

    sent_body = _json.loads(route.calls.last.request.content)
    assert sent_body["company"] == ["Example Test Co"]


@respx.mock
def test_search_always_uses_classic_people_category():
    route = respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    _provider().run(_people_request(titles=["CMO"]))

    import json as _json

    sent_body = _json.loads(route.calls.last.request.content)
    assert sent_body["api"] == "classic"
    assert sent_body["category"] == "people"


def test_provider_declares_people_discovery_and_company_enrichment_only():
    provider = _provider()
    assert provider.supports(ProviderCapability.PEOPLE_DISCOVERY) is True
    assert provider.supports(ProviderCapability.COMPANY_ENRICHMENT) is True
    assert provider.supports(ProviderCapability.COMPANY_DISCOVERY) is False
    assert provider.supports(ProviderCapability.PERSON_ENRICHMENT) is False


# --- COMPANY_ENRICHMENT fallback (company LinkedIn) ------------------------


@respx.mock
def test_company_enrichment_maps_linkedin_from_company_search():
    respx.post(UNIPILE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "type": "COMPANY",
                        "id": "165158",
                        "name": "Example Test Co",
                        "profile_url": "https://www.linkedin.com/company/example-test/",
                        "industry": "Cosmetics",
                    }
                ]
            },
        )
    )

    response = _provider().run(_company_request(company_name="Example Test Co"))

    assert response.success is True
    assert len(response.data) == 1
    assert response.data[0].attributes["linkedin_id"] == "company/example-test"


@respx.mock
def test_company_enrichment_uses_company_search_category():
    route = respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    _provider().run(_company_request(company_name="Example Test Co"))

    import json as _json

    sent_body = _json.loads(route.calls.last.request.content)
    assert sent_body["category"] == "companies"


def test_company_enrichment_with_no_company_name_returns_empty_without_a_network_call():
    with respx.mock:
        route = respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))
        response = _provider().run(_company_request())
        assert response.success is True
        assert response.data == ()
        assert not route.called


@respx.mock
def test_company_enrichment_no_results_returns_success_with_no_candidates():
    respx.post(UNIPILE_SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    response = _provider().run(_company_request(company_name="Nonexistent Co"))

    assert response.success is True
    assert response.data == ()
