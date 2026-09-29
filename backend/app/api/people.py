"""Phase 10 API — resolve people-discovery candidates into canonical people.

No verification, qualification, or scoring happens here. Resolution always
compares a candidate against *every* canonical person that exists —
regardless of which ICP or discovery run originally found them — so the
same real person is reused across ICPs instead of duplicated.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.evidence import _is_duplicate, _persist
from app.db.session import get_db
from app.models.people_discovery import PeopleDiscoveryCandidateModel, PeopleDiscoveryRunModel
from app.models.person import CanonicalPersonModel, PersonResolutionModel
from app.models.person_enrichment import PersonEnrichmentRunModel
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.models.company import CanonicalCompanyModel
from app.schemas.candidate_person import CandidatePerson
from app.schemas.company_resolution import ResolutionStatus
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceCreate, SourceType
from app.schemas.person_enrichment import PersonEnrichmentQuery, PersonEnrichmentRunRead, PersonEnrichmentRunStatus
from app.schemas.person_resolution import (
    CanonicalPersonRead,
    ExistingPersonIdentity,
    PersonResolutionRead,
    PersonResolveRequest,
)
from app.services.evidence_import import collect_person_evidence
from app.services.person_enrichment import is_entity_mock_sourced, run_person_enrichment
from app.services.person_identity import extract_linkedin_identifier
from app.services.person_resolution import resolve_candidate

router = APIRouter(prefix="/api/v1/people", tags=["people"])


def _to_identity(row: CanonicalPersonModel) -> ExistingPersonIdentity:
    return ExistingPersonIdentity(
        id=row.id,
        canonical_name=row.canonical_name,
        canonical_company_id=row.canonical_company_id,
        associated_company_ids=tuple(row.associated_company_ids),
        aliases=tuple(row.aliases),
        linkedin_id=row.linkedin_id,
        provider_identities=dict(row.provider_identities),
    )


def _candidate_row_to_schema(row: PeopleDiscoveryCandidateModel) -> CandidatePerson:
    return CandidatePerson(
        id=row.id,
        company_id=row.company_id,
        icp_id=row.icp_id,
        icp_version=row.icp_version,
        provider_id=row.provider_id,
        external_id=row.external_id,
        name=row.name,
        title=row.title,
        attributes=row.attributes,
        discovered_at=row.discovered_at,
    )


def _apply_match(person_row: CanonicalPersonModel, candidate: CandidatePerson, conflicting: list[str]) -> None:
    """Adds this candidate's identity info to an existing person — never
    overwriting a provider's already-recorded external id, and preserving
    (never overwriting) company associations. Conflicts are appended to
    `conflicting` (mutated in place) rather than silently dropped."""
    provider_identities = dict(person_row.provider_identities)
    existing_external_id = provider_identities.get(candidate.provider_id)
    if existing_external_id is None:
        provider_identities[candidate.provider_id] = candidate.external_id
        person_row.provider_identities = provider_identities
    elif existing_external_id != candidate.external_id and "provider_identity_conflict" not in conflicting:
        conflicting.append("provider_identity_conflict")

    if candidate.company_id and candidate.company_id not in person_row.associated_company_ids:
        person_row.associated_company_ids = [*person_row.associated_company_ids, candidate.company_id]
    if person_row.canonical_company_id is None:
        person_row.canonical_company_id = candidate.company_id

    linkedin_id = extract_linkedin_identifier(candidate.attributes)
    if person_row.linkedin_id is None and linkedin_id is not None:
        person_row.linkedin_id = linkedin_id
    elif (
        linkedin_id is not None
        and linkedin_id != person_row.linkedin_id
        and "linkedin_id_conflict" not in conflicting
    ):
        conflicting.append("linkedin_id_conflict")

    known_names = {a.lower() for a in person_row.aliases} | {person_row.canonical_name.lower()}
    if candidate.name.lower() not in known_names:
        person_row.aliases = [*person_row.aliases, candidate.name]


@router.post("/resolve", response_model=list[PersonResolutionRead])
def resolve_people_discovery_run(payload: PersonResolveRequest, db: Session = Depends(get_db)) -> list[PersonResolutionRead]:
    run_row = db.get(PeopleDiscoveryRunModel, payload.people_discovery_run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="People discovery run not found")

    candidate_rows = (
        db.query(PeopleDiscoveryCandidateModel)
        .filter(PeopleDiscoveryCandidateModel.run_id == payload.people_discovery_run_id)
        .order_by(PeopleDiscoveryCandidateModel.discovered_at)
        .all()
    )

    person_rows: dict[str, CanonicalPersonModel] = {row.id: row for row in db.query(CanonicalPersonModel).all()}
    identities = [_to_identity(row) for row in person_rows.values()]

    result_rows: list[PersonResolutionModel] = []

    for candidate_row in candidate_rows:
        existing_resolution = (
            db.query(PersonResolutionModel)
            .filter(PersonResolutionModel.candidate_id == candidate_row.id)
            .one_or_none()
        )
        if existing_resolution is not None:
            # Idempotent: a candidate is resolved at most once, ever.
            result_rows.append(existing_resolution)
            continue

        candidate = _candidate_row_to_schema(candidate_row)
        decision = resolve_candidate(candidate, identities)
        conflicting = list(decision.conflicting_signals)

        if decision.status == ResolutionStatus.NEW:
            new_row = CanonicalPersonModel(
                id=decision.canonical_person_id,
                canonical_name=candidate.name,
                canonical_company_id=candidate.company_id,
                associated_company_ids=[candidate.company_id] if candidate.company_id else [],
                aliases=[],
                linkedin_id=extract_linkedin_identifier(candidate.attributes),
                provider_identities={candidate.provider_id: candidate.external_id},
            )
            db.add(new_row)
            person_rows[new_row.id] = new_row
            identities.append(_to_identity(new_row))
        elif decision.status == ResolutionStatus.MATCH:
            person_row = person_rows[decision.canonical_person_id]
            _apply_match(person_row, candidate, conflicting)
            for i, identity in enumerate(identities):
                if identity.id == person_row.id:
                    identities[i] = _to_identity(person_row)
                    break

        resolution_row = PersonResolutionModel(
            id=str(uuid4()),
            candidate_id=candidate.id,
            people_discovery_run_id=payload.people_discovery_run_id,
            status=decision.status.value,
            canonical_person_id=decision.canonical_person_id,
            matched_person_id=decision.matched_person_id,
            confidence=decision.confidence,
            matched_signals=list(decision.matched_signals),
            conflicting_signals=conflicting,
            reason_code=decision.reason_code.value,
            explanation=decision.explanation,
        )
        db.add(resolution_row)
        result_rows.append(resolution_row)

    db.commit()
    for row in result_rows:
        db.refresh(row)

    return [PersonResolutionRead.model_validate(row) for row in result_rows]


@router.get("/resolutions", response_model=list[PersonResolutionRead])
def list_person_resolutions(
    people_discovery_run_id: str = Query(...), db: Session = Depends(get_db)
) -> list[PersonResolutionRead]:
    rows = (
        db.query(PersonResolutionModel)
        .filter(PersonResolutionModel.people_discovery_run_id == people_discovery_run_id)
        .order_by(PersonResolutionModel.resolved_at)
        .all()
    )
    return [PersonResolutionRead.model_validate(row) for row in rows]


@router.get("", response_model=list[CanonicalPersonRead])
def list_people(db: Session = Depends(get_db)) -> list[CanonicalPersonModel]:
    return db.query(CanonicalPersonModel).order_by(CanonicalPersonModel.created_at).all()


@router.get("/{person_id}", response_model=CanonicalPersonRead)
def get_person(person_id: str, db: Session = Depends(get_db)) -> CanonicalPersonModel:
    row = db.get(CanonicalPersonModel, person_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return row


def _import_person_evidence_if_missing(db: Session, person_id: str) -> None:
    """Ensures the person's existing Phase 9 discovery facts (title, company
    association, linkedin_id) are already in the evidence table before
    enrichment runs — Apollo's match quality depends on having a name/company
    to search with, and this reuses app/api/evidence.py's own idempotent
    persist/duplicate logic rather than a second import path."""
    for candidate in collect_person_evidence(db, person_id):
        if not _is_duplicate(db, candidate):
            _persist(db, candidate)
    db.commit()


def _latest_evidence_value(db: Session, person_id: str, field: str) -> str | None:
    from app.models.evidence import EvidenceModel

    row = (
        db.query(EvidenceModel)
        .filter(
            EvidenceModel.entity_type == EntityType.PERSON.value,
            EvidenceModel.entity_id == person_id,
            EvidenceModel.field == field,
        )
        .order_by(EvidenceModel.retrieved_at.desc())
        .first()
    )
    if row is None or not isinstance(row.value, str):
        return None
    return row.value


def _confidence_from_apollo_email_status(email_status: object) -> ConfidenceLevel:
    """Maps Apollo's own email_status field to the structured confidence
    scale — never invented beyond what Apollo literally reported. Any
    email_status value this adapter doesn't recognize is UNKNOWN, not
    guessed toward HIGH or LOW."""
    if email_status == "verified":
        return ConfidenceLevel.HIGH
    if email_status == "guessed":
        return ConfidenceLevel.LOW
    return ConfidenceLevel.UNKNOWN


@router.post("/{person_id}/enrich", response_model=PersonEnrichmentRunRead, status_code=201)
def enrich_person(
    person_id: str,
    registry: ProviderRegistry = Depends(get_provider_registry),
    db: Session = Depends(get_db),
) -> PersonEnrichmentRunModel:
    """Runs PERSON_ENRICHMENT (Apollo, when configured) for one already-
    resolved canonical person. Never fails the whole request because of a
    provider error — a failed or unavailable run is still a 201 with that
    status recorded, matching the same contract as
    POST /companies/{id}/enrich (app/api/companies.py)."""
    person_row = db.get(CanonicalPersonModel, person_id)
    if person_row is None:
        raise HTTPException(status_code=404, detail="Person not found")

    _import_person_evidence_if_missing(db, person_id)

    # "company_association" evidence holds a company_id (see
    # collect_person_evidence in app/services/evidence_import.py), not a
    # name — so the only honest source for a human-readable company
    # name/domain is the canonical company row itself, via
    # canonical_company_id (which _import_person_evidence_if_missing above
    # may have just populated for the first time via a NEW resolution).
    company_domain: str | None = None
    company_name: str | None = None
    company_id = person_row.canonical_company_id or _latest_evidence_value(
        db, person_id, "company_association"
    )
    if company_id:
        company_row = db.get(CanonicalCompanyModel, company_id)
        if company_row is not None:
            company_domain = company_row.canonical_domain
            company_name = company_row.canonical_name

    query = PersonEnrichmentQuery(
        full_name=person_row.canonical_name,
        linkedin_id=person_row.linkedin_id,
        company_domain=company_domain,
        company_name=company_name,
    )

    # Phase 4 — SAFE SPEND/CALL GUARD: never spend a real, paid
    # PERSON_ENRICHMENT call (Apollo) on a person whose entire evidence
    # trail is mock-sourced (see is_entity_mock_sourced's own docstring —
    # this is the concrete, previously-unguarded gap where a real Apollo
    # key configured while people-discovery still runs on
    # MockPeopleDataProvider, e.g. Unipile not fully configured, would let
    # a real API call run against a fabricated name/company with zero
    # chance of a meaningful result). Checked here, not inside
    # run_person_enrichment, because only the caller has already loaded
    # this person's evidence rows for _latest_evidence_value above.
    from app.models.evidence import EvidenceModel

    evidence_source_provider_ids = [
        row.source_provider_id
        for row in db.query(EvidenceModel.source_provider_id)
        .filter(EvidenceModel.entity_type == EntityType.PERSON.value, EvidenceModel.entity_id == person_id)
        .all()
    ]
    if is_entity_mock_sourced(evidence_source_provider_ids):
        run_row = PersonEnrichmentRunModel(
            id=str(uuid4()),
            person_id=person_id,
            status=PersonEnrichmentRunStatus.MOCK_SOURCED_SKIPPED.value,
            provider_id=None,
            error_code=None,
            error_message=(
                "Every evidence record for this person traces back to a mock discovery provider — "
                "skipped to avoid spending a real enrichment call on fabricated input."
            ),
        )
        db.add(run_row)
        db.commit()
        db.refresh(run_row)
        return run_row

    outcome, facts = run_person_enrichment(person_id, query, registry)

    run_row = PersonEnrichmentRunModel(
        id=str(uuid4()),
        person_id=person_id,
        status=outcome.status.value,
        provider_id=outcome.provider_id,
        error_code=outcome.error.code if outcome.error else None,
        error_message=outcome.error.message if outcome.error else None,
    )
    db.add(run_row)

    # email_status is its own fact (not persisted as a separate evidence
    # field the UI needs) — used only to derive the email fact's confidence,
    # never invented beyond what Apollo literally returned.
    email_status_by_provider = {
        fact.provider_id: fact.value for fact in facts if fact.field == "email_status"
    }

    for fact in facts:
        if fact.field == "email_status":
            continue
        confidence = ConfidenceLevel.UNKNOWN
        if fact.field == "email":
            confidence = _confidence_from_apollo_email_status(email_status_by_provider.get(fact.provider_id))

        candidate = EvidenceCreate(
            entity_type=EntityType.PERSON,
            entity_id=person_id,
            field=fact.field,
            value=fact.value,
            source_provider_id=fact.provider_id,
            source_type=SourceType.PROVIDER,
            external_id=fact.external_id,
            retrieved_at=fact.retrieved_at,
            confidence=confidence,
        )
        if not _is_duplicate(db, candidate):
            _persist(db, candidate)

    db.commit()
    db.refresh(run_row)
    return run_row
