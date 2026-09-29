"""Phase 10 — Person Identity Resolution.

Decides whether a Phase 9 people-discovery candidate refers to an
already-known canonical person (MATCH), is genuinely new (NEW), or cannot
be safely decided either way yet (UNRESOLVED). This module answers exactly
one question — "is this the same real person?" — and nothing else: it
never verifies a title, confirms a LinkedIn profile, checks an email, or
qualifies anyone against an ICP.

Every rule here is deterministic and conservative, mirroring
app/services/company_resolution.py's structure exactly:

  - The strongest signal is a provider's own external id already recorded
    against a person — the provider itself already told us this is the
    same record.
  - The next strongest is a LinkedIn/person identifier the provider
    actually supplied — never invented — matching an existing one exactly.
  - A normalized person name matching by itself is NEVER sufficient for a
    MATCH ("John Smith" is not a unique identity) — at most it produces
    UNRESOLVED, as a hint for a future human/evidence process.
  - The same name at a *different* company is treated as evidence of a
    different real person (a new canonical person), not ambiguity — a
    person cannot simultaneously be two different companies' decision-maker
    with no other evidence connecting the two sightings.
  - Any conflicting signal on top of an otherwise-strong match — a company
    association, a LinkedIn identifier, or a name that doesn't match what's
    on record — resolves to UNRESOLVED rather than a flagged MATCH.
    Misidentifying a real person is higher-stakes than misidentifying a
    company (Phase 7 tolerates a domain-match-with-name-conflict as a
    flagged MATCH), so this module holds a stricter line: reconciling a
    "current vs. former employer" conflict is a later phase's job.

resolve_candidate() is pure: given a candidate and a snapshot of existing
person identities, it returns a decision with no side effects and no
database access — app/api/people.py owns persistence, including the rule
that one provider's identity (or company association) must never overwrite
another's.
"""
from __future__ import annotations

from uuid import uuid4

from app.schemas.candidate_person import CandidatePerson
from app.schemas.company_resolution import ResolutionStatus
from app.schemas.person_resolution import (
    ExistingPersonIdentity,
    PersonResolutionReasonCode,
    PersonResolutionResult,
)
from app.services.person_identity import extract_linkedin_identifier, normalize_person_name


def _person_knows_name(person: ExistingPersonIdentity, normalized_name: str | None) -> bool:
    if normalized_name is None:
        return False
    if normalize_person_name(person.canonical_name) == normalized_name:
        return True
    return any(normalize_person_name(alias) == normalized_name for alias in person.aliases)


def resolve_candidate(
    candidate: CandidatePerson,
    existing_people: list[ExistingPersonIdentity],
) -> PersonResolutionResult:
    normalized_name = normalize_person_name(candidate.name)
    linkedin_id = extract_linkedin_identifier(candidate.attributes)

    # 1. Trusted provider identity — the provider itself already told us
    #    this exact external id belongs to this person. Unlike company
    #    resolution (Phase 7), a conflicting company association here is
    #    NOT tolerated as a mere flag: people are higher-stakes than
    #    companies to merge incorrectly, so any conflict on top of the
    #    provider match still resolves to UNRESOLVED rather than a
    #    flagged MATCH. Reconciling "current vs. former employer" is a
    #    later phase's job, not this one's.
    for person in existing_people:
        recorded_external_id = person.provider_identities.get(candidate.provider_id)
        if recorded_external_id is not None and recorded_external_id == candidate.external_id:
            conflicting = []
            if (
                candidate.company_id
                and person.canonical_company_id is not None
                and candidate.company_id not in person.associated_company_ids
            ):
                conflicting.append("company")
            if linkedin_id and person.linkedin_id and linkedin_id != person.linkedin_id:
                conflicting.append("linkedin_id")
            if conflicting:
                return PersonResolutionResult(
                    candidate_id=candidate.id,
                    status=ResolutionStatus.UNRESOLVED,
                    canonical_person_id=None,
                    matched_person_id=person.id,
                    confidence=None,
                    matched_signals=("provider_identity",),
                    conflicting_signals=tuple(conflicting),
                    reason_code=PersonResolutionReasonCode.IDENTITY_SIGNAL_CONFLICT,
                    explanation=(
                        f"Provider '{candidate.provider_id}' external id '{candidate.external_id}' matches "
                        f"person {person.id}, but {', '.join(conflicting)} conflicts with what is on record."
                    ),
                )
            return PersonResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.MATCH,
                canonical_person_id=person.id,
                matched_person_id=person.id,
                confidence="HIGH",
                matched_signals=("provider_identity",),
                conflicting_signals=(),
                reason_code=PersonResolutionReasonCode.TRUSTED_PROVIDER_IDENTITY,
                explanation=(
                    f"Provider '{candidate.provider_id}' external id '{candidate.external_id}' "
                    f"is already recorded against person {person.id}."
                ),
            )

    # 2. LinkedIn identifier match — a provider-supplied, close to unique
    #    real-world identifier. Same zero-tolerance-for-conflict rule.
    if linkedin_id:
        matches = [p for p in existing_people if p.linkedin_id == linkedin_id]
        if matches:
            person = matches[0]
            conflicting = []
            if (
                candidate.company_id
                and person.canonical_company_id is not None
                and candidate.company_id not in person.associated_company_ids
            ):
                conflicting.append("company")
            if normalized_name and not _person_knows_name(person, normalized_name):
                conflicting.append("name")
            if conflicting:
                return PersonResolutionResult(
                    candidate_id=candidate.id,
                    status=ResolutionStatus.UNRESOLVED,
                    canonical_person_id=None,
                    matched_person_id=person.id,
                    confidence=None,
                    matched_signals=("linkedin_id",),
                    conflicting_signals=tuple(conflicting),
                    reason_code=PersonResolutionReasonCode.IDENTITY_SIGNAL_CONFLICT,
                    explanation=(
                        f"LinkedIn identifier '{linkedin_id}' matches person {person.id}, but "
                        f"{', '.join(conflicting)} conflicts with what is on record."
                    ),
                )
            return PersonResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.MATCH,
                canonical_person_id=person.id,
                matched_person_id=person.id,
                confidence="HIGH",
                matched_signals=("linkedin_id",),
                conflicting_signals=(),
                reason_code=PersonResolutionReasonCode.LINKEDIN_ID_MATCH,
                explanation=f"LinkedIn identifier '{linkedin_id}' matches person {person.id}.",
            )

        # A LinkedIn identifier that matches nothing existing is itself
        # strong, positive evidence of a distinct person — not ambiguity.
        return PersonResolutionResult(
            candidate_id=candidate.id,
            status=ResolutionStatus.NEW,
            canonical_person_id=str(uuid4()),
            matched_person_id=None,
            confidence=None,
            matched_signals=(),
            conflicting_signals=(),
            reason_code=PersonResolutionReasonCode.NEW_UNIQUE_LINKEDIN_ID,
            explanation=f"LinkedIn identifier '{linkedin_id}' does not match any known person.",
        )

    # 3. No LinkedIn identifier on this candidate — check for a genuine
    #    conflict first: the same provider already reports a *different*
    #    external id for a person with this exact name *at the same
    #    company context*. That combination is real, mixed evidence, not
    #    something to resolve automatically either way. If the company
    #    also differs, the provider disagreement and the company
    #    difference point the same direction (a different person), so this
    #    falls through to step 4's "different company -> NEW" handling
    #    instead of being treated as ambiguous.
    for person in existing_people:
        recorded_external_id = person.provider_identities.get(candidate.provider_id)
        if recorded_external_id is not None and recorded_external_id != candidate.external_id:
            same_company_context = not candidate.company_id or candidate.company_id in person.associated_company_ids
            if _person_knows_name(person, normalized_name) and same_company_context:
                return PersonResolutionResult(
                    candidate_id=candidate.id,
                    status=ResolutionStatus.UNRESOLVED,
                    canonical_person_id=None,
                    matched_person_id=person.id,
                    confidence=None,
                    matched_signals=("name", "company"),
                    conflicting_signals=("provider_external_id",),
                    reason_code=PersonResolutionReasonCode.PROVIDER_IDENTITY_CONFLICT,
                    explanation=(
                        f"Name and company match person {person.id}, but provider '{candidate.provider_id}' "
                        f"already reports a different external id for them."
                    ),
                )

    # 4. Name-only signal — never sufficient alone. The same name at the
    #    *same* company is genuinely ambiguous (UNRESOLVED); the same name
    #    at a *different* company is treated as evidence of a different
    #    person (falls through to NEW below), matching the task's explicit
    #    "same name but different company -> not automatically merged."
    if normalized_name:
        name_matches = [p for p in existing_people if _person_knows_name(p, normalized_name)]
        if name_matches:
            same_company_matches = [
                p
                for p in name_matches
                if candidate.company_id is not None and candidate.company_id in p.associated_company_ids
            ]
            if len(same_company_matches) == 1:
                return PersonResolutionResult(
                    candidate_id=candidate.id,
                    status=ResolutionStatus.UNRESOLVED,
                    canonical_person_id=None,
                    matched_person_id=same_company_matches[0].id,
                    confidence=None,
                    matched_signals=("name", "company"),
                    conflicting_signals=(),
                    reason_code=PersonResolutionReasonCode.NAME_AND_COMPANY_INSUFFICIENT,
                    explanation=(
                        f"Name and company match person {same_company_matches[0].id}, but without a "
                        f"trusted provider identity or LinkedIn identifier that is not enough to merge."
                    ),
                )
            if len(same_company_matches) > 1:
                return PersonResolutionResult(
                    candidate_id=candidate.id,
                    status=ResolutionStatus.UNRESOLVED,
                    canonical_person_id=None,
                    matched_person_id=None,
                    confidence=None,
                    matched_signals=("name", "company"),
                    conflicting_signals=("multiple_matches",),
                    reason_code=PersonResolutionReasonCode.AMBIGUOUS_NAME_MULTIPLE_MATCHES,
                    explanation=f"Name and company match {len(same_company_matches)} existing people; cannot pick one.",
                )

            # Name matched, but only at a different company (or no company
            # is on record for the candidate) — treated as a distinct
            # person, not ambiguity.
            return PersonResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.NEW,
                canonical_person_id=str(uuid4()),
                matched_person_id=None,
                confidence=None,
                matched_signals=(),
                conflicting_signals=(),
                reason_code=PersonResolutionReasonCode.NEW_DIFFERENT_COMPANY_CONTEXT,
                explanation=(
                    f"Name matches an existing person, but not at this candidate's company — "
                    f"treated as a different person."
                ),
            )

    # 5. Nothing to compare against at all — a new entity by elimination,
    #    not a special high-confidence case, just the default when no
    #    evidence suggests this collides with anything already known.
    return PersonResolutionResult(
        candidate_id=candidate.id,
        status=ResolutionStatus.NEW,
        canonical_person_id=str(uuid4()),
        matched_person_id=None,
        confidence=None,
        matched_signals=(),
        conflicting_signals=(),
        reason_code=PersonResolutionReasonCode.NO_IDENTITY_SIGNALS,
        explanation="No LinkedIn identifier, provider identity, or name match against any known person.",
    )


def resolve_candidates(
    candidates: list[CandidatePerson],
    existing_people: list[ExistingPersonIdentity],
) -> list[tuple[PersonResolutionResult, ExistingPersonIdentity | None]]:
    """Resolves a batch of candidates in order, feeding each newly-minted
    person back into the comparison pool immediately — so two candidates
    for the same new person within one batch correctly MATCH each other
    instead of both becoming NEW.
    """
    pool = list(existing_people)
    results: list[tuple[PersonResolutionResult, ExistingPersonIdentity | None]] = []

    for candidate in candidates:
        decision = resolve_candidate(candidate, pool)
        new_identity = None
        if decision.status == ResolutionStatus.NEW:
            new_identity = ExistingPersonIdentity(
                id=decision.canonical_person_id,
                canonical_name=candidate.name,
                canonical_company_id=candidate.company_id,
                associated_company_ids=(candidate.company_id,) if candidate.company_id else (),
                aliases=(),
                linkedin_id=extract_linkedin_identifier(candidate.attributes),
                provider_identities={candidate.provider_id: candidate.external_id},
            )
            pool.append(new_identity)
        results.append((decision, new_identity))

    return results
