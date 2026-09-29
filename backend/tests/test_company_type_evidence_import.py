"""P1 fix — company_type evidence import regression tests.

Root cause traced in app/services/evidence_import.py's own
_company_type_structured_match docstring: Explorium's /businesses response
has no field stating a company's type at all (unlike naics_description ->
industry), so before this fix nothing ever wrote company_type evidence,
and the company_type hard rule could never resolve from Explorium-only
evidence — always HOLD, regardless of how the company was actually found.

This file tests the fix at the unit level (the pure functions in
evidence_import.py) and via the real evidence-import HTTP endpoint,
mirroring test_evidence_api.py's own
test_import_company_evidence_reads_industry_and_country_from_discovery
pattern for industry/country.
"""
from datetime import datetime, timezone

from app.main import app
from app.providers.base import ProviderAdapter
from app.providers.contracts import NormalizedRecord, ProviderCapability, ProviderRequest, ProviderResponse, SourceMetadata
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceStatus, SourceType
from app.services.evidence_engine import compute_field_status
from app.services.evidence_import import _company_type_match_provenance, _company_type_structured_match


def _icp_payload(name: str, company_type=None) -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": [],
            "geography": [],
            "min_employees": None,
            "max_employees": None,
            "allowed_titles": [],
            "company_type": ["D2C"] if company_type is None else company_type,
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": [], "commercial_signals": [], "growth_signals": [],
            "marketing_signals": [], "other_preferences": [],
        },
    }


def _create_icp(client, name: str, **overrides) -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name, **overrides)).json()


class _FixedCompanyProvider(ProviderAdapter):
    def __init__(self, provider_id: str, records: list[NormalizedRecord]):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})
        self._records = records

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, capability=request.capability, success=True, data=tuple(self._records),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=True),
        )


def _resolve_a_company(client, icp_id: str, attributes: dict, domain: str = "d2c-brand.invalid") -> str:
    registry = ProviderRegistry()
    registry.register(
        _FixedCompanyProvider(
            "explorium-company-discovery-v1",
            [NormalizedRecord(external_id="ext-1", name="D2C Brand Co", attributes={"domain": domain, **attributes})],
        )
    )
    app.dependency_overrides[get_provider_registry] = lambda: registry
    try:
        run = client.post("/api/v1/discovery/runs", json={"icp_id": icp_id}).json()
    finally:
        del app.dependency_overrides[get_provider_registry]
    resolution = client.post("/api/v1/companies/resolve", json={"discovery_run_id": run["id"]}).json()
    return resolution[0]["canonical_company_id"]


# --- unit level: _company_type_structured_match / _company_type_match_provenance ---


def test_unambiguous_single_term_structured_match_is_surfaced():
    attributes = {
        "company_type_match_branch": "linkedin_category",
        "company_type_match_terms": ["D2C"],
        "company_type_match_resolved_category_count": 1,
    }
    match = _company_type_structured_match(attributes)
    assert match == ("D2C", "linkedin_category")


def test_naics_branch_is_surfaced_the_same_way():
    attributes = {
        "company_type_match_branch": "naics_category",
        "company_type_match_terms": ["Nonprofit"],
        "company_type_match_resolved_category_count": 1,
    }
    assert _company_type_structured_match(attributes) == ("Nonprofit", "naics_category")


def test_ambiguous_multi_term_branch_is_never_surfaced():
    """The same same-branch OR-list confound _bridged_industry_terms
    guards against for industry: Explorium merges multiple resolved
    category values into one branch response with no per-value
    attribution, so a branch that resolved more than one distinct
    company_type term must never be attributed to any single term."""
    attributes = {
        "company_type_match_branch": "linkedin_category",
        "company_type_match_terms": ["D2C", "PE-backed"],
        "company_type_match_resolved_category_count": 2,
    }
    assert _company_type_structured_match(attributes) is None


def test_mismatched_resolved_count_is_never_surfaced():
    """A single term listed but a resolved_category_count that disagrees
    (a confound signal from a merged cross-branch record) must not be
    trusted either — the count and the term list must agree."""
    attributes = {
        "company_type_match_branch": "linkedin_category",
        "company_type_match_terms": ["D2C"],
        "company_type_match_resolved_category_count": 2,
    }
    assert _company_type_structured_match(attributes) is None


def test_keyword_fallback_attributes_are_never_surfaced():
    """A low-precision website_keywords hit carries no
    company_type_match_branch at all — must stay unsurfaced exactly as
    before this fix."""
    attributes = {"keyword_match_terms": ["D2C"], "keyword_match_term_sources": ["company_type"]}
    assert _company_type_structured_match(attributes) is None


def test_no_attributes_returns_none():
    assert _company_type_structured_match(None) is None
    assert _company_type_structured_match({}) is None


def test_provenance_payload_records_the_taxonomy_field_and_term():
    attributes = {
        "company_type_match_branch": "linkedin_category",
        "company_type_match_terms": ["D2C"],
        "company_type_match_resolved_category_count": 1,
    }
    text = _company_type_match_provenance(attributes)
    assert text is not None
    assert '"taxonomy_field": "linkedin_category"' in text
    assert '"icp_term": "D2C"' in text


def test_provenance_is_none_when_match_is_none():
    assert _company_type_match_provenance({"keyword_match_terms": ["D2C"]}) is None


# --- end-to-end: evidence import + hard-rule status --------------------


def test_import_company_evidence_reads_company_type_from_structured_discovery_match(client):
    icp = _create_icp(client, "ICP CompanyType A")
    company_id = _resolve_a_company(
        client, icp["id"],
        {
            "company_type_match_branch": "linkedin_category",
            "company_type_match_terms": ["D2C"],
            "company_type_match_resolved_category_count": 1,
        },
    )
    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    added = response.json()["added"]
    by_field = {r["field"]: r for r in added}
    assert by_field["company_type"]["value"] == "D2C"
    assert by_field["company_type"]["source_provider_id"] == "explorium-company-discovery-v1"
    assert by_field["company_type"]["confidence"] == "UNKNOWN"  # never fabricated, honest UNKNOWN


def test_company_type_from_structured_match_reaches_supported_structured_status():
    """The concrete fix to the P1 finding: a single, genuinely
    unambiguous structured company_type sighting must now be usable by
    the hard-rule engine (SUPPORTED_STRUCTURED), not stuck at
    INSUFFICIENT forever."""
    from app.schemas.evidence import EvidenceRecord
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field="company_type",
        value="D2C", source_provider_id="explorium-company-discovery-v1", source_type=SourceType.PROVIDER,
        retrieved_at=now, confidence=ConfidenceLevel.UNKNOWN, created_at=now,
    )
    status = compute_field_status("company_type", [record])
    assert status == EvidenceStatus.SUPPORTED_STRUCTURED


def test_company_type_keyword_fallback_still_insufficient():
    """The low-precision keyword tier must remain exactly as untrusted as
    before this fix — this test would fail if the trust allowlist were
    accidentally widened beyond the structured branch."""
    from app.schemas.evidence import EvidenceRecord
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="company-1", field="company_type",
        value="D2C", source_provider_id="mock-company-data-v1", source_type=SourceType.PROVIDER,
        retrieved_at=now, confidence=ConfidenceLevel.UNKNOWN, created_at=now,
    )
    status = compute_field_status("company_type", [record])
    assert status == EvidenceStatus.INSUFFICIENT


def test_import_company_evidence_omits_company_type_when_no_structured_match(client):
    """A candidate found only via the keyword-fallback tier (no
    company_type_match_* attributes at all) must not get any
    company_type evidence written — exactly as before this fix."""
    icp = _create_icp(client, "ICP CompanyType B")
    company_id = _resolve_a_company(client, icp["id"], {})
    response = client.post("/api/v1/evidence/import", json={"entity_type": "COMPANY", "entity_id": company_id})
    assert response.status_code == 200
    fields = {r["field"] for r in response.json()["added"]}
    assert "company_type" not in fields
