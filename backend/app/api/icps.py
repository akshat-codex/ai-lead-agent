from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.icp import ICPModel
from app.schemas.candidate import Candidate
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.hard_rule_result import HardRuleEvaluation
from app.schemas.icp import ICPCreate, ICPRead
from app.services.filter_catalog import FilterDefinition, list_filter_definitions
from app.services.hard_rule_engine import evaluate_hard_rules
from app.services.icp_normalization import IcpNormalizationError, normalize_icp

router = APIRouter(prefix="/api/v1/icps", tags=["icps"])


@router.post("", response_model=ICPRead, status_code=201)
def create_icp(payload: ICPCreate, db: Session = Depends(get_db)) -> ICPModel:
    """Save a new ICP draft.

    Versioning is derived from the name: saving under a name that already
    exists creates the next version rather than overwriting it.
    """
    name = payload.name.strip()
    latest_version = (
        db.query(func.max(ICPModel.version))
        .filter(func.lower(ICPModel.name) == name.lower())
        .scalar()
    )

    # payload.hard_rules/soft_preferences are guaranteed non-None here: the
    # ICPCreate model_validator either received them directly or derived them
    # from payload.filters via project_filters().
    record = ICPModel(
        id=str(uuid4()),
        name=name,
        version=(latest_version or 0) + 1,
        hard_rules=payload.hard_rules.model_dump(),
        soft_preferences=payload.soft_preferences.model_dump(),
        filters=[f.model_dump(mode="json") for f in payload.filters],
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("", response_model=list[ICPRead])
def list_icps(db: Session = Depends(get_db)) -> list[ICPModel]:
    return db.query(ICPModel).order_by(ICPModel.name, ICPModel.version).all()


@router.get("/filter-catalog", response_model=list[FilterDefinition])
def get_filter_catalog() -> list[FilterDefinition]:
    """Returns every filter the ICP/search builder currently supports, for the
    frontend's '+ Add filter, search' UX. Backend-owned and versionless -
    adding a FilterDefinition to app/services/filter_catalog.py is the only
    change needed for a new filter to appear here.

    Registered before GET /{icp_id} - FastAPI matches routes in registration
    order, and "filter-catalog" would otherwise be swallowed by the
    {icp_id} path parameter.
    """
    return list(list_filter_definitions())


@router.get("/{icp_id}", response_model=ICPRead)
def get_icp(icp_id: str, db: Session = Depends(get_db)) -> ICPModel:
    record = db.get(ICPModel, icp_id)
    if record is None:
        raise HTTPException(status_code=404, detail="ICP not found")
    return record


@router.get("/{icp_id}/canonical", response_model=CanonicalICP)
def get_canonical_icp(icp_id: str, db: Session = Depends(get_db)) -> CanonicalICP:
    """Normalizes a saved ICP into the canonical form Phase 3 onward consumes."""
    record = db.get(ICPModel, icp_id)
    if record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    try:
        return normalize_icp(record.id, record.version, record.hard_rules, record.soft_preferences)
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc


@router.post("/{icp_id}/evaluate", response_model=HardRuleEvaluation)
def evaluate_candidate(icp_id: str, candidate: Candidate, db: Session = Depends(get_db)) -> HardRuleEvaluation:
    """Runs the Phase 3 Hard ICP Rule Engine for one candidate against one saved ICP."""
    record = db.get(ICPModel, icp_id)
    if record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    try:
        canonical = normalize_icp(record.id, record.version, record.hard_rules, record.soft_preferences)
    except IcpNormalizationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc

    return evaluate_hard_rules(canonical, candidate)
