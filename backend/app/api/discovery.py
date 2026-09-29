"""Phase 6 API — start a discovery run, retrieve its results.

No qualification, scoring, or batch orchestration happens here — this
endpoint only persists what app/services/company_discovery.py found.

Phase 27 addition: before calling run_company_discovery, this endpoint
asks app/api/provider_routing.compute_routing() for the current provider
order — a purely additive lookup that returns the exact, unchanged
registry order whenever no optimization is active for this ICP/version
(the default, and today's out-of-the-box behavior). If that lookup ever
fails for any reason, provider_order simply stays None and
run_company_discovery falls back to its own original, unrouted behavior —
routing can only ever reorder calls, never block or break discovery.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.provider_routing import compute_routing
from app.db.session import get_db
from app.models.discovery import DiscoveryCandidateModel, DiscoveryRunModel
from app.models.icp import ICPModel
from app.providers.contracts import ProviderCapability
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.discovery import (
    CandidateCompanyRead,
    DiscoveryRunCreate,
    DiscoveryRunRead,
    ProviderOutcomeRead,
)
from app.services.company_discovery import run_company_discovery
from app.services.icp_normalization import IcpNormalizationError, normalize_icp

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])


def _to_read_model(
    run_row: DiscoveryRunModel, candidate_rows: list[DiscoveryCandidateModel]
) -> DiscoveryRunRead:
    return DiscoveryRunRead(
        id=run_row.id,
        icp_id=run_row.icp_id,
        icp_version=run_row.icp_version,
        status=run_row.status,
        started_at=run_row.started_at,
        requested_limit=run_row.requested_limit,
        total_returned=run_row.total_returned,
        provider_outcomes=[ProviderOutcomeRead(**outcome) for outcome in run_row.provider_outcomes],
        candidates=[CandidateCompanyRead.model_validate(row) for row in candidate_rows],
    )


@router.post("/runs", response_model=DiscoveryRunRead, status_code=201)
def start_discovery_run(
    payload: DiscoveryRunCreate,
    registry: ProviderRegistry = Depends(get_provider_registry),
    db: Session = Depends(get_db),
) -> DiscoveryRunRead:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    try:
        canonical = normalize_icp(
            icp_record.id, icp_record.version, icp_record.hard_rules, icp_record.soft_preferences
        )
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc

    provider_order = None
    try:
        routing = compute_routing(db, registry, icp_record.id, icp_record.version, ProviderCapability.COMPANY_DISCOVERY)
        provider_order = routing.ordered_provider_ids
    except Exception:
        provider_order = None  # routing is advisory-only; any failure here must never block discovery itself

    result = run_company_discovery(canonical, registry, limit=payload.limit, provider_order=provider_order)

    run_row = DiscoveryRunModel(
        id=result.run_id,
        icp_id=result.icp_id,
        icp_version=result.icp_version,
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

    candidate_rows: list[DiscoveryCandidateModel] = []
    for candidate in result.candidates:
        row = DiscoveryCandidateModel(
            id=candidate.id,
            run_id=result.run_id,
            icp_id=candidate.icp_id,
            icp_version=candidate.icp_version,
            provider_id=candidate.provider_id,
            external_id=candidate.external_id,
            name=candidate.name,
            domain=candidate.domain,
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


@router.get("/runs/{run_id}", response_model=DiscoveryRunRead)
def get_discovery_run(run_id: str, db: Session = Depends(get_db)) -> DiscoveryRunRead:
    run_row = db.get(DiscoveryRunModel, run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="Discovery run not found")

    candidate_rows = (
        db.query(DiscoveryCandidateModel)
        .filter(DiscoveryCandidateModel.run_id == run_id)
        .order_by(DiscoveryCandidateModel.discovered_at)
        .all()
    )
    return _to_read_model(run_row, candidate_rows)
