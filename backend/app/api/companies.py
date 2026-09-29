"""Phase 7/8 API — resolve discovery candidates into canonical companies,
and enrich a canonical company with structured facts.

Resolution (Phase 7) always compares a candidate against *every* canonical
company that exists — regardless of which ICP or discovery run originally
found it — so the same real company is reused across ICPs instead of
duplicated. Enrichment (Phase 8) operates purely on an already-resolved
company id; it never creates a company and never re-runs identity
resolution. Neither performs qualification, scoring, or business-model
classification — those belong to later phases.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.company import CanonicalCompanyModel, CompanyResolutionModel
from app.models.discovery import DiscoveryCandidateModel, DiscoveryRunModel
from app.models.enrichment import EnrichmentFactModel, EnrichmentRunModel
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.candidate_company import CandidateCompany
from app.schemas.company_enrichment import (
    CompanyFactsRead,
    EnrichmentFact,
    EnrichmentRunRead,
    ProviderEnrichmentOutcomeRead,
)
from app.schemas.company_resolution import (
    CanonicalCompanyRead,
    CompanyResolveRequest,
    ExistingCompanyIdentity,
    ResolutionRead,
    ResolutionStatus,
)
from app.services.company_enrichment import group_facts_by_field, run_company_enrichment
from app.services.company_identity import normalize_domain
from app.services.company_resolution import resolve_candidate

router = APIRouter(prefix="/api/v1/companies", tags=["companies"])


def _to_identity(row: CanonicalCompanyModel) -> ExistingCompanyIdentity:
    return ExistingCompanyIdentity(
        id=row.id,
        canonical_name=row.canonical_name,
        canonical_domain=row.canonical_domain,
        aliases=tuple(row.aliases),
        provider_identities=dict(row.provider_identities),
    )


def _candidate_row_to_schema(row: DiscoveryCandidateModel) -> CandidateCompany:
    return CandidateCompany(
        id=row.id,
        icp_id=row.icp_id,
        icp_version=row.icp_version,
        provider_id=row.provider_id,
        external_id=row.external_id,
        name=row.name,
        domain=row.domain,
        attributes=row.attributes,
        discovered_at=row.discovered_at,
    )


def _apply_match(company_row: CanonicalCompanyModel, candidate: CandidateCompany, conflicting: list[str]) -> None:
    """Adds this candidate's identity info to an existing company —
    never overwriting a provider's already-recorded external id, and never
    overwriting an already-set canonical domain. Conflicts are appended to
    `conflicting` (mutated in place) rather than silently dropped.
    """
    provider_identities = dict(company_row.provider_identities)
    existing_external_id = provider_identities.get(candidate.provider_id)
    if existing_external_id is None:
        provider_identities[candidate.provider_id] = candidate.external_id
        company_row.provider_identities = provider_identities
    elif existing_external_id != candidate.external_id and "provider_identity_conflict" not in conflicting:
        conflicting.append("provider_identity_conflict")

    normalized_domain = normalize_domain(candidate.domain)
    if company_row.canonical_domain is None and normalized_domain is not None:
        company_row.canonical_domain = normalized_domain
    elif (
        normalized_domain is not None
        and normalized_domain != company_row.canonical_domain
        and "domain_conflict" not in conflicting
    ):
        conflicting.append("domain_conflict")

    known_names = {a.lower() for a in company_row.aliases} | {company_row.canonical_name.lower()}
    if candidate.name.lower() not in known_names:
        company_row.aliases = [*company_row.aliases, candidate.name]


@router.post("/resolve", response_model=list[ResolutionRead])
def resolve_discovery_run(payload: CompanyResolveRequest, db: Session = Depends(get_db)) -> list[ResolutionRead]:
    run_row = db.get(DiscoveryRunModel, payload.discovery_run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="Discovery run not found")

    candidate_rows = (
        db.query(DiscoveryCandidateModel)
        .filter(DiscoveryCandidateModel.run_id == payload.discovery_run_id)
        .order_by(DiscoveryCandidateModel.discovered_at)
        .all()
    )

    company_rows: dict[str, CanonicalCompanyModel] = {row.id: row for row in db.query(CanonicalCompanyModel).all()}
    identities = [_to_identity(row) for row in company_rows.values()]

    result_rows: list[CompanyResolutionModel] = []

    for candidate_row in candidate_rows:
        existing_resolution = (
            db.query(CompanyResolutionModel)
            .filter(CompanyResolutionModel.candidate_id == candidate_row.id)
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
            new_row = CanonicalCompanyModel(
                id=decision.canonical_company_id,
                canonical_name=candidate.name,
                canonical_domain=normalize_domain(candidate.domain),
                aliases=[],
                provider_identities={candidate.provider_id: candidate.external_id},
            )
            db.add(new_row)
            company_rows[new_row.id] = new_row
            identities.append(_to_identity(new_row))
        elif decision.status == ResolutionStatus.MATCH:
            company_row = company_rows[decision.canonical_company_id]
            _apply_match(company_row, candidate, conflicting)
            for i, identity in enumerate(identities):
                if identity.id == company_row.id:
                    identities[i] = _to_identity(company_row)
                    break

        resolution_row = CompanyResolutionModel(
            id=str(uuid4()),
            candidate_id=candidate.id,
            discovery_run_id=payload.discovery_run_id,
            status=decision.status.value,
            canonical_company_id=decision.canonical_company_id,
            matched_company_id=decision.matched_company_id,
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

    return [ResolutionRead.model_validate(row) for row in result_rows]


@router.get("/resolutions", response_model=list[ResolutionRead])
def list_resolutions(
    discovery_run_id: str = Query(...), db: Session = Depends(get_db)
) -> list[ResolutionRead]:
    rows = (
        db.query(CompanyResolutionModel)
        .filter(CompanyResolutionModel.discovery_run_id == discovery_run_id)
        .order_by(CompanyResolutionModel.resolved_at)
        .all()
    )
    return [ResolutionRead.model_validate(row) for row in rows]


@router.get("", response_model=list[CanonicalCompanyRead])
def list_companies(db: Session = Depends(get_db)) -> list[CanonicalCompanyModel]:
    return db.query(CanonicalCompanyModel).order_by(CanonicalCompanyModel.created_at).all()


@router.get("/{company_id}", response_model=CanonicalCompanyRead)
def get_company(company_id: str, db: Session = Depends(get_db)) -> CanonicalCompanyModel:
    row = db.get(CanonicalCompanyModel, company_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return row


@router.post("/{company_id}/enrich", response_model=EnrichmentRunRead, status_code=201)
def enrich_company(
    company_id: str,
    registry: ProviderRegistry = Depends(get_provider_registry),
    db: Session = Depends(get_db),
) -> EnrichmentRunRead:
    """Runs one enrichment pass for an already-resolved canonical company.

    Never creates a company and never re-runs entity resolution — a 404
    here means "resolve this candidate into a company first" (Phase 7),
    not "create one."
    """
    company_row = db.get(CanonicalCompanyModel, company_id)
    if company_row is None:
        raise HTTPException(status_code=404, detail="Company not found")

    identity = ExistingCompanyIdentity(
        id=company_row.id,
        canonical_name=company_row.canonical_name,
        canonical_domain=company_row.canonical_domain,
        aliases=tuple(company_row.aliases),
        provider_identities=dict(company_row.provider_identities),
    )
    result = run_company_enrichment(identity, registry)

    run_row = EnrichmentRunModel(
        id=result.run_id,
        company_id=result.company_id,
        status=result.status.value,
        provider_outcomes=[
            {
                "provider_id": outcome.provider_id,
                "success": outcome.success,
                "fields_returned": outcome.fields_returned,
                "latency_ms": outcome.latency_ms,
                "error_code": outcome.error.code if outcome.error else None,
                "error_message": outcome.error.message if outcome.error else None,
            }
            for outcome in result.provider_outcomes
        ],
    )
    db.add(run_row)

    for field in result.fields:
        for fact in field.facts:
            db.add(
                EnrichmentFactModel(
                    id=str(uuid4()),
                    run_id=result.run_id,
                    company_id=result.company_id,
                    field=fact.field,
                    value=fact.value,
                    provider_id=fact.provider_id,
                    external_id=fact.external_id,
                    confidence=fact.confidence,
                    retrieved_at=fact.retrieved_at,
                )
            )

    db.commit()
    db.refresh(run_row)

    return EnrichmentRunRead(
        id=run_row.id,
        company_id=run_row.company_id,
        status=run_row.status,
        started_at=run_row.started_at,
        provider_outcomes=[ProviderEnrichmentOutcomeRead(**outcome) for outcome in run_row.provider_outcomes],
    )


@router.get("/{company_id}/enrichment-runs/{run_id}", response_model=EnrichmentRunRead)
def get_enrichment_run(company_id: str, run_id: str, db: Session = Depends(get_db)) -> EnrichmentRunRead:
    run_row = db.get(EnrichmentRunModel, run_id)
    if run_row is None or run_row.company_id != company_id:
        raise HTTPException(status_code=404, detail="Enrichment run not found")

    return EnrichmentRunRead(
        id=run_row.id,
        company_id=run_row.company_id,
        status=run_row.status,
        started_at=run_row.started_at,
        provider_outcomes=[ProviderEnrichmentOutcomeRead(**outcome) for outcome in run_row.provider_outcomes],
    )


@router.get("/{company_id}/facts", response_model=CompanyFactsRead)
def get_company_facts(company_id: str, db: Session = Depends(get_db)) -> CompanyFactsRead:
    """The aggregated, current view of every fact ever gathered for this
    company, across every enrichment run to date — grouped by field, with
    conflicts flagged rather than resolved."""
    company_row = db.get(CanonicalCompanyModel, company_id)
    if company_row is None:
        raise HTTPException(status_code=404, detail="Company not found")

    fact_rows = (
        db.query(EnrichmentFactModel)
        .filter(EnrichmentFactModel.company_id == company_id)
        .order_by(EnrichmentFactModel.retrieved_at)
        .all()
    )
    facts = [
        EnrichmentFact(
            field=row.field,
            value=row.value,
            provider_id=row.provider_id,
            external_id=row.external_id,
            retrieved_at=row.retrieved_at,
            confidence=row.confidence,
        )
        for row in fact_rows
    ]

    return CompanyFactsRead(company_id=company_id, fields=list(group_facts_by_field(facts)))
