from datetime import datetime, timezone

from app.schemas.candidate_person import CandidatePerson
from app.schemas.company_resolution import ResolutionStatus
from app.schemas.person_resolution import ExistingPersonIdentity, PersonResolutionReasonCode
from app.services.person_resolution import resolve_candidate, resolve_candidates


def _candidate(**overrides) -> CandidatePerson:
    base = dict(
        id="cand-1",
        company_id="company-1",
        icp_id="icp-1",
        icp_version=1,
        provider_id="provider-a",
        external_id="ext-1",
        name="Jane Testperson",
        title="CMO",
        attributes={"title": "CMO"},
        discovered_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return CandidatePerson(**base)


def _person(**overrides) -> ExistingPersonIdentity:
    base = dict(
        id="person-1",
        canonical_name="Jane Testperson",
        canonical_company_id="company-1",
        associated_company_ids=("company-1",),
        aliases=(),
        linkedin_id=None,
        provider_identities={},
    )
    base.update(overrides)
    return ExistingPersonIdentity(**base)


# --- trusted provider identity -> MATCH -----------------------------------


def test_trusted_provider_identity_matches():
    person = _person(provider_identities={"provider-a": "ext-1"})
    candidate = _candidate(provider_id="provider-a", external_id="ext-1")
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.MATCH
    assert result.canonical_person_id == person.id
    assert result.reason_code == PersonResolutionReasonCode.TRUSTED_PROVIDER_IDENTITY
    assert "provider_identity" in result.matched_signals


def test_provider_identity_from_a_different_provider_does_not_match():
    person = _person(provider_identities={"provider-a": "ext-1"})
    candidate = _candidate(provider_id="provider-b", external_id="ext-1", name="Someone Else")
    result = resolve_candidate(candidate, [person])
    assert result.status != ResolutionStatus.MATCH


def test_trusted_provider_identity_with_company_conflict_is_unresolved():
    """Unlike Phase 7's company resolution (which tolerates a
    domain-match-with-name-conflict as a flagged MATCH), a person match is
    held to a stricter bar: a confirmed provider identity whose company
    association conflicts with what's on record is UNRESOLVED, not a
    flagged MATCH — reconciling a job change is a later phase's job."""
    person = _person(provider_identities={"provider-a": "ext-1"}, canonical_company_id="old-company", associated_company_ids=("old-company",))
    candidate = _candidate(provider_id="provider-a", external_id="ext-1", company_id="new-company")
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == PersonResolutionReasonCode.IDENTITY_SIGNAL_CONFLICT
    assert "company" in result.conflicting_signals
    assert result.matched_person_id == person.id


# --- same normalized identity (LinkedIn) where safely supported -----------


def test_linkedin_identifier_match():
    person = _person(linkedin_id="in/janetestperson")
    candidate = _candidate(attributes={"linkedin_id": "https://www.linkedin.com/in/janetestperson/"})
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.MATCH
    assert result.reason_code == PersonResolutionReasonCode.LINKEDIN_ID_MATCH
    assert "linkedin_id" in result.matched_signals


def test_new_unique_linkedin_identifier_is_new_not_unresolved():
    candidate = _candidate(attributes={"linkedin_id": "in/brandnewperson"})
    result = resolve_candidate(candidate, [])
    assert result.status == ResolutionStatus.NEW
    assert result.reason_code == PersonResolutionReasonCode.NEW_UNIQUE_LINKEDIN_ID


def test_linkedin_match_with_company_conflict_is_unresolved():
    person = _person(linkedin_id="in/janetestperson", canonical_company_id="old-company", associated_company_ids=("old-company",))
    candidate = _candidate(company_id="new-company", attributes={"linkedin_id": "in/janetestperson"})
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == PersonResolutionReasonCode.IDENTITY_SIGNAL_CONFLICT
    assert "company" in result.conflicting_signals


# --- same person across repeated discovery / different ICPs --------------


def test_repeated_discovery_via_same_provider_identity_matches():
    person = _person(provider_identities={"provider-a": "ext-1"})
    first = resolve_candidate(_candidate(id="cand-1", icp_id="icp-a"), [person])
    second = resolve_candidate(_candidate(id="cand-2", icp_id="icp-b"), [person])

    assert first.status == ResolutionStatus.MATCH
    assert second.status == ResolutionStatus.MATCH
    assert first.canonical_person_id == second.canonical_person_id == person.id


# --- same name, different company -> not automatically merged -------------


def test_same_name_different_company_is_not_merged():
    person = _person(canonical_company_id="company-1", associated_company_ids=("company-1",))
    candidate = _candidate(company_id="company-2", provider_id="unrelated-provider", external_id="z")
    result = resolve_candidate(candidate, [person])

    assert result.status != ResolutionStatus.MATCH
    assert result.status == ResolutionStatus.NEW
    assert result.reason_code == PersonResolutionReasonCode.NEW_DIFFERENT_COMPANY_CONTEXT


def test_same_name_different_company_produces_a_distinct_canonical_person():
    people = [_person(id="p1"), _person(id="p2", canonical_company_id="company-2", associated_company_ids=("company-2",))]
    candidate = _candidate(company_id="company-3", provider_id="unrelated-provider", external_id="z")
    result = resolve_candidate(candidate, people)
    assert result.status == ResolutionStatus.NEW
    assert result.canonical_person_id not in {"p1", "p2"}


# --- same name only -> UNRESOLVED ------------------------------------------


def test_same_name_and_company_without_strong_identifier_is_unresolved():
    person = _person()
    candidate = _candidate(provider_id="different-provider", external_id="z")  # same name+company, no trusted signal
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == PersonResolutionReasonCode.NAME_AND_COMPANY_INSUFFICIENT
    assert result.canonical_person_id is None
    assert result.matched_person_id == person.id  # hint only, not a commitment


def test_name_matching_multiple_people_at_the_same_company_is_unresolved():
    people = [
        _person(id="p1"),
        _person(id="p2"),
    ]
    candidate = _candidate(provider_id="different-provider", external_id="z")
    result = resolve_candidate(candidate, people)

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == PersonResolutionReasonCode.AMBIGUOUS_NAME_MULTIPLE_MATCHES
    assert result.matched_person_id is None


def test_never_forces_a_merge_on_uncertain_evidence():
    person = _person()
    candidate = _candidate(provider_id="unrelated-provider", external_id="z")
    result = resolve_candidate(candidate, [person])
    assert result.status != ResolutionStatus.MATCH


# --- conflicting provider identities -> UNRESOLVED -------------------------


def test_conflicting_provider_identity_and_matching_name_is_unresolved():
    person = _person(provider_identities={"provider-a": "old-ext-id"})
    candidate = _candidate(provider_id="provider-a", external_id="new-ext-id")
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == PersonResolutionReasonCode.PROVIDER_IDENTITY_CONFLICT
    assert "provider_external_id" in result.conflicting_signals


# --- conflicting company associations -> UNRESOLVED ------------------------


def test_conflicting_provider_identity_at_the_same_company_is_unresolved():
    """Same provider, a different external id, same name, same company —
    genuinely ambiguous: could be a data-quality issue or two different
    people; not resolvable either way without stronger evidence."""
    person = _person(provider_identities={"provider-a": "old-ext-id"}, canonical_company_id="company-1", associated_company_ids=("company-1",))
    candidate = _candidate(provider_id="provider-a", external_id="new-ext-id", company_id="company-1")
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == PersonResolutionReasonCode.PROVIDER_IDENTITY_CONFLICT
    assert "provider_external_id" in result.conflicting_signals


def test_conflicting_provider_identity_with_a_different_company_becomes_new():
    """Here the provider disagreement AND the company both point the same
    direction (different person) — treated as NEW, not ambiguous, the same
    way plain "same name, different company" is."""
    person = _person(provider_identities={"provider-a": "old-ext-id"}, canonical_company_id="company-1", associated_company_ids=("company-1",))
    candidate = _candidate(provider_id="provider-a", external_id="new-ext-id", company_id="company-2")
    result = resolve_candidate(candidate, [person])

    assert result.status == ResolutionStatus.NEW
    assert result.reason_code == PersonResolutionReasonCode.NEW_DIFFERENT_COMPANY_CONTEXT


# --- multiple distinct people with the same name remain distinct ----------


def test_multiple_distinct_people_with_the_same_name_remain_distinct():
    candidates = [
        _candidate(id="c1", name="John Smith", company_id="company-a", provider_id="p", external_id="1"),
        _candidate(id="c2", name="John Smith", company_id="company-b", provider_id="p", external_id="2"),
    ]
    results = resolve_candidates(candidates, [])

    assert results[0][0].status == ResolutionStatus.NEW
    assert results[1][0].status == ResolutionStatus.NEW
    assert results[0][0].canonical_person_id != results[1][0].canonical_person_id


# --- batch behavior: within-batch matching, determinism -------------------


def test_two_candidates_for_a_brand_new_person_in_one_batch_match_each_other():
    candidates = [
        _candidate(id="c1", provider_id="provider-a", external_id="1"),
        _candidate(id="c2", provider_id="provider-b", external_id="9"),
    ]
    results = resolve_candidates(candidates, [])

    assert results[0][0].status == ResolutionStatus.NEW
    assert results[1][0].status == ResolutionStatus.UNRESOLVED  # same name+company, no trusted id yet


def test_resolution_is_deterministic_for_identical_input():
    person = _person(provider_identities={"provider-a": "ext-1"})
    candidate = _candidate(provider_id="provider-a", external_id="ext-1")

    first = resolve_candidate(candidate, [person])
    second = resolve_candidate(candidate, [person])

    assert first.status == second.status
    assert first.canonical_person_id == second.canonical_person_id
    assert first.reason_code == second.reason_code
    assert first.matched_signals == second.matched_signals


# --- no fabricated data / no qualification fields --------------------------


def test_no_linkedin_is_fabricated_when_not_supplied():
    candidate = _candidate(attributes={"title": "CMO"})
    result = resolve_candidate(candidate, [])
    assert result.status == ResolutionStatus.NEW
    assert result.reason_code != PersonResolutionReasonCode.NEW_UNIQUE_LINKEDIN_ID


def test_canonical_person_identity_schema_has_no_qualification_fields():
    field_names = set(ExistingPersonIdentity.model_fields.keys())
    assert field_names.isdisjoint({"verified", "qualified", "score", "email", "email_verified", "current_title_verified"})
