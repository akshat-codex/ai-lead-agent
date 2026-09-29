from datetime import datetime, timezone

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.mocks import MockCompanyDataProvider
from app.providers.registry import ProviderRegistry
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
    GeographyEntry,
)
from app.schemas.discovery import DiscoveryStatus
from app.services.company_discovery import build_company_discovery_query, run_company_discovery


def _icp(icp_id: str = "icp-1", version: int = 1, **hard_overrides) -> CanonicalICP:
    hard_defaults = dict(
        industries=("Skincare",),
        geography=CanonicalGeography(
            countries=(GeographyEntry(raw="United States", code="US", label="United States"),),
            unrecognized=("Nordics",),
        ),
        employee_range=EmployeeRange(min=10, max=200),
        allowed_titles=("CMO",),
        company_types=("D2C",),
        exclusions=("Acme Corp",),
    )
    hard_defaults.update(hard_overrides)
    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=CanonicalHardRules(**hard_defaults),
        soft_preferences=CanonicalSoftPreferences(),
    )


class _FakeCompanyProvider(ProviderAdapter):
    """A configurable test double distinct from the real mock, so
    multi-provider tests aren't just the same mock twice."""

    def __init__(self, provider_id: str, records: list[NormalizedRecord], should_fail: bool = False):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records
        self._should_fail = should_fail

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if self._should_fail:
            raise RuntimeError(f"{self.provider_id} is down")
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(self._records),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc),
                is_mock=True,
            ),
        )


# --- CanonicalICP -> discovery request --------------------------------


def test_query_maps_hard_rules_from_arbitrary_icp():
    icp = _icp(
        industries=("Fintech", "Insurance"),
        company_types=("Enterprise",),
        employee_range=EmployeeRange(min=100, max=5000),
    )
    query = build_company_discovery_query(icp, limit=15)

    assert query.industries == ("Fintech", "Insurance")
    assert query.company_types == ("Enterprise",)
    assert query.min_employees == 100
    assert query.max_employees == 5000
    assert query.limit == 15


def test_query_maps_geography_codes_and_unrecognized_separately():
    icp = _icp()
    query = build_company_discovery_query(icp)
    assert query.geography_codes == ("US",)
    assert query.geography_unrecognized == ("Nordics",)


def test_query_excludes_exclusion_rules_but_carries_allowed_titles_as_a_signal():
    """exclusions/custom_rules are not part of what a company discovery
    query asks for — they're a post-discovery Hard Rule Engine concern.

    allowed_titles is the one deliberate exception (see
    app/schemas/candidate_company.py::CompanyDiscoveryQuery's own
    docstring): it remains the ICP's person-level decision-maker filter
    everywhere else (this is NOT a redesign of that), but is additionally
    carried through here as an OPTIONAL discovery-query SIGNAL — a
    web-search provider (app/providers/tavily.py's hiring-signal query
    type) can use the ICP's own target roles to search real job-board
    postings for companies actively hiring, confirmed live to surface
    real, named companies. This changes nothing about what a candidate
    qualifies against; the Hard ICP Rule Engine's own allowed_titles
    evaluation (checked against discovered PEOPLE, never companies) is
    completely unaffected and unaware this field also reached here."""
    query_fields = set(build_company_discovery_query(_icp()).model_dump().keys())
    assert "allowed_titles" in query_fields
    assert "exclusions" not in query_fields
    assert "custom_rules" not in query_fields


def test_query_carries_the_icps_own_allowed_titles_verbatim():
    icp = _icp(allowed_titles=("Founder", "AI/ML Researcher"))
    query = build_company_discovery_query(icp)
    assert query.allowed_titles == ("Founder", "AI/ML Researcher")


def test_query_allowed_titles_is_empty_when_the_icp_has_none():
    query = build_company_discovery_query(_icp(allowed_titles=()))
    assert query.allowed_titles == ()


# --- mock provider integration ------------------------------------------


def test_discovery_uses_the_mock_company_provider():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    result = run_company_discovery(_icp(), registry)

    assert result.status == DiscoveryStatus.COMPLETED
    assert len(result.candidates) == 2
    names = {c.name for c in result.candidates}
    assert names == {"Example Test Co", "Sample Widgets Inc"}


def test_discovery_works_for_an_entirely_different_arbitrary_icp():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    icp = _icp(icp_id="icp-fintech", version=3, industries=("Fintech",), company_types=("Enterprise",))
    result = run_company_discovery(icp, registry)

    assert result.icp_id == "icp-fintech"
    assert result.icp_version == 3
    assert len(result.candidates) == 2
    assert all(c.icp_id == "icp-fintech" and c.icp_version == 3 for c in result.candidates)


# --- candidate mapping / metadata --------------------------------------


def test_candidate_mapping_preserves_normalized_attributes_without_vendor_leakage():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    result = run_company_discovery(_icp(), registry)
    for candidate in result.candidates:
        assert set(candidate.attributes.keys()) == {"country", "employee_count"}
        assert "co_name" not in candidate.attributes
        assert "hq_country" not in candidate.attributes


def test_candidate_records_provider_and_external_id_for_traceability():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    result = run_company_discovery(_icp(), registry)
    for candidate in result.candidates:
        assert candidate.provider_id == "mock-company-data-v1"
        assert candidate.external_id  # non-empty


def test_candidate_discovery_timestamp_is_recent():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    before = datetime.now(timezone.utc)
    result = run_company_discovery(_icp(), registry)
    after = datetime.now(timezone.utc)

    for candidate in result.candidates:
        assert before <= candidate.discovered_at <= after


def test_provider_outcome_records_requested_and_returned_counts():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    result = run_company_discovery(_icp(), registry, limit=7)
    outcome = result.provider_outcomes[0]
    assert outcome.requested == 7
    assert outcome.returned == 2
    assert outcome.success is True


# --- multiple providers / failure handling ------------------------------


def test_multiple_providers_all_contribute_candidates():
    registry = ProviderRegistry()
    registry.register(
        _FakeCompanyProvider("provider-a", [NormalizedRecord(external_id="a1", name="Alpha Co", attributes={})])
    )
    registry.register(
        _FakeCompanyProvider("provider-b", [NormalizedRecord(external_id="b1", name="Beta Co", attributes={})])
    )

    result = run_company_discovery(_icp(), registry)

    assert result.status == DiscoveryStatus.COMPLETED
    assert {c.name for c in result.candidates} == {"Alpha Co", "Beta Co"}
    assert {o.provider_id for o in result.provider_outcomes} == {"provider-a", "provider-b"}


def test_one_provider_failure_while_another_succeeds():
    registry = ProviderRegistry()
    registry.register(_FakeCompanyProvider("healthy", [NormalizedRecord(external_id="1", name="Healthy Co", attributes={})]))
    registry.register(_FakeCompanyProvider("broken", [], should_fail=True))

    result = run_company_discovery(_icp(), registry)

    assert result.status == DiscoveryStatus.PARTIAL_FAILURE
    assert [c.name for c in result.candidates] == ["Healthy Co"]
    outcomes = {o.provider_id: o for o in result.provider_outcomes}
    assert outcomes["healthy"].success is True
    assert outcomes["broken"].success is False
    assert outcomes["broken"].error is not None


def test_all_providers_failing_does_not_raise_and_reports_failed():
    registry = ProviderRegistry()
    registry.register(_FakeCompanyProvider("broken-a", [], should_fail=True))
    registry.register(_FakeCompanyProvider("broken-b", [], should_fail=True))

    result = run_company_discovery(_icp(), registry)  # must not raise

    assert result.status == DiscoveryStatus.FAILED
    assert result.candidates == ()
    assert all(not o.success for o in result.provider_outcomes)


def test_no_providers_registered_reports_failed_cleanly():
    registry = ProviderRegistry()  # nothing registered at all
    result = run_company_discovery(_icp(), registry)

    assert result.status == DiscoveryStatus.FAILED
    assert result.provider_outcomes == ()
    assert result.candidates == ()


def test_empty_results_from_a_successful_provider_is_still_completed():
    registry = ProviderRegistry()
    registry.register(_FakeCompanyProvider("quiet", []))

    result = run_company_discovery(_icp(), registry)

    assert result.status == DiscoveryStatus.COMPLETED
    assert result.candidates == ()


def test_exact_duplicate_records_within_one_provider_response_are_collapsed():
    registry = ProviderRegistry()
    registry.register(
        _FakeCompanyProvider(
            "dupey",
            [
                NormalizedRecord(external_id="dup-1", name="Dup Co", attributes={}),
                NormalizedRecord(external_id="dup-1", name="Dup Co", attributes={}),
            ],
        )
    )

    result = run_company_discovery(_icp(), registry)
    assert len(result.candidates) == 1


# --- determinism -----------------------------------------------------------


def test_mock_provider_discovery_is_deterministic_across_runs():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    first = run_company_discovery(_icp(), registry)
    second = run_company_discovery(_icp(), registry)

    first_content = sorted((c.provider_id, c.external_id, c.name, tuple(sorted(c.attributes.items()))) for c in first.candidates)
    second_content = sorted((c.provider_id, c.external_id, c.name, tuple(sorted(c.attributes.items()))) for c in second.candidates)
    assert first_content == second_content


# --- no qualification ----------------------------------------------------


def test_discovery_returns_candidates_even_when_they_would_fail_hard_rules():
    """An ICP demanding 999,999+ employees would fail every hard-rule
    evaluation against the mock's fake companies (42 and 120 employees) —
    but discovery must still return them. Qualification is Phase 3's job,
    run separately and later."""
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    impossible_icp = _icp(employee_range=EmployeeRange(min=999_999, max=None))
    result = run_company_discovery(impossible_icp, registry)

    assert len(result.candidates) == 2  # not filtered out despite failing an obvious hard rule


# --- ICP/version traceability ----------------------------------------------


def test_run_result_and_every_candidate_trace_back_to_the_source_icp():
    registry = ProviderRegistry()
    registry.register(MockCompanyDataProvider())

    icp = _icp(icp_id="icp-xyz", version=9)
    result = run_company_discovery(icp, registry)

    assert result.icp_id == "icp-xyz"
    assert result.icp_version == 9
    assert all(c.icp_id == "icp-xyz" and c.icp_version == 9 for c in result.candidates)
