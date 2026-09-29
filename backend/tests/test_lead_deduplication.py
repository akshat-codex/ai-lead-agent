from app.schemas.lead_deduplication import (
    ExistingLead,
    LeadDeduplicationDecision,
    LeadDeduplicationReasonCode,
    LeadSourceReference,
)
from app.services.lead_deduplication import deduplicate_lead


def _lead(id_, company_id, person_id=None) -> ExistingLead:
    return ExistingLead(id=id_, company_id=company_id, person_id=person_id)


# --- exact match: same company + same person ------------------------------


def test_exact_company_person_match_returns_existing_lead():
    existing = [_lead("lead-1", "company-1", "person-1")]
    result = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    assert result.decision == LeadDeduplicationDecision.MATCHED_EXISTING_LEAD
    assert result.lead_id == "lead-1"
    assert result.reason_code == LeadDeduplicationReasonCode.EXACT_COMPANY_PERSON_MATCH


def test_repeated_processing_is_idempotent_in_result_shape():
    existing = [_lead("lead-1", "company-1", "person-1")]
    first = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    second = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    assert first.decision == second.decision == LeadDeduplicationDecision.MATCHED_EXISTING_LEAD
    assert first.lead_id == second.lead_id


def test_same_lead_across_different_icps_matches_the_same_lead():
    existing = [_lead("lead-1", "company-1", "person-1")]
    result_a = deduplicate_lead("icp-a", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    result_b = deduplicate_lead("icp-b", 5, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    assert result_a.lead_id == result_b.lead_id == "lead-1"
    assert result_a.icp_id == "icp-a"
    assert result_b.icp_id == "icp-b"
    assert result_b.icp_version == 5


def test_same_lead_from_different_providers_still_matches():
    """Different discovery providers resolving to the same canonical
    company_id/person_id (Phase 7/10's job) must still collapse to one
    lead here — this module trusts the canonical ids, not provider origin."""
    existing = [_lead("lead-1", "company-1", "person-1")]
    source_a = LeadSourceReference(company_candidate_id="cand-provider-a")
    source_b = LeadSourceReference(company_candidate_id="cand-provider-b")
    result_a = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",), source=source_a)
    result_b = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",), source=source_b)
    assert result_a.lead_id == result_b.lead_id == "lead-1"
    assert result_a.source.company_candidate_id == "cand-provider-a"
    assert result_b.source.company_candidate_id == "cand-provider-b"


# --- different leads: same company different person, same person different company ---


def test_same_company_different_person_is_a_different_lead():
    existing = [_lead("lead-1", "company-1", "person-1")]
    result = deduplicate_lead("icp-1", 1, "company-1", "person-2", existing, person_associated_company_ids=("company-1",))
    assert result.decision == LeadDeduplicationDecision.NEW_LEAD
    assert result.lead_id != "lead-1"
    assert result.reason_code == LeadDeduplicationReasonCode.NEW_COMPANY_PERSON_PAIR


def test_same_person_different_company_without_association_is_unresolved():
    existing = [_lead("lead-1", "company-1", "person-1")]
    # person-1's OWN identity evidence (Phase 10) only associates them with company-1
    result = deduplicate_lead("icp-1", 1, "company-2", "person-1", existing, person_associated_company_ids=("company-1",))
    assert result.decision == LeadDeduplicationDecision.UNRESOLVED
    assert result.reason_code == LeadDeduplicationReasonCode.PERSON_NOT_CURRENTLY_ASSOCIATED_WITH_COMPANY
    assert result.lead_id is None


def test_same_person_different_company_with_proven_association_is_new_lead():
    """A person who has genuinely changed jobs (Phase 10 already recorded
    both companies in associated_company_ids) produces a new, distinct
    lead for the new company — never silently merged with the old one."""
    existing = [_lead("lead-1", "company-1", "person-1")]
    result = deduplicate_lead("icp-1", 1, "company-2", "person-1", existing, person_associated_company_ids=("company-1", "company-2"))
    assert result.decision == LeadDeduplicationDecision.NEW_LEAD
    assert result.company_id == "company-2"
    assert result.person_id == "person-1"


def test_company_only_lead_and_company_person_lead_are_distinct():
    existing = [_lead("lead-1", "company-1", None)]
    result = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    assert result.decision == LeadDeduplicationDecision.NEW_LEAD
    assert result.lead_id != "lead-1"


def test_company_only_match():
    existing = [_lead("lead-1", "company-1", None)]
    result = deduplicate_lead("icp-1", 1, "company-1", None, existing)
    assert result.decision == LeadDeduplicationDecision.MATCHED_EXISTING_LEAD
    assert result.lead_id == "lead-1"
    assert result.reason_code == LeadDeduplicationReasonCode.EXACT_COMPANY_ONLY_MATCH


def test_new_company_only_lead():
    result = deduplicate_lead("icp-1", 1, "company-9", None, [])
    assert result.decision == LeadDeduplicationDecision.NEW_LEAD
    assert result.reason_code == LeadDeduplicationReasonCode.NEW_COMPANY_ONLY


# --- similar names must never merge (this module never compares names at all) ---


def test_module_never_compares_names_only_canonical_ids():
    """Two candidates that happen to share a similar/identical human-readable
    name but were resolved (upstream, Phase 7/10) to two DIFFERENT canonical
    ids must never merge here — this module has no name field at all."""
    existing = [_lead("lead-1", "company-1", "person-1")]
    result = deduplicate_lead("icp-1", 1, "company-2", "person-2", existing, person_associated_company_ids=("company-2",))
    assert result.decision == LeadDeduplicationDecision.NEW_LEAD
    assert result.lead_id != "lead-1"


def test_deduplication_result_schema_has_no_name_field():
    import inspect

    from app.schemas.lead_deduplication import LeadDeduplicationRequest, LeadDeduplicationResult

    for schema in (LeadDeduplicationRequest, LeadDeduplicationResult):
        fields = schema.model_fields.keys()
        assert not any("name" in f.lower() for f in fields)


# --- unresolved identity ---------------------------------------------------


def test_no_company_id_is_unresolved():
    result = deduplicate_lead("icp-1", 1, None, "person-1", [])
    assert result.decision == LeadDeduplicationDecision.UNRESOLVED
    assert result.reason_code == LeadDeduplicationReasonCode.COMPANY_IDENTITY_UNRESOLVED
    assert result.lead_id is None


def test_ambiguity_never_becomes_a_duplicate_merge():
    """A candidate whose person identity is unresolved (person_id=None,
    because Phase 10 itself returned UNRESOLVED) must never be silently
    attached to any existing person-bearing lead at the same company —
    it is treated as its own company-only lead/match, never guessed into
    one of the ambiguous person-specific leads."""
    existing = [_lead("lead-1", "company-1", "person-1"), _lead("lead-2", "company-1", "person-2")]
    result = deduplicate_lead("icp-1", 1, "company-1", None, existing)
    assert result.decision == LeadDeduplicationDecision.NEW_LEAD
    assert result.lead_id not in {"lead-1", "lead-2"}
    assert result.person_id is None


def test_no_existing_leads_at_all_produces_unresolved_for_missing_company():
    result = deduplicate_lead("icp-1", 1, None, "person-1", [])
    assert result.decision == LeadDeduplicationDecision.UNRESOLVED


# --- request validation: at least one id required -------------------------


def test_request_requires_at_least_one_id():
    import pytest

    from app.schemas.lead_deduplication import LeadDeduplicationRequest

    with pytest.raises(Exception):
        LeadDeduplicationRequest(icp_id="icp-1")


# --- provenance is preserved -----------------------------------------------


def test_source_reference_is_preserved_through_the_result():
    source = LeadSourceReference(
        company_candidate_id="cc-1",
        company_resolution_id="cr-1",
        person_candidate_id="pc-1",
        person_resolution_id="pr-1",
    )
    result = deduplicate_lead("icp-1", 1, "company-1", "person-1", [], person_associated_company_ids=("company-1",), source=source)
    assert result.source == source


# --- determinism -------------------------------------------------------


def test_deduplication_is_deterministic():
    existing = [_lead("lead-1", "company-1", "person-1")]
    first = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    second = deduplicate_lead("icp-1", 1, "company-1", "person-1", existing, person_associated_company_ids=("company-1",))
    assert first.decision == second.decision
    assert first.lead_id == second.lead_id
    assert first.reason_code == second.reason_code


# --- no identity system duplication ----------------------------------------


def test_service_never_imports_company_or_person_resolution_logic():
    import inspect

    import app.services.lead_deduplication as module

    source = inspect.getsource(module)
    for forbidden in ("resolve_candidate", "normalize_company_name", "normalize_person_name", "extract_linkedin_identifier"):
        assert forbidden not in source
