"""Phase 7 — Company Entity Resolution.

Decides whether a Phase 6 discovery candidate refers to an already-known
canonical company (MATCH), is genuinely new (NEW), or cannot be safely
decided either way yet (UNRESOLVED). This module answers exactly one
question — "are these records the same real-world company?" — and nothing
else: it never touches lead deduplication, hard-ICP fitness, business
model, or scoring.

Every rule here is deterministic and conservative. A false merge (treating
two different real companies as one) is strictly worse than a temporary
UNRESOLVED record, so nothing here ever guesses from a weak signal alone:

  - The strongest signal is a provider's own external id already recorded
    against a company — the provider itself already told us this is the
    same record.
  - The next strongest is an exact normalized domain match — domains are
    close to a real-world unique identifier.
  - A normalized company name matching by itself is NEVER sufficient for a
    MATCH (two different real companies can trivially share a short name)
    — it only ever produces UNRESOLVED, as a hint for a future
    human/evidence process, never an auto-merge.
  - Conflicting signals (e.g. the same provider reporting a different
    external id for what otherwise looks like the same company) also
    resolve to UNRESOLVED rather than picking a side.

resolve_candidate() is pure: given a candidate and a snapshot of existing
company identities, it returns a decision with no side effects and no
database access — app/api/companies.py owns persistence, including the
rule that one provider's identity must never overwrite another's.
"""
from __future__ import annotations

from uuid import uuid4

from app.schemas.candidate_company import CandidateCompany
from app.schemas.company_resolution import (
    ExistingCompanyIdentity,
    ResolutionReasonCode,
    ResolutionResult,
    ResolutionStatus,
)
from app.services.company_identity import normalize_company_name, normalize_domain


def _company_knows_name(company: ExistingCompanyIdentity, normalized_name: str | None) -> bool:
    if normalized_name is None:
        return False
    if normalize_company_name(company.canonical_name) == normalized_name:
        return True
    return any(normalize_company_name(alias) == normalized_name for alias in company.aliases)


def resolve_candidate(
    candidate: CandidateCompany,
    existing_companies: list[ExistingCompanyIdentity],
) -> ResolutionResult:
    normalized_domain = normalize_domain(candidate.domain)
    normalized_name = normalize_company_name(candidate.name)

    # 1. Trusted provider identity — the provider itself already told us
    #    this exact external id belongs to this company.
    for company in existing_companies:
        recorded_external_id = company.provider_identities.get(candidate.provider_id)
        if recorded_external_id is not None and recorded_external_id == candidate.external_id:
            conflicting = []
            if normalized_domain and company.canonical_domain and normalized_domain != company.canonical_domain:
                conflicting.append("domain")
            return ResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.MATCH,
                canonical_company_id=company.id,
                matched_company_id=company.id,
                confidence="HIGH",
                matched_signals=("provider_identity",),
                conflicting_signals=tuple(conflicting),
                reason_code=ResolutionReasonCode.TRUSTED_PROVIDER_IDENTITY,
                explanation=(
                    f"Provider '{candidate.provider_id}' external id '{candidate.external_id}' "
                    f"is already recorded against company {company.id}."
                ),
            )

    # 2. Domain match — close to a real-world unique identifier.
    if normalized_domain:
        domain_matches = [c for c in existing_companies if c.canonical_domain == normalized_domain]
        if domain_matches:
            company = domain_matches[0]
            conflicting = []
            if normalized_name and not _company_knows_name(company, normalized_name):
                conflicting.append("name")
            return ResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.MATCH,
                canonical_company_id=company.id,
                matched_company_id=company.id,
                confidence="HIGH",
                matched_signals=("domain",),
                conflicting_signals=tuple(conflicting),
                reason_code=ResolutionReasonCode.DOMAIN_MATCH,
                explanation=f"Normalized domain '{normalized_domain}' matches company {company.id}.",
            )

        # A well-formed domain that matches nothing existing is itself
        # strong, positive evidence of a distinct company — not ambiguity.
        return ResolutionResult(
            candidate_id=candidate.id,
            status=ResolutionStatus.NEW,
            canonical_company_id=str(uuid4()),
            matched_company_id=None,
            confidence=None,
            matched_signals=(),
            conflicting_signals=(),
            reason_code=ResolutionReasonCode.NEW_UNIQUE_DOMAIN,
            explanation=f"Normalized domain '{normalized_domain}' does not match any known company.",
        )

    # 3. No domain on this candidate — check for a genuine conflict first:
    #    the same provider already reports a *different* external id for a
    #    company with this exact name. That's real, mixed evidence, not
    #    something to resolve automatically either way.
    for company in existing_companies:
        recorded_external_id = company.provider_identities.get(candidate.provider_id)
        if recorded_external_id is not None and recorded_external_id != candidate.external_id:
            if _company_knows_name(company, normalized_name):
                return ResolutionResult(
                    candidate_id=candidate.id,
                    status=ResolutionStatus.UNRESOLVED,
                    canonical_company_id=None,
                    matched_company_id=company.id,
                    confidence=None,
                    matched_signals=("name",),
                    conflicting_signals=("provider_external_id",),
                    reason_code=ResolutionReasonCode.PROVIDER_IDENTITY_NAME_CONFLICT,
                    explanation=(
                        f"Name matches company {company.id}, but provider '{candidate.provider_id}' "
                        f"already reports a different external id for it."
                    ),
                )

    # 4. No domain, no conflict — name alone is never sufficient to merge.
    if normalized_name:
        name_matches = [c for c in existing_companies if _company_knows_name(c, normalized_name)]
        if len(name_matches) == 1:
            return ResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.UNRESOLVED,
                canonical_company_id=None,
                matched_company_id=name_matches[0].id,
                confidence=None,
                matched_signals=("name",),
                conflicting_signals=(),
                reason_code=ResolutionReasonCode.NAME_ONLY_INSUFFICIENT,
                explanation=(
                    f"Name matches company {name_matches[0].id}, but without a domain or trusted "
                    f"provider identity a name alone is not enough to merge."
                ),
            )
        if len(name_matches) > 1:
            return ResolutionResult(
                candidate_id=candidate.id,
                status=ResolutionStatus.UNRESOLVED,
                canonical_company_id=None,
                matched_company_id=None,
                confidence=None,
                matched_signals=("name",),
                conflicting_signals=("multiple_name_matches",),
                reason_code=ResolutionReasonCode.AMBIGUOUS_NAME_MULTIPLE_MATCHES,
                explanation=f"Name matches {len(name_matches)} existing companies; cannot pick one.",
            )

    # 5. Nothing to compare against at all — a new entity by elimination,
    #    not a special high-confidence case, just the default when no
    #    evidence suggests this collides with anything already known.
    return ResolutionResult(
        candidate_id=candidate.id,
        status=ResolutionStatus.NEW,
        canonical_company_id=str(uuid4()),
        matched_company_id=None,
        confidence=None,
        matched_signals=(),
        conflicting_signals=(),
        reason_code=ResolutionReasonCode.NO_IDENTITY_SIGNALS,
        explanation="No domain, provider identity, or name match against any known company.",
    )


def resolve_candidates(
    candidates: list[CandidateCompany],
    existing_companies: list[ExistingCompanyIdentity],
) -> list[tuple[ResolutionResult, ExistingCompanyIdentity | None]]:
    """Resolves a batch of candidates in order, feeding each newly-minted
    company back into the comparison pool immediately — so two candidates
    for the same new company within one batch correctly MATCH each other
    instead of both becoming NEW.

    Returns one (decision, new_identity_or_None) pair per candidate; the
    second element is populated only for a NEW decision, giving the caller
    exactly what it needs to persist without re-deriving it.
    """
    pool = list(existing_companies)
    results: list[tuple[ResolutionResult, ExistingCompanyIdentity | None]] = []

    for candidate in candidates:
        decision = resolve_candidate(candidate, pool)
        new_identity = None
        if decision.status == ResolutionStatus.NEW:
            new_identity = ExistingCompanyIdentity(
                id=decision.canonical_company_id,
                canonical_name=candidate.name,
                canonical_domain=normalize_domain(candidate.domain),
                aliases=(),
                provider_identities={candidate.provider_id: candidate.external_id},
            )
            pool.append(new_identity)
        results.append((decision, new_identity))

    return results
