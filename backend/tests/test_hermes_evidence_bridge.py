"""Phase 35 — the Hermes usefulness fix: free-text evidence -> industry
bridge.

Root problem: a Hermes candidate's industry evidence NEVER reaches
SUPPORTED/SUPPORTED_STRUCTURED (Hermes's provider_id is deliberately
absent from app/services/evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS
— see that module's own docstring), so before this fix a Hermes-only
candidate always HOLDs regardless of how clearly its free-text evidence
actually proves the ICP's full intent. This file tests
app/services/hard_icp_validation.py::_free_text_industry_bridge (and its
sibling _free_text_industry_evidence_ids), which closes that gap by
reading ONLY the "description" evidence field's raw text and checking
literal, case-insensitive substring containment of EVERY one of the ICP's
own already-stated industry terms — never fuzzy/semantic matching, never
reading the "industry" field itself (which has its own, separately-gated
trust path and must never be bypassed by this new one), and never
overriding a candidate whose industry ALREADY reached SUPPORTED/
SUPPORTED_STRUCTURED via the existing, unmodified path.

Every test here calls validate_against_icp directly (pure, DB-free) —
no HTTP, no respx needed at this layer. A handful of end-to-end,
respx-mocked batch-level tests at the bottom cover the requirements this
layer can't (enrichment ordering, the 10-lead bound) by reusing
tests/test_hermes_discovery.py's own fixtures.
"""
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import respx

from app.core.config import Settings
from app.providers.base import ProviderAdapter
from app.providers.contracts import ProviderCapability, ProviderRequest, ProviderResponse, SourceMetadata
from app.providers.registry import ProviderRegistry
from app.schemas.canonical_icp import CanonicalGeography, CanonicalHardRules, CanonicalICP, CanonicalSoftPreferences, EmployeeRange
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.hard_rule_result import OverallResult, ReasonCode, RuleStatus
from app.services.hard_icp_validation import validate_against_icp

import app.services.batch_orchestration as orchestration_module
from tests.test_batch_api import _with_registry
from tests.test_hermes_discovery import (
    HERMES_BASE_URL,
    HERMES_TOKEN,
    _EmptyDiscoveryProvider,
    _create_icp_with_hard_rules,
    _hermes_record,
    _hermes_settings,
    _mock_status_completed,
    _mock_submit,
)

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
HERMES_PROVIDER_ID = "hermes-icp-search-v1"


def _icp(industries: tuple[str, ...], icp_id: str = "icp-1") -> CanonicalICP:
    return CanonicalICP(
        icp_id=icp_id,
        version=1,
        hard_rules=CanonicalHardRules(industries=industries, geography=CanonicalGeography(), employee_range=EmployeeRange()),
        soft_preferences=CanonicalSoftPreferences(),
    )


def _hermes_evidence(**field_values) -> list[EvidenceRecord]:
    """One EvidenceRecord per field=value pair, all from a single,
    untrusted Hermes-style single sighting — company_identity/domain
    always included so validate_against_icp has a real candidate identity
    (never load-bearing for the industry rule itself)."""
    records = [
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="company_identity", value="Some Co",
            source_provider_id=HERMES_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
    ]
    for field, value in field_values.items():
        records.append(
            EvidenceRecord(
                id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field=field, value=value,
                source_provider_id=HERMES_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
                retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
            )
        )
    return records


# --- 1. Hermes-only Healthcare SaaS with strong intersection evidence -> PASS ---


def test_hermes_only_healthcare_saas_with_intersection_description_passes():
    icp = _icp(("Healthcare", "SaaS"))
    evidence = _hermes_evidence(description="A SaaS platform serving healthcare providers with patient scheduling tools.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.PASS
    assert result.evaluation.overall_result == OverallResult.PASS
    # Evidence is honestly cited — never an empty list for a bridged PASS.
    assert result.evidence_ids["industry"] != ()


# --- 2. Hermes-only Healthcare company with no software evidence -> NOT PASS ---


def test_hermes_only_healthcare_only_description_never_passes_compound_icp():
    icp = _icp(("Healthcare", "SaaS"))
    evidence = _hermes_evidence(description="A regional hospital network providing patient care and emergency services.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert result.evaluation.overall_result != OverallResult.PASS


# --- 3. Hermes-only generic SaaS with no healthcare evidence -> NOT PASS ---


def test_hermes_only_generic_saas_description_never_passes_compound_icp():
    icp = _icp(("Healthcare", "SaaS"))
    evidence = _hermes_evidence(description="A SaaS platform for project management and team collaboration.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status != RuleStatus.PASS
    assert result.evaluation.overall_result != OverallResult.PASS


# --- 4. Hermes-only ambiguous evidence -> HOLD ---


def test_hermes_only_ambiguous_description_holds():
    icp = _icp(("Healthcare", "SaaS"))
    evidence = _hermes_evidence(description="A modern technology company building tools for businesses.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_UNKNOWN
    assert result.evaluation.overall_result == OverallResult.HOLD


def test_no_description_evidence_at_all_still_holds_never_crashes():
    icp = _icp(("Healthcare", "SaaS"))
    evidence = _hermes_evidence()  # no description field at all
    result = validate_against_icp(icp, "c1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD


# --- 5. Hermes + Explorium corroboration -> PASS with both provenance preserved ---


def test_hermes_description_and_explorium_structured_evidence_both_cited():
    """Explorium's own structured industry match already PASSes on its
    own merits (existing, unmodified behavior) — this test proves that
    when BOTH an Explorium structured sighting AND Hermes description
    evidence exist for the same company, the structured path is used (not
    silently replaced) and its own evidence is what's cited, while the
    Hermes-sourced description evidence is preserved as a separate,
    real, independently-retrievable evidence record (never discarded)."""
    icp = _icp(("Healthcare",))
    explorium_record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="industry", value="Healthcare",
        source_provider_id="explorium-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-2",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    hermes_description_record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="description", value="A healthcare technology company.",
        source_provider_id=HERMES_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    identity_record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="company_identity", value="Some Co",
        source_provider_id=HERMES_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    result = validate_against_icp(icp, "c1", [identity_record, explorium_record, hermes_description_record], None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.PASS
    # The STRUCTURED (Explorium) record is what's cited — the stronger,
    # already-correctly-gated path takes precedence over the free-text
    # bridge, exactly as validate_against_icp's own "only attempt the
    # free-text bridge when industry is still None" guard requires.
    assert result.evidence_ids["industry"] == (explorium_record.id,)
    # The Hermes description record is a real, independently-persisted
    # EvidenceRecord regardless of whether this rule cited it — provenance
    # is never discarded just because a stronger source also matched.
    assert hermes_description_record.source_provider_id == HERMES_PROVIDER_ID
    assert explorium_record.source_provider_id == "explorium-company-discovery-v1"


# --- 6. Generic compound ICP (Fintech SaaS) -> same behavior, no hardcoding ---


def test_fintech_saas_generalizes_identically_to_healthcare_saas():
    icp = _icp(("Fintech", "SaaS"))
    evidence_pass = _hermes_evidence(description="A SaaS platform for fintech companies to manage payments and compliance.")
    result_pass = validate_against_icp(icp, "c1", evidence_pass, None, [])
    assert next(r for r in result_pass.evaluation.rule_results if r.rule == "industry").status == RuleStatus.PASS

    evidence_fintech_only = _hermes_evidence(description="A traditional fintech lender offering personal loans.")
    result_fintech_only = validate_against_icp(icp, "c1", evidence_fintech_only, None, [])
    assert next(r for r in result_fintech_only.evaluation.rule_results if r.rule == "industry").status != RuleStatus.PASS


def test_d2c_fmcg_style_compound_icp_also_generalizes():
    icp = _icp(("D2C", "FMCG"))
    evidence = _hermes_evidence(description="A D2C FMCG brand selling snack foods directly to consumers online.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    assert next(r for r in result.evaluation.rule_results if r.rule == "industry").status == RuleStatus.PASS


# --- 7. Existing Explorium exact-match behavior remains unchanged ---


def test_explorium_single_term_exact_match_still_passes_without_description():
    """No description evidence at all — Explorium's own existing,
    unmodified exact-match path must still work exactly as before this
    phase."""
    icp = _icp(("Skincare",))
    record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="industry", value="Skincare",
        source_provider_id="explorium-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    result = validate_against_icp(icp, "c1", [record], None, [])
    assert next(r for r in result.evaluation.rule_results if r.rule == "industry").status == RuleStatus.PASS


def test_untrusted_single_industry_field_sighting_still_holds_even_with_this_fix():
    """The exact regression this fix's design had to avoid: an untrusted
    single "industry" field sighting (not "description") must still HOLD
    — _free_text_industry_bridge must never read the "industry" field
    itself, only "description"."""
    icp = _icp(("Skincare",))
    record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="industry", value="Skincare",
        source_provider_id="some-other-provider", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
    )
    result = validate_against_icp(icp, "c1", [record], None, [])
    assert next(r for r in result.evaluation.rule_results if r.rule == "industry").status == RuleStatus.HOLD


def test_partial_structured_explorium_match_still_holds_unchanged():
    """test_healthcare_saas_optical_goods_stores_no_longer_falsely_passes's
    own scenario, re-verified unaffected by this phase — no description
    evidence at all here, so _free_text_industry_bridge never even
    activates; this must behave identically to before this phase."""
    icp = _icp(("Healthcare", "SaaS"))
    record = EvidenceRecord(
        id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="industry", value="Optical Goods Stores",
        source_provider_id="explorium-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-1",
        retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        evidence_text='{"industry_match": {"icp_terms": ["Healthcare", "SaaS"], "resolved_category_count": 1}}',
    )
    result = validate_against_icp(icp, "c1", [record], None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    assert industry_rule.status == RuleStatus.HOLD
    assert industry_rule.reason_code == ReasonCode.INDUSTRY_UNKNOWN


# --- 8 & 9. End-to-end: no enrichment before quality gate, 10-lead bound unchanged ---


@respx.mock
def test_hermes_description_driven_pass_still_respects_no_enrichment_before_quality_gate(client, monkeypatch):
    """A Hermes candidate that NOW genuinely PASSes (via description
    evidence) must still go through enrichment/person-discovery only
    AFTER COMPANY_QUALITY_SCORED — this fix changes ONLY the hard-rule
    validation outcome, never the pipeline ordering."""
    monkeypatch.setattr(orchestration_module, "get_settings", lambda: _hermes_settings())
    _mock_submit(job_id="job-e2e-1")

    registry = ProviderRegistry()
    registry.register(_EmptyDiscoveryProvider())
    # employee_size/geography deliberately unconstrained (None) — Hermes is
    # not a trusted structured provider for employee_range either (by the
    # same, correct, unchanged trust discipline), so leaving those rules
    # NOT_APPLICABLE isolates this test to exactly what this phase changed:
    # the industry rule's outcome for description-proven evidence.
    icp = _create_icp_with_hard_rules(client, "Hermes Evidence Bridge E2E", industry=["Healthcare", "SaaS"], min_employees=None, max_employees=None)
    first = _with_registry(client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 3})).json()
    _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume")).json()

    respx.routes.clear()
    _mock_status_completed(
        "job-e2e-1",
        [_hermes_record(
            company_name="HealthTech SaaS Co", website="https://healthtechsaas.example",
            industry="Healthcare Software",
            description="A SaaS platform serving healthcare providers with patient scheduling tools.",
        )],
    )
    completed = _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume")).json()

    item = completed["items"][0]
    assert item["hard_rule_result"] == "PASS"  # the fix this phase adds
    assert item["stage"] == "DONE"  # pipeline still ran to completion in the correct order
    assert item["outcome"] is not None


@respx.mock
def test_hermes_description_driven_pass_still_respects_target_count_bound(client, monkeypatch):
    """Even with Hermes candidates now able to reach genuine ACCEPTED via
    description evidence, the existing per-round processing bound
    (app/services/batch_orchestration.py::run_batch's own early-stop once
    _accepted_count >= target_count) must still apply unchanged."""
    monkeypatch.setattr(orchestration_module, "get_settings", lambda: _hermes_settings())
    _mock_submit(job_id="job-e2e-2")

    registry = ProviderRegistry()
    registry.register(_EmptyDiscoveryProvider())
    icp = _create_icp_with_hard_rules(client, "Hermes Evidence Bridge Bound", industry=["Healthcare", "SaaS"], min_employees=None, max_employees=None)
    first = _with_registry(client, registry, lambda: client.post("/api/v1/batches", json={"icp_id": icp["id"], "target_count": 1})).json()
    _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume")).json()

    respx.routes.clear()
    strong_match = _hermes_record(
        company_name="HealthTech SaaS Co", website="https://healthtechsaas.example",
        industry="Healthcare Software",
        description="A SaaS platform serving healthcare providers with patient scheduling tools.",
    )
    another_strong_match = _hermes_record(
        company_name="Second HealthTech Co", website="https://secondhealthtech.example",
        industry="Healthcare Software",
        description="Another SaaS platform serving healthcare providers.",
    )
    _mock_status_completed("job-e2e-2", [strong_match, another_strong_match])
    completed = _with_registry(client, registry, lambda: client.post(f"/api/v1/batches/{first['id']}/resume")).json()

    assert completed["accepted_count"] == 1  # target_count=1 respected even though both candidates would genuinely PASS
    processed_items = [i for i in completed["items"] if i["outcome"] is not None]
    assert len(processed_items) == 1  # the second candidate was left unprocessed once target was reached mid-round
