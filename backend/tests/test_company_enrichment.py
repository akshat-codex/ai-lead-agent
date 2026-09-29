from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
)
from app.providers.mocks import MockCompanyDataProvider, MockCompanyRegistryProvider
from app.providers.registry import ProviderRegistry
from app.schemas.company_enrichment import EnrichmentRunStatus
from app.schemas.company_resolution import ExistingCompanyIdentity
from app.services.company_enrichment import build_enrichment_query, run_company_enrichment


def _company(**overrides) -> ExistingCompanyIdentity:
    base = dict(
        id="company-1",
        canonical_name="Example Test Co",
        canonical_domain="example-test.invalid",
        aliases=(),
        provider_identities={},
    )
    base.update(overrides)
    return ExistingCompanyIdentity(**base)


class _BrokenEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str = "broken-enrichment"):
        super().__init__(provider_id=provider_id, provider_name="Broken", capabilities={ProviderCapability.COMPANY_ENRICHMENT})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("vendor outage")


# --- query building ------------------------------------------------------


def test_query_uses_companys_domain_and_name():
    company = _company()
    query = build_enrichment_query(company, "some-provider")
    assert query.domain == "example-test.invalid"
    assert query.company_name == "Example Test Co"
    assert query.external_id is None


def test_query_includes_external_id_only_for_a_provider_that_has_one():
    company = _company(provider_identities={"provider-a": "ext-1"})
    query_a = build_enrichment_query(company, "provider-a")
    query_b = build_enrichment_query(company, "provider-b")
    assert query_a.external_id == "ext-1"
    assert query_b.external_id is None


# --- basic enrichment ------------------------------------------------------


def test_enrichment_returns_structured_facts_from_the_mock():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    result = run_company_enrichment(_company(), registry)

    assert result.status == EnrichmentRunStatus.COMPLETED
    assert result.company_id == "company-1"
    field_names = {f.field for f in result.fields}
    assert {"industry", "employee_range", "country", "company_type", "business_model", "linkedin_id", "products_services", "domain"} <= field_names


def test_no_vendor_field_names_leak_into_facts():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    result = run_company_enrichment(_company(), registry)

    field_names = {f.field for f in result.fields}
    vendor_only = {"co_id", "co_name", "hq_country", "emp_band", "industry_code", "org_type", "biz_model", "li_handle", "offerings"}
    assert field_names.isdisjoint(vendor_only)


def test_missing_field_is_simply_absent_not_fabricated():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    result = run_company_enrichment(_company(), registry)

    field_names = {f.field for f in result.fields}
    assert "revenue" not in field_names  # nothing here ever returns it — must not appear at all


def test_no_confidence_is_invented_for_facts_that_have_none():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    result = run_company_enrichment(_company(), registry)

    for field in result.fields:
        for fact in field.facts:
            assert fact.confidence is None


def test_facts_carry_provider_and_external_id_provenance():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    result = run_company_enrichment(_company(), registry)

    for field in result.fields:
        for fact in field.facts:
            assert fact.provider_id == "mock-company-data-v1"
            assert fact.external_id  # non-empty
            assert fact.retrieved_at is not None


# --- multiple providers / conflicts ---------------------------------------


def test_multiple_providers_both_contribute_facts():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())

    result = run_company_enrichment(_company(), registry)
    provider_ids = {o.provider_id for o in result.provider_outcomes}
    assert provider_ids == {"mock-company-data-v1", "mock-company-registry-v1"}


def test_conflicting_employee_range_is_flagged_not_resolved():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())

    result = run_company_enrichment(_company(), registry)
    employee_field = next(f for f in result.fields if f.field == "employee_range")

    assert employee_field.conflict is True
    values = {tuple(sorted(fact.value.items())) for fact in employee_field.facts}
    assert len(values) == 2  # both distinct values preserved, neither dropped


def test_agreeing_fields_are_not_flagged_as_conflicts():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())

    result = run_company_enrichment(_company(), registry)
    industry_field = next(f for f in result.fields if f.field == "industry")

    assert industry_field.conflict is False


def test_conflict_never_silently_picks_a_winner():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())

    result = run_company_enrichment(_company(), registry)
    employee_field = next(f for f in result.fields if f.field == "employee_range")
    assert len(employee_field.facts) == 2  # neither value discarded


# --- provider failure handling ---------------------------------------------


def test_one_provider_failing_reports_partial_failure_and_keeps_the_others_facts():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(_BrokenEnrichmentProvider())

    result = run_company_enrichment(_company(), registry)

    assert result.status == EnrichmentRunStatus.PARTIAL_FAILURE
    assert any(f.field == "industry" for f in result.fields)  # healthy provider's data still present
    outcomes = {o.provider_id: o for o in result.provider_outcomes}
    assert outcomes["broken-enrichment"].success is False
    assert outcomes["broken-enrichment"].error is not None


def test_all_providers_failing_does_not_raise_and_reports_failed():
    registry = ProviderRegistry()
    registry.register(_BrokenEnrichmentProvider("broken-a"))
    registry.register(_BrokenEnrichmentProvider("broken-b"))

    result = run_company_enrichment(_company(), registry)  # must not raise

    assert result.status == EnrichmentRunStatus.FAILED
    assert result.fields == ()


def test_no_providers_registered_reports_failed_cleanly():
    registry = ProviderRegistry()
    result = run_company_enrichment(_company(), registry)
    assert result.status == EnrichmentRunStatus.FAILED
    assert result.fields == ()


# --- determinism -----------------------------------------------------------


def test_mock_enrichment_is_deterministic_across_runs():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    registry.register(MockCompanyRegistryProvider())

    first = run_company_enrichment(_company(), registry)
    second = run_company_enrichment(_company(), registry)

    def content(result):
        return sorted(
            (f.field, fact.provider_id, fact.value if not isinstance(fact.value, (list, dict)) else str(fact.value))
            for f in result.fields
            for fact in f.facts
        )

    assert content(first) == content(second)


# --- no qualification / no identity resolution -----------------------------


def test_enrichment_never_touches_qualification_fields():
    """A structural check that enrichment facts never include anything
    resembling a fit/score/qualification verdict."""
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())
    result = run_company_enrichment(_company(), registry)

    field_names = {f.field for f in result.fields}
    assert field_names.isdisjoint({"score", "fit", "qualified", "pass", "fail", "hold"})
