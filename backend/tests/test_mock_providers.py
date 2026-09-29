from app.providers.contracts import NormalizedRecord, ProviderCapability, ProviderRequest
from app.providers.mocks import (
    MOCK_SIMULATED_ERROR_CODE,
    MockCompanyDataProvider,
    MockPeopleDataProvider,
    MockWebSearchProvider,
)

# Vendor-only field names that must never appear as attribute keys on a
# NormalizedRecord — if one of these shows up, the adapter boundary has
# leaked a provider-specific shape into the normalized contract.
_VENDOR_ONLY_KEYS = {
    "co_id",
    "co_name",
    "hq_country",
    "emp_cnt",
    "person_ref",
    "full_name",
    "job_title",
    "employer_domain",
    "result_url",
    "headline",
}


def _assert_no_vendor_leakage(records: tuple[NormalizedRecord, ...]) -> None:
    for record in records:
        assert set(record.attributes.keys()).isdisjoint(_VENDOR_ONLY_KEYS)
        assert set(record.model_dump().keys()) == {"external_id", "name", "attributes"}


# --- company provider -------------------------------------------------


def test_company_discovery_returns_normalized_records():
    provider = MockCompanyDataProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY))

    assert response.success is True
    assert len(response.data) == 2
    assert response.data[0].attributes.keys() == {"country", "employee_count"}
    _assert_no_vendor_leakage(response.data)


def test_company_discovery_uses_obviously_fake_data():
    provider = MockCompanyDataProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY))
    names = {record.name for record in response.data}
    assert names == {"Example Test Co", "Sample Widgets Inc"}


def test_company_enrichment_uses_the_requested_domain():
    provider = MockCompanyDataProvider()
    response = provider.run(
        ProviderRequest(capability=ProviderCapability.COMPANY_ENRICHMENT, query={"domain": "acme-test.invalid"})
    )
    assert response.success is True
    assert len(response.data) == 1
    assert response.data[0].name == "Acme Test"
    _assert_no_vendor_leakage(response.data)


def test_company_provider_marks_source_as_mock():
    provider = MockCompanyDataProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY))
    assert response.source.is_mock is True
    assert response.cost.amount == 0.0


def test_company_provider_supports_both_of_its_capabilities():
    provider = MockCompanyDataProvider()
    assert provider.supports(ProviderCapability.COMPANY_DISCOVERY)
    assert provider.supports(ProviderCapability.COMPANY_ENRICHMENT)
    assert not provider.supports(ProviderCapability.WEB_SEARCH)


# --- people provider -----------------------------------------------------


def test_people_discovery_returns_normalized_records():
    provider = MockPeopleDataProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.PEOPLE_DISCOVERY))

    assert response.success is True
    assert len(response.data) == 2
    assert response.data[0].attributes.keys() == {"title", "company_domain"}
    _assert_no_vendor_leakage(response.data)


def test_person_enrichment_also_returns_normalized_records():
    provider = MockPeopleDataProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.PERSON_ENRICHMENT))
    assert response.success is True
    _assert_no_vendor_leakage(response.data)


def test_people_provider_uses_obviously_fake_data():
    provider = MockPeopleDataProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.PEOPLE_DISCOVERY))
    names = {record.name for record in response.data}
    assert names == {"Jane Testperson", "Alex Sampleuser"}
    domains = {record.attributes["company_domain"] for record in response.data}
    assert all(domain.endswith(".invalid") for domain in domains)


# --- web search provider ---------------------------------------------


def test_web_search_returns_normalized_records_for_the_query():
    provider = MockWebSearchProvider()
    response = provider.run(ProviderRequest(capability=ProviderCapability.WEB_SEARCH, query={"q": "growth marketing"}))

    assert response.success is True
    assert len(response.data) == 2
    assert all("growth marketing" in record.name for record in response.data)
    assert response.data[0].attributes.keys() == {"url"}
    _assert_no_vendor_leakage(response.data)


# --- error handling ---------------------------------------------------


def test_simulated_error_on_company_provider():
    provider = MockCompanyDataProvider()
    response = provider.run(
        ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query={"simulate_error": True})
    )
    assert response.success is False
    assert response.error.code == MOCK_SIMULATED_ERROR_CODE
    assert response.error.retryable is True
    assert response.data == ()


def test_simulated_error_on_people_provider():
    provider = MockPeopleDataProvider()
    response = provider.run(
        ProviderRequest(capability=ProviderCapability.PEOPLE_DISCOVERY, query={"simulate_error": True})
    )
    assert response.success is False
    assert response.error.code == MOCK_SIMULATED_ERROR_CODE


def test_simulated_error_on_web_search_provider():
    provider = MockWebSearchProvider()
    response = provider.run(
        ProviderRequest(capability=ProviderCapability.WEB_SEARCH, query={"simulate_error": True})
    )
    assert response.success is False
    assert response.error.code == MOCK_SIMULATED_ERROR_CODE


def test_error_responses_still_have_latency_measured():
    provider = MockCompanyDataProvider()
    response = provider.run(
        ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY, query={"simulate_error": True})
    )
    assert response.latency_ms is not None


# --- reliability metadata -------------------------------------------------


def test_reliability_profile_is_present_and_within_bounds():
    for provider in (MockCompanyDataProvider(), MockPeopleDataProvider(), MockWebSearchProvider()):
        profile = provider.reliability_profile
        for bounded in (profile.accuracy, profile.coverage, profile.failure_rate):
            assert bounded is None or 0 <= bounded <= 1
        assert profile.cost_per_request == 0.0


# --- determinism -----------------------------------------------------------


def test_mock_provider_output_is_deterministic_across_calls():
    provider = MockCompanyDataProvider()
    request = ProviderRequest(capability=ProviderCapability.COMPANY_DISCOVERY)

    first = provider.run(request)
    second = provider.run(request)

    assert first.data == second.data
    assert [r.name for r in first.data] == [r.name for r in second.data]
