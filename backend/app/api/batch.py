"""Phase 21 API — batch orchestration.

Runs the existing pipeline synchronously, inside this request, for many
candidates under one ICP/version — see app/services/batch_orchestration.py
for the actual per-stage calls, every one of which is an unchanged
Phase 6-20 function. No Celery/Redis task is introduced: the project's own
Celery scaffolding (app/worker/celery_app.py) registers zero tasks and is
used nowhere else, so adding a background job runner here would be a new
piece of infrastructure this phase does not need — a batch of the sizes
this endpoint supports completes within one HTTP request/response cycle.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models.batch import BatchItemModel, BatchModel
from app.models.discovery import DiscoveryCandidateModel, DiscoveryRunModel
from app.models.icp import ICPModel
from app.providers.default_registry import get_provider_registry
from app.providers.registry import ProviderRegistry
from app.schemas.batch import BatchCreate, BatchDetailRead, BatchItemRead, BatchRead, BatchResumeRequest
from app.schemas.discovery import ProviderOutcomeRead
from app.services.batch_orchestration import run_batch
from app.services.person_enrichment import _MOCK_PROVIDER_PREFIX

router = APIRouter(prefix="/api/v1/batches", tags=["batch"])


def _clamp_for_live_test(discovery_limit: int, target_count: int, max_discovery_rounds: int) -> tuple[int, int, int]:
    """Phase 5 — SAFE SPEND/CALL GUARD, applied at request time (never
    inside the round loop, unlike max_discovery_rounds_per_batch, which
    stays a separate, unchanged, lifetime-of-the-batch backend ceiling —
    see that setting's own docstring).

    Fails closed: settings.live_test_mode defaults to True, so a fresh
    checkout with no .env changes at all gets the tighter limits
    automatically, with no action required. Only ever LOWERS a caller's
    requested value, never raises it — a caller asking for less than the
    live-test cap keeps exactly what they asked for. Returns
    (discovery_limit, target_count, max_discovery_rounds), each clamped
    independently."""
    settings = get_settings()
    if not settings.live_test_mode:
        return discovery_limit, target_count, max_discovery_rounds
    return (
        min(discovery_limit, settings.live_test_max_discovery_limit),
        min(target_count, settings.live_test_max_target_count),
        min(max_discovery_rounds, 1),
    )


def _used_mock_company_data(db: Session, items: list[BatchItemModel]) -> bool:
    """True when at least one of this batch's items was seeded from a
    mock-prefixed COMPANY_DISCOVERY provider (app/providers/mocks.py) —
    see BatchDetailRead.used_mock_company_data's own docstring for why
    this matters. Reuses the SAME _MOCK_PROVIDER_PREFIX convention
    app/services/person_enrichment.py already established, never a second
    definition of what "mock" means."""
    if not items:
        return False
    candidate_ids = [item.source_candidate_id for item in items]
    provider_ids = (
        db.query(DiscoveryCandidateModel.provider_id)
        .filter(DiscoveryCandidateModel.id.in_(candidate_ids))
        .distinct()
        .all()
    )
    return any(provider_id.startswith(_MOCK_PROVIDER_PREFIX) for (provider_id,) in provider_ids)


def _provider_call_outcomes(db: Session, items: list[BatchItemModel]) -> list[ProviderOutcomeRead]:
    """See BatchDetailRead.provider_call_outcomes's own docstring for the
    full root cause. Traces batch items -> their DiscoveryCandidateModel
    rows' run_id -> the DISTINCT DiscoveryRunModel rows those point to,
    then flattens every run's own provider_outcomes (already the real,
    per-provider call record app/services/company_discovery.py's
    run_company_discovery produces — never re-derived or guessed here)."""
    if not items:
        return []
    candidate_ids = [item.source_candidate_id for item in items]
    run_ids = {
        run_id
        for (run_id,) in db.query(DiscoveryCandidateModel.run_id).filter(DiscoveryCandidateModel.id.in_(candidate_ids)).distinct().all()
    }
    if not run_ids:
        return []
    run_rows = db.query(DiscoveryRunModel).filter(DiscoveryRunModel.id.in_(run_ids)).order_by(DiscoveryRunModel.started_at).all()
    outcomes: list[ProviderOutcomeRead] = []
    for run_row in run_rows:
        outcomes.extend(ProviderOutcomeRead(**outcome) for outcome in run_row.provider_outcomes)
    return outcomes


def _provider_call_counts(discovery_outcomes: list[ProviderOutcomeRead], batch: BatchModel) -> dict[str, int]:
    """Real call counts only — every entry here corresponds to an actual
    ProviderResponse this batch received, never an estimate. discovery
    (COMPANY_DISCOVERY) counts come straight from discovery_outcomes (one
    entry already exists per real call — see _provider_call_outcomes'
    own docstring); the discovery-strategy LLM call (Gemini/OpenAI/mock)
    is counted separately since it's a completely different capability,
    tracked via batch.discovery_strategy.provider_id — present exactly
    once per batch regardless of how many discovery rounds ran, since
    interpret_icp is called at most once and its result is cached (see
    BatchModel.discovery_strategy's own docstring)."""
    counts: dict[str, int] = {}
    for outcome in discovery_outcomes:
        counts[outcome.provider_id] = counts.get(outcome.provider_id, 0) + 1
    strategy = batch.discovery_strategy or {}
    strategy_provider_id = strategy.get("provider_id")
    if strategy_provider_id:
        counts[strategy_provider_id] = counts.get(strategy_provider_id, 0) + 1
    return counts


_EXPLORIUM_PROVIDER_ID = "explorium-company-discovery-v1"


def _estimated_explorium_credits(discovery_outcomes: list[ProviderOutcomeRead]) -> float | None:
    """See BatchDetailRead.estimated_explorium_credits's own docstring —
    an OBSERVED estimate, never a vendor-confirmed figure. Based on
    `returned` (real companies Explorium actually returned), not
    `requested` — a call that returned fewer than requested should not
    be estimated as if it cost for the ones it didn't return."""
    explorium_returned = sum(outcome.returned for outcome in discovery_outcomes if outcome.provider_id == _EXPLORIUM_PROVIDER_ID)
    if explorium_returned == 0 and not any(outcome.provider_id == _EXPLORIUM_PROVIDER_ID for outcome in discovery_outcomes):
        return None
    return explorium_returned * get_settings().explorium_estimated_credits_per_company


# A real LLM call was attempted for every deep_prescreen_status EXCEPT
# SKIPPED_NO_CONTENT, which app/services/deep_prescreen.py::run_prescreen
# returns BEFORE ever calling provider.qualify() (see that function's own
# docstring) — counting it as a "call" would overstate real LLM spend.
_NO_LLM_CALL_STATUS = "SKIPPED_NO_CONTENT"


def _deep_prescreen_counts(items: list[BatchItemModel]) -> tuple[int, int]:
    """Deep mode only — see BatchDetailRead.deep_prescreen_homepage_fetch_count/
    deep_prescreen_llm_call_count's own docstring for the full rationale.
    Both are 0 for every fast/safe/hard batch, since deep_prescreen_status/
    deep_prescreen_homepage_fetched are never set (stay None) for such a
    batch's items."""
    homepage_fetch_count = sum(1 for item in items if item.deep_prescreen_homepage_fetched)
    llm_call_count = sum(1 for item in items if item.deep_prescreen_status and item.deep_prescreen_status != _NO_LLM_CALL_STATUS)
    return homepage_fetch_count, llm_call_count


def _to_detail(db: Session, batch: BatchModel) -> BatchDetailRead:
    items = db.query(BatchItemModel).filter(BatchItemModel.batch_id == batch.id).order_by(BatchItemModel.created_at).all()
    discovery_outcomes = _provider_call_outcomes(db, items)
    homepage_fetch_count, llm_call_count = _deep_prescreen_counts(items)
    return BatchDetailRead(
        **BatchRead.model_validate(batch).model_dump(),
        items=[BatchItemRead.model_validate(item) for item in items],
        used_mock_company_data=_used_mock_company_data(db, items),
        provider_call_outcomes=discovery_outcomes,
        provider_call_counts=_provider_call_counts(discovery_outcomes, batch),
        estimated_explorium_credits=_estimated_explorium_credits(discovery_outcomes),
        deep_prescreen_homepage_fetch_count=homepage_fetch_count,
        deep_prescreen_llm_call_count=llm_call_count,
    )


@router.post("", response_model=BatchDetailRead, status_code=201)
def create_batch(
    payload: BatchCreate,
    db: Session = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> BatchDetailRead:
    icp_record = db.get(ICPModel, payload.icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    discovery_limit, target_count, _ = _clamp_for_live_test(payload.discovery_limit, payload.target_count, 1)
    batch = BatchModel(
        id=str(uuid4()),
        icp_id=icp_record.id,
        icp_version=icp_record.version,
        requested_target_count=target_count,
        discovery_limit=discovery_limit,
        people_limit_per_company=payload.people_limit_per_company,
        discovery_mode=payload.discovery_mode.value,
        status="PENDING",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    run_batch(db, registry, batch, icp_record.id)
    db.refresh(batch)
    return _to_detail(db, batch)


@router.post("/{batch_id}/resume", response_model=BatchDetailRead)
def resume_batch(
    batch_id: str,
    payload: BatchResumeRequest = BatchResumeRequest(),
    db: Session = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> BatchDetailRead:
    """Safe to call on any batch, including one already COMPLETED — items
    already at a terminal outcome are skipped (see run_batch), so resuming
    a fully-completed batch with no body is a no-op that reconfirms the
    same counts, never a source of duplicate canonical companies/people/
    leads.

    This is also the "Find More Leads" / target-count-continuation
    endpoint: `payload.max_discovery_rounds` (default 1, matching a plain
    bodyless call's exact prior behavior) bounds how many NEW discovery
    rounds this one call may seed — a manual "Find More" click sends 1; a
    target-count session sends a higher value (or calls this endpoint
    repeatedly) to keep going until the target is reached, the candidate
    pool is exhausted, or a real provider error stops it. New results are
    always appended to the batch's existing items — never replace or
    remove any prior item — so this call never wipes already-qualified
    leads, including when it stops early on a provider error such as
    Explorium credits being exhausted."""
    batch = db.get(BatchModel, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    _, _, max_discovery_rounds = _clamp_for_live_test(batch.discovery_limit, batch.requested_target_count, payload.max_discovery_rounds)
    run_batch(db, registry, batch, batch.icp_id, max_new_discovery_rounds=max_discovery_rounds, is_resume=True)
    db.refresh(batch)
    return _to_detail(db, batch)


@router.get("/{batch_id}", response_model=BatchDetailRead)
def get_batch(batch_id: str, db: Session = Depends(get_db)) -> BatchDetailRead:
    batch = db.get(BatchModel, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _to_detail(db, batch)


@router.get("", response_model=list[BatchRead])
def list_batches(db: Session = Depends(get_db)) -> list[BatchModel]:
    return db.query(BatchModel).order_by(BatchModel.started_at).all()


@router.get("/{batch_id}/items", response_model=list[BatchItemRead])
def list_batch_items(batch_id: str, db: Session = Depends(get_db)) -> list[BatchItemModel]:
    if db.get(BatchModel, batch_id) is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return db.query(BatchItemModel).filter(BatchItemModel.batch_id == batch_id).order_by(BatchItemModel.created_at).all()
