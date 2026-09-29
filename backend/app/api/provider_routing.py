"""Phase 27 API — provider routing and adaptive search.

Reuses Phase 26's `EffectiveConfiguration` read model UNCHANGED (via the
same in-process import pattern established by Phase 22-26: `_build_result`,
`_compute_ranking`, etc.) and Phase 5's own `ProviderRegistry.find_by_capability()`
UNCHANGED — this module adds only the reordering decision and its audit
trail. Never mutates the registry, the ICP, evidence, feedback, scores, or
any optimization-application row it reads.

`get_effective_configuration` is imported directly from
app/api/optimization_application.py rather than reimplemented, so the
"only reorder when an explicitly approved active optimization exists"
rule is enforced by construction: this module never computes its own
notion of "is an optimization active," it only ever asks Phase 26.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.optimization_application import get_effective_configuration
from app.db.session import get_db
from app.models.commercial_signal import CommercialSignalModel
from app.models.icp import ICPModel
from app.providers.contracts import ProviderCapability
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.provider_routing import ProviderRoutingResult
from app.services.provider_routing import route_providers

router = APIRouter(prefix="/api/v1/provider-routing", tags=["provider-routing"])


def _provider_signal_names(db: Session) -> dict[str, frozenset[str]]:
    """Maps provider_id -> the set of commercial-signal names real Phase
    14 evidence has ever attributed to that provider, via
    CommercialSignalModel.provider_ids — the only source of "does this
    provider actually relate to this signal" this module trusts. A
    provider that has never contributed to any recorded signal simply has
    an empty set here, never a guessed one."""
    mapping: dict[str, set[str]] = {}
    for row in db.query(CommercialSignalModel).all():
        signal_name = f"commercial_signal:{row.signal_type}"
        for provider_id in row.provider_ids:
            mapping.setdefault(provider_id, set()).add(signal_name)
    return {pid: frozenset(names) for pid, names in mapping.items()}


def compute_routing(
    db: Session,
    registry: ProviderRegistry,
    icp_id: str,
    icp_version: int,
    capability: ProviderCapability,
) -> ProviderRoutingResult:
    """The single function both this API and the discovery/people-discovery
    call sites use to get a routing decision — one implementation, reused
    everywhere routing is needed, per the task's "make minimal changes."
    """
    effective_config = get_effective_configuration(icp_id=icp_id, icp_version=icp_version, db=db)
    candidates = registry.find_by_capability(capability)
    provider_signal_names = _provider_signal_names(db)
    return route_providers(
        icp_id=icp_id,
        icp_version=icp_version,
        capability=capability,
        candidates=candidates,
        effective_config=effective_config,
        provider_signal_names=provider_signal_names,
        generated_at=datetime.now(timezone.utc),
    )


@router.get("", response_model=ProviderRoutingResult)
def get_provider_routing(
    icp_id: str = Query(..., min_length=1),
    icp_version: int = Query(...),
    capability: ProviderCapability = Query(...),
    db: Session = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> ProviderRoutingResult:
    icp_record = db.get(ICPModel, icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    return compute_routing(db, registry, icp_id, icp_version, capability)
