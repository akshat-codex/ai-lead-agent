"""Phase 5 — mock provider adapters.

These exist only to prove the abstraction works end-to-end without any
external dependency, cost, or credential. Every record they return uses
obviously fake data (names like "Example Test Co", and domains under the
`.invalid` TLD — reserved by RFC 2606 for exactly this purpose) and is
tagged `source.is_mock = True`. Nothing here may ever be treated as a real
lead.

Each mock's internal fake payload deliberately uses vendor-looking field
names (`co_name`, `hq_country`, `emp_cnt`, ...) that are different from the
normalized attribute names it maps them to. That mismatch is intentional —
it is what the "no provider-specific data leaking into core contracts"
tests in tests/test_mock_providers.py check for.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderReliabilityProfile,
    ProviderRequest,
    ProviderResponse,
    RequestCost,
    SourceMetadata,
)

# A fault-injection hook, not a real provider error code: set
# query={"simulate_error": True} in a request to exercise error handling
# without needing a real (or flaky) external call.
MOCK_SIMULATED_ERROR_CODE = "SIMULATED_ERROR"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _simulated_error(provider_id: str, capability: ProviderCapability) -> ProviderResponse:
    return ProviderResponse(
        provider_id=provider_id,
        capability=capability,
        success=False,
        error=ProviderError(
            code=MOCK_SIMULATED_ERROR_CODE,
            message="Simulated provider failure (requested via query.simulate_error).",
            retryable=True,
        ),
    )


class MockCompanyDataProvider(ProviderAdapter):
    """Fakes a vendor offering both company discovery and enrichment."""

    def __init__(self, provider_id: str = "mock-company-data-v1") -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Mock Company Data Provider",
            capabilities={ProviderCapability.COMPANY_DISCOVERY, ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(
                accuracy=0.5,
                coverage=0.5,
                freshness_days=0,
                cost_per_request=0.0,
                average_latency_ms=5,
                failure_rate=0.0,
            ),
        )

    @staticmethod
    def _fake_vendor_discovery_payload() -> list[dict]:
        return [
            {"co_id": "mock-co-1", "co_name": "Example Test Co", "hq_country": "US", "emp_cnt": 42},
            {"co_id": "mock-co-2", "co_name": "Sample Widgets Inc", "hq_country": "CA", "emp_cnt": 120},
        ]

    @staticmethod
    def _fake_vendor_enrichment_payload(domain: str) -> dict:
        return {
            "co_id": f"mock-{domain}",
            "co_name": domain.split(".")[0].replace("-", " ").title(),
            "hq_country": "US",
            "emp_band": {"min": 51, "max": 200},
            "industry_code": "Skincare",
            "org_type": "D2C",
            "biz_model": "Subscription",
            "li_handle": f"company/{domain.split('.')[0]}",
            "offerings": ["Skincare products", "Subscription boxes"],
        }

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.query.get("simulate_error"):
            return _simulated_error(self.provider_id, request.capability)

        if request.capability == ProviderCapability.COMPANY_DISCOVERY:
            raw_records = self._fake_vendor_discovery_payload()
            normalized = tuple(
                NormalizedRecord(
                    external_id=raw["co_id"],
                    name=raw["co_name"],
                    attributes={"country": raw["hq_country"], "employee_count": raw["emp_cnt"]},
                )
                for raw in raw_records
            )
        else:  # COMPANY_ENRICHMENT
            domain = request.query.get("domain") or "example-test.invalid"
            raw = self._fake_vendor_enrichment_payload(domain)
            normalized = (
                NormalizedRecord(
                    external_id=raw["co_id"],
                    name=raw["co_name"],
                    attributes={
                        "domain": domain,
                        "country": raw["hq_country"],
                        "employee_range": raw["emp_band"],
                        "industry": raw["industry_code"],
                        "company_type": raw["org_type"],
                        "business_model": raw["biz_model"],
                        "linkedin_id": raw["li_handle"],
                        "products_services": raw["offerings"],
                    },
                ),
            )

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=normalized,
            cost=RequestCost(amount=0.0),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=True,
            ),
        )


class MockCompanyRegistryProvider(ProviderAdapter):
    """A second, independent fake company-data source, used to exercise
    multi-provider enrichment and conflicting-value handling (Phase 8).

    Deliberately disagrees with MockCompanyDataProvider on employee range
    while agreeing on industry and domain — the way two real vendors often
    do: mostly overlapping, but not identical.
    """

    def __init__(self, provider_id: str = "mock-company-registry-v1") -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Mock Company Registry Provider",
            capabilities={ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(
                accuracy=0.4,
                coverage=0.6,
                freshness_days=90,
                cost_per_request=0.0,
                average_latency_ms=12,
                failure_rate=0.0,
            ),
        )

    @staticmethod
    def _fake_vendor_payload(domain: str) -> dict:
        return {
            "registry_id": f"reg-{domain}",
            "legal_name": domain.split(".")[0].replace("-", " ").title(),
            "hq": "United States",
            "staff_band": {"min": 11, "max": 50},
            "sector": "Skincare",
        }

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.query.get("simulate_error"):
            return _simulated_error(self.provider_id, request.capability)

        domain = request.query.get("domain") or "example-test.invalid"
        raw = self._fake_vendor_payload(domain)
        normalized = (
            NormalizedRecord(
                external_id=raw["registry_id"],
                name=raw["legal_name"],
                attributes={
                    "domain": domain,
                    "country": raw["hq"],
                    "employee_range": raw["staff_band"],
                    "industry": raw["sector"],
                },
            ),
        )

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=normalized,
            cost=RequestCost(amount=0.0),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=True,
            ),
        )


class MockPeopleDataProvider(ProviderAdapter):
    """Fakes a vendor offering both people discovery and person enrichment."""

    def __init__(self, provider_id: str = "mock-people-data-v1") -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Mock People Data Provider",
            capabilities={ProviderCapability.PEOPLE_DISCOVERY, ProviderCapability.PERSON_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(
                accuracy=0.5,
                coverage=0.4,
                freshness_days=30,
                cost_per_request=0.0,
                average_latency_ms=8,
                failure_rate=0.0,
            ),
        )

    @staticmethod
    def _fake_vendor_person_payload() -> list[dict]:
        return [
            {
                "person_ref": "mock-person-1",
                "full_name": "Jane Testperson",
                "job_title": "Head of Growth",
                "employer_domain": "example-test.invalid",
            },
            {
                "person_ref": "mock-person-2",
                "full_name": "Alex Sampleuser",
                "job_title": "CMO",
                "employer_domain": "sample-widgets.invalid",
            },
        ]

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.query.get("simulate_error"):
            return _simulated_error(self.provider_id, request.capability)

        raw_records = self._fake_vendor_person_payload()
        if request.capability == ProviderCapability.PEOPLE_DISCOVERY:
            requested_titles = request.query.get("titles")
            if requested_titles:
                wanted = {str(t).strip().lower() for t in requested_titles}
                raw_records = [r for r in raw_records if r["job_title"].strip().lower() in wanted]

        normalized = tuple(
            NormalizedRecord(
                external_id=raw["person_ref"],
                name=raw["full_name"],
                attributes={"title": raw["job_title"], "company_domain": raw["employer_domain"]},
            )
            for raw in raw_records
        )

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=normalized,
            cost=RequestCost(amount=0.0),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=True,
            ),
        )


class MockWebSearchProvider(ProviderAdapter):
    """Fakes a vendor offering generic web/search research."""

    def __init__(self, provider_id: str = "mock-web-search-v1") -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Mock Web Search Provider",
            capabilities={ProviderCapability.WEB_SEARCH},
            reliability_profile=ProviderReliabilityProfile(
                accuracy=0.3,
                coverage=0.9,
                freshness_days=1,
                cost_per_request=0.0,
                average_latency_ms=15,
                failure_rate=0.0,
            ),
        )

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if request.query.get("simulate_error"):
            return _simulated_error(self.provider_id, request.capability)

        term = request.query.get("q", "test query")
        raw_results = [
            {"result_url": "https://example.invalid/result-1", "headline": f"Mock result 1 for '{term}'"},
            {"result_url": "https://example.invalid/result-2", "headline": f"Mock result 2 for '{term}'"},
        ]
        normalized = tuple(
            NormalizedRecord(
                external_id=raw["result_url"],
                name=raw["headline"],
                attributes={"url": raw["result_url"]},
            )
            for raw in raw_results
        )

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=normalized,
            cost=RequestCost(amount=0.0),
            source=SourceMetadata(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                retrieved_at=_now(),
                is_mock=True,
            ),
        )
