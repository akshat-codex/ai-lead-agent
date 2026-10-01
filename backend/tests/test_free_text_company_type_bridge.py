"""The free-text evidence -> company_type bridge.

Root problem (confirmed via a live-code audit of app/providers/tavily.py::
execute() and app/providers/serper.py::execute()): neither web-search
discovery provider ever populates attributes["company_type_match_*"], so
a Tavily/Serper-only candidate's company_type evidence never reaches
SUPPORTED/SUPPORTED_STRUCTURED — app/services/hard_rule_engine.py's
company_type rule HOLDs on COMPANY_TYPE_UNKNOWN for every such candidate,
even when the candidate's own search-result description plainly states
it (e.g. "A D2C skincare brand..." against an ICP requiring company_type
"D2C"). This mirrors test_hermes_evidence_bridge.py's own industry-bridge
tests exactly, applied to app/services/hard_icp_validation.py::
_free_text_company_type_bridge instead of _free_text_industry_bridge —
same discipline: literal, case-insensitive substring containment of
EVERY one of the ICP's own already-stated company_type terms, never
fuzzy/semantic matching, never reading the "company_type" field itself.

Every test here calls validate_against_icp directly (pure, DB-free) — no
HTTP, no respx needed at this layer.
"""
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.canonical_icp import CanonicalGeography, CanonicalHardRules, CanonicalICP, CanonicalSoftPreferences, EmployeeRange
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.schemas.hard_rule_result import OverallResult, ReasonCode, RuleStatus
from app.services.hard_icp_validation import validate_against_icp

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
TAVILY_PROVIDER_ID = "tavily-company-discovery-v1"


def _icp(company_types: tuple[str, ...] = (), industries: tuple[str, ...] = (), icp_id: str = "icp-1") -> CanonicalICP:
    return CanonicalICP(
        icp_id=icp_id,
        version=1,
        hard_rules=CanonicalHardRules(
            industries=industries, company_types=company_types, geography=CanonicalGeography(), employee_range=EmployeeRange()
        ),
        soft_preferences=CanonicalSoftPreferences(),
    )


def _web_search_evidence(**field_values) -> list[EvidenceRecord]:
    """One EvidenceRecord per field=value pair, all from a single,
    untrusted Tavily-style sighting — company_identity/domain always
    included so validate_against_icp has a real candidate identity."""
    records = [
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="company_identity", value="Some Co",
            source_provider_id=TAVILY_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
    ]
    for field, value in field_values.items():
        records.append(
            EvidenceRecord(
                id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field=field, value=value,
                source_provider_id=TAVILY_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
                retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
            )
        )
    return records


def test_web_search_candidate_with_matching_description_passes_company_type():
    icp = _icp(company_types=("D2C",))
    evidence = _web_search_evidence(description="A fast-growing D2C skincare brand sold direct to consumers online.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    assert rule.status == RuleStatus.PASS
    assert result.evaluation.overall_result == OverallResult.PASS
    # Evidence is honestly cited — never an empty list for a bridged PASS.
    assert result.evidence_ids["company_type"] != ()


def test_web_search_candidate_with_unrelated_description_never_passes():
    icp = _icp(company_types=("D2C",))
    evidence = _web_search_evidence(description="A B2B enterprise software vendor serving Fortune 500 logistics teams.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    assert rule.status != RuleStatus.PASS
    assert result.evaluation.overall_result != OverallResult.PASS


def test_web_search_candidate_with_no_description_still_holds_never_crashes():
    icp = _icp(company_types=("D2C",))
    evidence = _web_search_evidence()  # no description field at all
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    assert rule.status == RuleStatus.HOLD
    assert rule.reason_code == ReasonCode.COMPANY_TYPE_UNKNOWN


def test_bridge_requires_every_company_type_term_not_just_one():
    icp = _icp(company_types=("D2C", "Subscription"))
    # Only proves "D2C" — "Subscription" is never mentioned, so the bridge
    # must not fire on a partial match.
    evidence = _web_search_evidence(description="A D2C skincare brand sold online.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    assert rule.status != RuleStatus.PASS


def test_bridge_never_bypasses_the_industry_rules_own_trust_gate():
    """The bridge only ever reads the "description" field — it must never
    read the "company_type" field itself (an untrusted single sighting on
    that field must still HOLD on its own, unaffected by this bridge's
    existence)."""
    icp = _icp(company_types=("D2C",))
    evidence = _web_search_evidence(company_type="D2C")  # a real company_type field, but only ONE untrusted sighting
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    # A lone untrusted sighting never reaches SUPPORTED, so it must still HOLD.
    assert rule.status == RuleStatus.HOLD


def test_bridge_never_overrides_an_already_supported_structured_company_type():
    """A candidate whose company_type ALREADY reached SUPPORTED/
    SUPPORTED_STRUCTURED via the existing structured path must never have
    that outcome second-guessed by weaker free-text evidence — simulated
    here via two independent, agreeing sightings (which the evidence
    engine promotes to SUPPORTED on its own)."""
    icp = _icp(company_types=("D2C",))
    evidence = [
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="company_identity", value="Some Co",
            source_provider_id=TAVILY_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="company_type", value="D2C",
            source_provider_id="tavily-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-1",
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="company_type", value="D2C",
            source_provider_id="serper-company-discovery-v1", source_type=SourceType.PROVIDER, external_id="ext-2",
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
        # A contradictory description that would otherwise satisfy nothing
        # — proves the structured value (D2C, already SUPPORTED via
        # agreement) wins, the free-text bridge is never even consulted.
        EvidenceRecord(
            id=str(uuid4()), entity_type=EntityType.COMPANY, entity_id="c1", field="description", value="An enterprise B2B vendor.",
            source_provider_id=TAVILY_PROVIDER_ID, source_type=SourceType.PROVIDER, external_id="ext-1",
            retrieved_at=NOW, confidence=ConfidenceLevel.UNKNOWN, created_at=NOW,
        ),
    ]
    result = validate_against_icp(icp, "c1", evidence, None, [])
    rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    assert rule.status == RuleStatus.PASS


def test_industry_and_company_type_bridges_can_both_fire_independently():
    """A Tavily/Serper-only candidate can now genuinely pass BOTH industry
    and company_type from the same description, closing the two biggest
    real-world gaps for web-search-only discovery (no Explorium/OpenAI
    configured) identified in the live diagnosis."""
    icp = _icp(industries=("Skincare",), company_types=("D2C",))
    evidence = _web_search_evidence(description="A D2C skincare brand selling organic skincare products online.")
    result = validate_against_icp(icp, "c1", evidence, None, [])
    industry_rule = next(r for r in result.evaluation.rule_results if r.rule == "industry")
    company_type_rule = next(r for r in result.evaluation.rule_results if r.rule == "company_type")
    assert industry_rule.status == RuleStatus.PASS
    assert company_type_rule.status == RuleStatus.PASS
    assert result.evaluation.overall_result == OverallResult.PASS
