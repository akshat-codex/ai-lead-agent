from datetime import datetime, timezone

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.mocks import MockPeopleDataProvider
from app.providers.registry import ProviderRegistry
from app.schemas.canonical_icp import (
    CanonicalGeography,
    CanonicalHardRules,
    CanonicalICP,
    CanonicalSoftPreferences,
    EmployeeRange,
)
from app.schemas.company_resolution import ExistingCompanyIdentity
from app.schemas.people_discovery import PeopleDiscoveryStatus
from app.services.people_discovery import build_people_discovery_query, run_people_discovery


def _icp(icp_id: str = "icp-1", version: int = 1, allowed_titles: tuple[str, ...] = ("CMO", "Head of Growth")) -> CanonicalICP:
    return CanonicalICP(
        icp_id=icp_id,
        version=version,
        hard_rules=CanonicalHardRules(
            industries=("Skincare",),
            geography=CanonicalGeography(),
            employee_range=EmployeeRange(min=10, max=200),
            allowed_titles=allowed_titles,
            company_types=("D2C",),
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )


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


class _FakePeopleProvider(ProviderAdapter):
    """A configurable test double distinct from the real mock, so tests can
    freely construct arbitrary fake people/titles."""

    def __init__(self, provider_id: str, records: list[NormalizedRecord], should_fail: bool = False):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.PEOPLE_DISCOVERY})
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
                provider_id=self.provider_id, provider_name=self.provider_name,
                retrieved_at=datetime.now(timezone.utc), is_mock=True,
            ),
        )


# --- ICP titles -> discovery query -----------------------------------


def test_query_maps_allowed_titles_from_arbitrary_icp():
    icp = _icp(allowed_titles=("Founder", "CEO", "CMO"))
    company = _company()
    query = build_people_discovery_query(icp, company)
    assert query.titles == ("Founder", "CEO", "CMO")


def test_different_icps_produce_different_title_queries():
    icp_a = _icp(icp_id="icp-a", allowed_titles=("Founder", "CEO", "CMO"))
    icp_b = _icp(icp_id="icp-b", allowed_titles=("CEO", "VP Sales", "Head of Growth"))
    company = _company()

    query_a = build_people_discovery_query(icp_a, company)
    query_b = build_people_discovery_query(icp_b, company)

    assert query_a.titles == ("Founder", "CEO", "CMO")
    assert query_b.titles == ("CEO", "VP Sales", "Head of Growth")
    assert query_a.titles != query_b.titles


def test_arbitrary_custom_titles_pass_through_unchanged():
    """Nothing in this module may hard-code a title vocabulary — an
    unusual, ICP-author-invented title must flow through exactly as-is."""
    icp = _icp(allowed_titles=("Head of Paid Growth & Retention",))
    query = build_people_discovery_query(icp, _company())
    assert query.titles == ("Head of Paid Growth & Retention",)


def test_query_includes_company_domain_and_name():
    company = _company(canonical_domain="acme-test.invalid", canonical_name="Acme Test")
    query = build_people_discovery_query(_icp(), company)
    assert query.company_domain == "acme-test.invalid"
    assert query.company_name == "Acme Test"


# --- mock provider integration -----------------------------------------


def test_discovery_uses_the_mock_people_provider_and_filters_by_title():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    result = run_people_discovery(_icp(allowed_titles=("CMO",)), _company(), registry)

    assert result.status == PeopleDiscoveryStatus.COMPLETED
    assert len(result.candidates) == 1
    assert result.candidates[0].name == "Alex Sampleuser"
    assert result.candidates[0].title == "CMO"


def test_discovery_with_no_matching_titles_returns_empty_not_fabricated():
    """The mock only knows two titles; asking for a third must yield an
    honest empty result, never an invented person."""
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    result = run_people_discovery(_icp(allowed_titles=("CTO",)), _company(), registry)

    assert result.status == PeopleDiscoveryStatus.COMPLETED
    assert result.candidates == ()


def test_discovery_with_no_title_constraint_returns_everyone():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)
    assert len(result.candidates) == 2


# --- company association / traceability --------------------------------


def test_every_candidate_references_the_passed_in_company():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    result = run_people_discovery(_icp(allowed_titles=()), _company(id="company-42"), registry)
    assert all(c.company_id == "company-42" for c in result.candidates)


def test_result_and_candidates_trace_back_to_the_source_icp():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    icp = _icp(icp_id="icp-xyz", version=7, allowed_titles=())
    result = run_people_discovery(icp, _company(), registry)

    assert result.icp_id == "icp-xyz"
    assert result.icp_version == 7
    assert all(c.icp_id == "icp-xyz" and c.icp_version == 7 for c in result.candidates)


def test_provider_and_external_id_metadata_present():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)
    for candidate in result.candidates:
        assert candidate.provider_id == "mock-people-data-v1"
        assert candidate.external_id


def test_discovery_timestamp_is_recent():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    before = datetime.now(timezone.utc)
    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)
    after = datetime.now(timezone.utc)

    for candidate in result.candidates:
        assert before <= candidate.discovered_at <= after


# --- multiple people for one company --------------------------------------


def test_multiple_legitimate_people_coexist_for_one_company():
    registry = ProviderRegistry()
    registry.register(
        _FakePeopleProvider(
            "provider-a",
            [
                NormalizedRecord(external_id="p1", name="Founder Person", attributes={"title": "Founder"}),
                NormalizedRecord(external_id="p2", name="Marketing Person", attributes={"title": "CMO"}),
            ],
        )
    )
    result = run_people_discovery(_icp(allowed_titles=("Founder", "CMO")), _company(), registry)

    assert len(result.candidates) == 2
    titles = {c.title for c in result.candidates}
    assert titles == {"Founder", "CMO"}


# --- missing fields / no fabrication ------------------------------------


def test_missing_title_is_none_not_fabricated():
    registry = ProviderRegistry()
    registry.register(_FakePeopleProvider("bare-provider", [NormalizedRecord(external_id="p1", name="Mystery Person", attributes={})]))

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)
    assert result.candidates[0].title is None


def test_no_linkedin_or_email_is_fabricated_when_provider_does_not_supply_one():
    registry = ProviderRegistry()
    registry.register(_FakePeopleProvider("bare-provider", [NormalizedRecord(external_id="p1", name="Mystery Person", attributes={"title": "CEO"})]))

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)
    candidate = result.candidates[0]
    assert "linkedin_url" not in candidate.attributes
    assert "email" not in candidate.attributes
    assert candidate.attributes == {"title": "CEO"}


# --- multiple providers / failure handling ------------------------------


def test_multiple_providers_all_contribute_candidates():
    registry = ProviderRegistry()
    registry.register(_FakePeopleProvider("provider-a", [NormalizedRecord(external_id="a1", name="Alpha Person", attributes={"title": "CEO"})]))
    registry.register(_FakePeopleProvider("provider-b", [NormalizedRecord(external_id="b1", name="Beta Person", attributes={"title": "CTO"})]))

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)

    assert result.status == PeopleDiscoveryStatus.COMPLETED
    assert {c.name for c in result.candidates} == {"Alpha Person", "Beta Person"}


def test_one_provider_failure_while_another_succeeds():
    registry = ProviderRegistry()
    registry.register(_FakePeopleProvider("healthy", [NormalizedRecord(external_id="1", name="Healthy Person", attributes={})]))
    registry.register(_FakePeopleProvider("broken", [], should_fail=True))

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)

    assert result.status == PeopleDiscoveryStatus.PARTIAL_FAILURE
    assert [c.name for c in result.candidates] == ["Healthy Person"]
    outcomes = {o.provider_id: o for o in result.provider_outcomes}
    assert outcomes["broken"].success is False
    assert outcomes["broken"].error is not None


def test_all_providers_failing_does_not_raise_and_reports_failed():
    registry = ProviderRegistry()
    registry.register(_FakePeopleProvider("broken-a", [], should_fail=True))
    registry.register(_FakePeopleProvider("broken-b", [], should_fail=True))

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)  # must not raise

    assert result.status == PeopleDiscoveryStatus.FAILED
    assert result.candidates == ()


def test_no_providers_registered_reports_failed_cleanly():
    registry = ProviderRegistry()
    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)

    assert result.status == PeopleDiscoveryStatus.FAILED
    assert result.provider_outcomes == ()
    assert result.candidates == ()


def test_empty_results_from_a_successful_provider_is_still_completed():
    registry = ProviderRegistry()
    registry.register(_FakePeopleProvider("quiet", []))

    result = run_people_discovery(_icp(allowed_titles=()), _company(), registry)
    assert result.status == PeopleDiscoveryStatus.COMPLETED
    assert result.candidates == ()


# --- no qualification/verification during discovery -----------------------


def test_a_returned_title_outside_the_requested_list_is_not_filtered_out():
    """Discovery trusts provider output as-is — it never second-guesses or
    re-filters what a provider decided to return, even against the ICP's
    own title list. That gatekeeping belongs to a later phase, not here."""
    registry = ProviderRegistry()
    registry.register(
        _FakePeopleProvider("imprecise-provider", [NormalizedRecord(external_id="p1", name="Unexpected Person", attributes={"title": "Intern"})])
    )

    result = run_people_discovery(_icp(allowed_titles=("CMO",)), _company(), registry)
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Intern"


def test_candidate_schema_has_no_verification_or_qualification_fields():
    from app.schemas.candidate_person import CandidatePerson

    field_names = set(CandidatePerson.model_fields.keys())
    assert field_names.isdisjoint({"verified", "is_verified", "qualified", "score", "identity_confirmed"})


# --- determinism -----------------------------------------------------------


def test_mock_provider_discovery_is_deterministic_across_runs():
    registry = ProviderRegistry()
    registry.register(MockPeopleDataProvider())

    icp = _icp(allowed_titles=())
    first = run_people_discovery(icp, _company(), registry)
    second = run_people_discovery(icp, _company(), registry)

    def content(result):
        return sorted((c.provider_id, c.external_id, c.name, c.title) for c in result.candidates)

    assert content(first) == content(second)
