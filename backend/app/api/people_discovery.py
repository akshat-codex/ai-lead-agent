"""Phase 9 API — discover candidate decision-makers for a canonical company
under a given ICP's allowed titles.

No identity verification, deduplication, or qualification happens here —
only app/services/people_discovery.py's raw discovery, persisted for audit.
Never creates a company and never re-runs company entity resolution
(Phase 7); a 404 on company_id means "resolve it first," not "create one."

Phase 27 addition: see app/api/discovery.py's own docstring — the same
purely additive, fail-open provider-routing lookup is applied here before
calling run_people_discovery.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.provider_routing import compute_routing
from app.db.session import get_db
from app.models.company import CanonicalCompanyModel
from app.models.icp import ICPModel
from app.models.people_discovery import PeopleDiscoveryCandidateModel, PeopleDiscoveryRunModel
from app.providers.contracts import ProviderCapability
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.company_resolution import ExistingCompanyIdentity
from app.schemas.people_discovery import (
    CandidatePersonRead,
    PeopleDiscoveryRunCreate,
    PeopleDiscoveryRunRead,
    ProviderOutcomeRead,
)
from app.services.icp_normalization import IcpNormalizationError, normalize_icp
from app.services.people_discovery import run_people_discovery

router = APIRouter(prefix="/api/v1/people-discovery", tags=["people-discovery"])


def _to_identity(row: CanonicalCompanyModel) -> ExistingCompanyIdentity:
    return ExistingCompanyIdentity(
        id=row.id,
        canonical_name=row.canonical_name,
        canonical_domain=row.canonical_domain,
        aliases=tuple(row.aliases),
        provider_identities=dict(row.provider_identities),
    )


def _to_read_model(
    run_row: PeopleDiscoveryRunModel, candidate_rows: list[PeopleDiscoveryCandidateModel]
) -> PeopleDiscoveryRunRead:
    return PeopleDiscoveryRunRead(
        id=run_row.id,
        icp_id=run_row.icp_id,
        icp_version=run_row.icp_version,
        company_id=run_row.company_id,
        status=run_row.status,
        started_at=run_row.started_at,
        requested_limit=run_row.requested_limit,
        total_returned=run_row.total_returned,
        provider_outcomes=[ProviderOutcomeRead(**outcome) for outcome in run_row.provider_outcomes],
        candidates=[CandidatePersonRead.model_validate(row) for row in candidate_rows],
    )


@router.post("/runs", response_model=PeopleDiscoveryRunRead, status_code=201)
def start_people_discovery_run(
    payload: PeopleDiscoveryRunCreate,
    registry: ProviderRegistry = Depends(get_provider_registry),
    db: Session = Depends(get_db),
) -> PeopleDiscoveryRunRead:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    company_record = db.get(CanonicalCompanyModel, payload.company_id)
    if company_record is None:
        raise HTTPException(status_code=404, detail="Company not found")

    try:
        canonical_icp = normalize_icp(
            icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences
        )
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc

    provider_order = None
    try:
        routing = compute_routing(db, registry, icp_record.id, icp_record.version, ProviderCapability.PEOPLE_DISCOVERY)
        provider_order = routing.ordered_provider_ids
    except Exception:
        provider_order = None  # routing is advisory-only; any failure here must never block discovery itself

    result = run_people_discovery(canonical_icp, _to_identity(company_record), registry, limit=payload.limit, provider_order=provider_order)

    run_row = PeopleDiscoveryRunModel(
        id=result.run_id,
        icp_id=result.icp_id,
        icp_version=result.icp_version,
        company_id=result.company_id,
        status=result.status.value,
        requested_limit=payload.limit,
        total_returned=len(result.candidates),
        provider_outcomes=[
            {
                "provider_id": outcome.provider_id,
                "success": outcome.success,
                "requested": outcome.requested,
                "returned": outcome.returned,
                "latency_ms": outcome.latency_ms,
                "error_code": outcome.error.code if outcome.error else None,
                "error_message": outcome.error.message if outcome.error else None,
            }
            for outcome in result.provider_outcomes
        ],
    )
    db.add(run_row)

    candidate_rows: list[PeopleDiscoveryCandidateModel] = []
    for candidate in result.candidates:
        row = PeopleDiscoveryCandidateModel(
            id=candidate.id,
            run_id=result.run_id,
            icp_id=candidate.icp_id,
            icp_version=candidate.icp_version,
            company_id=candidate.company_id,
            provider_id=candidate.provider_id,
            external_id=candidate.external_id,
            name=candidate.name,
            title=candidate.title,
            attributes=candidate.attributes,
            discovered_at=candidate.discovered_at,
        )
        db.add(row)
        candidate_rows.append(row)

    db.commit()
    db.refresh(run_row)
    for row in candidate_rows:
        db.refresh(row)

    return _to_read_model(run_row, candidate_rows)


@router.get("/runs/{run_id}", response_model=PeopleDiscoveryRunRead)
def get_people_discovery_run(run_id: str, db: Session = Depends(get_db)) -> PeopleDiscoveryRunRead:
    run_row = db.get(PeopleDiscoveryRunModel, run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="People discovery run not found")

    candidate_rows = (
        db.query(PeopleDiscoveryCandidateModel)
        .filter(PeopleDiscoveryCandidateModel.run_id == run_id)
        .order_by(PeopleDiscoveryCandidateModel.discovered_at)
        .all()
    )
    return _to_read_model(run_row, candidate_rows)
