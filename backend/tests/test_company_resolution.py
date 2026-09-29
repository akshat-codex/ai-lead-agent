from datetime import datetime, timezone

from app.schemas.candidate_company import CandidateCompany
from app.schemas.company_resolution import (
    ExistingCompanyIdentity,
    ResolutionReasonCode,
    ResolutionStatus,
)
from app.services.company_resolution import resolve_candidate, resolve_candidates


def _candidate(**overrides) -> CandidateCompany:
    base = dict(
        id="cand-1",
        icp_id="icp-1",
        icp_version=1,
        provider_id="provider-a",
        external_id="ext-1",
        name="Example Test Co",
        domain=None,
        attributes={},
        discovered_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return CandidateCompany(**base)


def _company(**overrides) -> ExistingCompanyIdentity:
    base = dict(
        id="company-1",
        canonical_name="Example Test Co",
        canonical_domain=None,
        aliases=(),
        provider_identities={},
    )
    base.update(overrides)
    return ExistingCompanyIdentity(**base)


# --- same domain -> MATCH ------------------------------------------------


def test_same_domain_matches():
    company = _company(canonical_domain="example-test.invalid")
    candidate = _candidate(domain="example-test.invalid")
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.MATCH
    assert result.canonical_company_id == company.id
    assert result.reason_code == ResolutionReasonCode.DOMAIN_MATCH
    assert "domain" in result.matched_signals


def test_normalized_domain_variants_match():
    company = _company(canonical_domain="example-test.invalid")
    candidate = _candidate(domain="https://www.example-test.invalid/")
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.MATCH
    assert result.canonical_company_id == company.id


# --- trusted provider identity -> MATCH -----------------------------------


def test_trusted_provider_identity_matches_even_without_domain():
    company = _company(provider_identities={"provider-a": "ext-1"})
    candidate = _candidate(provider_id="provider-a", external_id="ext-1", domain=None)
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.MATCH
    assert result.reason_code == ResolutionReasonCode.TRUSTED_PROVIDER_IDENTITY
    assert "provider_identity" in result.matched_signals


def test_provider_identity_from_a_different_provider_does_not_match():
    company = _company(provider_identities={"provider-a": "ext-1"})
    candidate = _candidate(provider_id="provider-b", external_id="ext-1", domain=None, name="Totally Different Co")
    result = resolve_candidate(candidate, [company])

    assert result.status != ResolutionStatus.MATCH


# --- strong name+domain combination (safe) --------------------------------


def test_exact_name_and_domain_together_is_a_confident_match_with_no_conflicts():
    company = _company(canonical_name="Example Test Co", canonical_domain="example-test.invalid")
    candidate = _candidate(name="Example Test Co", domain="example-test.invalid")
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.MATCH
    assert result.conflicting_signals == ()


# --- different domains -> separate ----------------------------------------


def test_different_domains_produce_separate_companies():
    candidates = [
        _candidate(id="c1", provider_id="p", external_id="1", name="Acme", domain="acme-widgets.invalid"),
        _candidate(id="c2", provider_id="p", external_id="2", name="Acme", domain="acme-anvils.invalid"),
    ]
    results = resolve_candidates(candidates, [])
    statuses = [r.status for r, _ in results]
    ids = {r.canonical_company_id for r, _ in results}

    assert statuses == [ResolutionStatus.NEW, ResolutionStatus.NEW]
    assert len(ids) == 2  # not merged despite the identical name


def test_similar_names_but_different_domains_are_not_merged():
    """Same normalized name ("acme"), clearly different domains — domain
    evidence is decisive and must not be overridden by name similarity."""
    candidates = [
        _candidate(id="c1", provider_id="p", external_id="1", name="Acme Inc.", domain="acme-one.invalid"),
        _candidate(id="c2", provider_id="p", external_id="2", name="Acme Corporation", domain="acme-two.invalid"),
    ]
    results = resolve_candidates(candidates, [])
    assert results[0][0].status == ResolutionStatus.NEW
    assert results[1][0].status == ResolutionStatus.NEW
    assert results[0][0].canonical_company_id != results[1][0].canonical_company_id


# --- insufficient evidence -> UNRESOLVED -----------------------------------


def test_name_only_match_without_domain_is_unresolved_not_merged():
    company = _company(canonical_name="Example Test Co", canonical_domain=None)
    candidate = _candidate(name="Example Test Co", domain=None, provider_id="other-provider", external_id="x")
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == ResolutionReasonCode.NAME_ONLY_INSUFFICIENT
    assert result.canonical_company_id is None
    assert result.matched_company_id == company.id  # hint only, not a commitment


def test_name_matching_multiple_companies_is_unresolved():
    companies = [
        _company(id="c-1", canonical_name="Acme"),
        _company(id="c-2", canonical_name="Acme"),
    ]
    candidate = _candidate(name="Acme", domain=None, provider_id="other-provider", external_id="x")
    result = resolve_candidate(candidate, companies)

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == ResolutionReasonCode.AMBIGUOUS_NAME_MULTIPLE_MATCHES
    assert result.matched_company_id is None


# --- conflicting evidence -> UNRESOLVED ------------------------------------


def test_conflicting_provider_identity_and_matching_name_is_unresolved():
    company = _company(canonical_name="Example Test Co", provider_identities={"provider-a": "old-ext-id"})
    candidate = _candidate(name="Example Test Co", domain=None, provider_id="provider-a", external_id="new-ext-id")
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason_code == ResolutionReasonCode.PROVIDER_IDENTITY_NAME_CONFLICT
    assert "provider_external_id" in result.conflicting_signals


def test_never_forces_a_merge_on_uncertain_evidence():
    company = _company(canonical_name="Example Test Co")
    candidate = _candidate(name="Example Test Co", domain=None, provider_id="unrelated-provider", external_id="z")
    result = resolve_candidate(candidate, [company])
    assert result.status != ResolutionStatus.MATCH


# --- no identity signals at all -> NEW (default, not ambiguity) ----------


def test_no_domain_no_name_match_becomes_new():
    company = _company(canonical_name="Some Other Co")
    candidate = _candidate(name="Brand New Startup", domain=None)
    result = resolve_candidate(candidate, [company])

    assert result.status == ResolutionStatus.NEW
    assert result.reason_code == ResolutionReasonCode.NO_IDENTITY_SIGNALS


# --- batch behavior: within-batch matching, determinism -----------------


def test_two_candidates_for_a_brand_new_company_in_one_batch_match_each_other():
    candidates = [
        _candidate(id="c1", provider_id="provider-a", external_id="1", name="New Co", domain="new-co.invalid"),
        _candidate(id="c2", provider_id="provider-b", external_id="9", name="New Co", domain="new-co.invalid"),
    ]
    results = resolve_candidates(candidates, [])

    assert results[0][0].status == ResolutionStatus.NEW
    assert results[1][0].status == ResolutionStatus.MATCH
    assert results[1][0].canonical_company_id == results[0][0].canonical_company_id


def test_resolution_is_deterministic_for_identical_input():
    company = _company(canonical_domain="example-test.invalid")
    candidate = _candidate(domain="example-test.invalid")

    first = resolve_candidate(candidate, [company])
    second = resolve_candidate(candidate, [company])

    assert first.status == second.status
    assert first.canonical_company_id == second.canonical_company_id
    assert first.reason_code == second.reason_code
    assert first.matched_signals == second.matched_signals
