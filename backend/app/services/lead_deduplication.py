"""Phase 19 — Lead Deduplication.

Deterministic and DB-free, mirroring app/services/company_resolution.py and
app/services/person_resolution.py exactly: given a request and a snapshot
of existing leads, returns a decision with no side effects. The caller
(app/api/lead_deduplication.py) owns persistence.

This module never re-derives company or person identity — it only
consumes the canonical ids Phase 7/10 already produced. Concretely:

  * company_id is None or person_id is None because the underlying
    candidate's own identity resolution came back UNRESOLVED (Phase 7/10
    never issue a canonical id for an unresolved candidate) -> this module
    reports COMPANY_IDENTITY_UNRESOLVED / PERSON_IDENTITY_UNRESOLVED,
    never guesses a pairing to compensate.
  * (company_id, person_id) matches an existing lead exactly -> the same
    lead. This is the ONLY path to MATCHED_EXISTING_LEAD; there is no
    fuzzy/partial match here, by design (no new identity signal is ever
    invented).
  * A person_id is supplied whose current company association (Phase 10's
    own associated_company_ids/canonical_company_id) does not include this
    company_id -> the pairing itself is not evidenced yet, even though
    both ids independently exist; this is reported explicitly rather than
    silently treated like any other new pairing, so a caller can tell
    "genuinely new pairing" from "this pairing isn't supported by what
    Phase 10 currently knows about this person."
"""
from __future__ import annotations

from uuid import uuid4

from app.schemas.lead_deduplication import (
    ExistingLead,
    LeadDeduplicationDecision,
    LeadDeduplicationReasonCode,
    LeadDeduplicationResult,
    LeadSourceReference,
)


def deduplicate_lead(
    icp_id: str,
    icp_version: int,
    company_id: str | None,
    person_id: str | None,
    existing_leads: list[ExistingLead],
    person_associated_company_ids: tuple[str, ...] | None = None,
    source: LeadSourceReference | None = None,
) -> LeadDeduplicationResult:
    source = source or LeadSourceReference()

    if company_id is None:
        return LeadDeduplicationResult(
            icp_id=icp_id,
            icp_version=icp_version,
            company_id=company_id,
            person_id=person_id,
            decision=LeadDeduplicationDecision.UNRESOLVED,
            lead_id=None,
            confidence=None,
            matched_signals=(),
            reason_code=LeadDeduplicationReasonCode.COMPANY_IDENTITY_UNRESOLVED,
            explanation="No canonical company id is available yet; company identity resolution has not confirmed one.",
            source=source,
        )

    if person_id is None:
        for lead in existing_leads:
            if lead.company_id == company_id and lead.person_id is None:
                return LeadDeduplicationResult(
                    icp_id=icp_id,
                    icp_version=icp_version,
                    company_id=company_id,
                    person_id=person_id,
                    decision=LeadDeduplicationDecision.MATCHED_EXISTING_LEAD,
                    lead_id=lead.id,
                    confidence="HIGH",
                    matched_signals=("company_id",),
                    reason_code=LeadDeduplicationReasonCode.EXACT_COMPANY_ONLY_MATCH,
                    explanation=f"Company-only lead already exists for company {company_id}.",
                    source=source,
                )
        return LeadDeduplicationResult(
            icp_id=icp_id,
            icp_version=icp_version,
            company_id=company_id,
            person_id=person_id,
            decision=LeadDeduplicationDecision.NEW_LEAD,
            lead_id=str(uuid4()),
            confidence=None,
            matched_signals=(),
            reason_code=LeadDeduplicationReasonCode.NEW_COMPANY_ONLY,
            explanation=f"No existing company-only lead for company {company_id}; this is a new lead.",
            source=source,
        )

    # A person is involved: person identity itself must already be
    # resolved (a real canonical id), but that alone does not prove this
    # SPECIFIC company association is current — Phase 10 tracks that
    # explicitly via associated_company_ids, and this module reuses it
    # rather than re-deriving employment from scratch.
    if person_associated_company_ids is not None and company_id not in person_associated_company_ids:
        return LeadDeduplicationResult(
            icp_id=icp_id,
            icp_version=icp_version,
            company_id=company_id,
            person_id=person_id,
            decision=LeadDeduplicationDecision.UNRESOLVED,
            lead_id=None,
            confidence=None,
            matched_signals=(),
            reason_code=LeadDeduplicationReasonCode.PERSON_NOT_CURRENTLY_ASSOCIATED_WITH_COMPANY,
            explanation=(
                f"Person {person_id} has a resolved identity, but existing identity evidence does not "
                f"associate them with company {company_id}; the pairing itself is unresolved."
            ),
            source=source,
        )

    for lead in existing_leads:
        if lead.company_id == company_id and lead.person_id == person_id:
            return LeadDeduplicationResult(
                icp_id=icp_id,
                icp_version=icp_version,
                company_id=company_id,
                person_id=person_id,
                decision=LeadDeduplicationDecision.MATCHED_EXISTING_LEAD,
                lead_id=lead.id,
                confidence="HIGH",
                matched_signals=("company_id", "person_id"),
                reason_code=LeadDeduplicationReasonCode.EXACT_COMPANY_PERSON_MATCH,
                explanation=f"Lead already exists for company {company_id} and person {person_id}.",
                source=source,
            )

    return LeadDeduplicationResult(
        icp_id=icp_id,
        icp_version=icp_version,
        company_id=company_id,
        person_id=person_id,
        decision=LeadDeduplicationDecision.NEW_LEAD,
        lead_id=str(uuid4()),
        confidence=None,
        matched_signals=(),
        reason_code=LeadDeduplicationReasonCode.NEW_COMPANY_PERSON_PAIR,
        explanation=f"No existing lead for company {company_id} and person {person_id}; this is a new lead.",
        source=source,
    )
