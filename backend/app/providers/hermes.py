"""Phase 34 — Hermes ICP Search client: a SECONDARY, complementary
COMPANY_DISCOVERY source, deliberately NOT a ProviderAdapter subclass.

Hermes is fundamentally different from every other provider in this
codebase (Explorium, Apollo, Unipile): it is an ASYNC, agent-driven web
research service (Gemini Flash + headless Chromium browsing DuckDuckGo and
company sites), not a synchronous structured-database lookup. A search
takes MINUTES (the vendor's own docs show ~400s for 3 records) and the
service enforces MAX_CONCURRENT_JOBS=1 globally — one job runs at a time,
for every caller of this deployment, full stop.

app/providers/base.py's ProviderAdapter.execute() contract expects one
call to return a complete ProviderResponse immediately (see
app/services/company_discovery.py::run_company_discovery, which calls
`provider.run(request)` synchronously in a loop for every registered
COMPANY_DISCOVERY provider). Forcing Hermes's submit-then-poll-for-minutes
shape into that interface would mean either fabricating an empty
synchronous response (silently discarding the job) or blocking an HTTP
request thread for minutes — neither is acceptable. Instead, Hermes gets
its own explicit two-call contract (submit_hermes_job / check_hermes_job),
and app/services/batch_orchestration.py drives it across MULTIPLE HTTP
requests (submit on one resume call, check on a later one) — see that
module's _maybe_advance_hermes_job for the actual orchestration and
app/models/batch.py's hermes_job_id/hermes_job_status/hermes_submitted_at
columns for how job state survives between calls.

API contract (from the vendor's own Postman collection — not guessed):
  POST {base_url}/search_icp
    Headers: Authorization: Bearer {token}, Content-Type: application/json
    Body: only `num_records` (int 1-200) and `instructions` (free text) are
      reserved; every other key is forwarded verbatim into the agent's
      research prompt as a criteria line — there is no fixed request
      schema to validate client-side.
    202 -> {job_id, status: "queued"|"running", status_url, queue_depth,
            slots, poll_after_seconds}
    401 bad/missing token; 422 num_records out of 1-200; 429 queue full
      (Retry-After header).

  GET {base_url}/status/{job_id}
    200 -> {job_id, status, done: bool, elapsed_seconds, ...}
    Non-terminal: status in ("queued", "running"), done=false.
    Terminal (done=true): status in ("completed", "failed", "timeout",
      "rate_limited", "cancelled"). Only "completed" carries `records` (a
      list of dicts — company_name, website, linkedin_url, employee_size,
      industry, location, description, source_url; ANY field may be null
      where the agent couldn't verify it, and `count` can be less than the
      requested num_records).
    404 -> unknown/expired job_id.

Every record from this client is an honest, single, UNVERIFIED web-research
sighting — never a structured taxonomy match. This client's provider_id
("hermes-icp-search-v1") is deliberately NEVER added to
app/services/evidence_engine.py's _TRUSTED_STRUCTURED_PROVIDERS: a lone
Hermes sighting must never reach SUPPORTED_STRUCTURED the way a lone
Explorium sighting can for industry/country/employee_range. This is not an
oversight to fix later — it is the correct, permanent trust level for a
provider whose fields are LLM-agent free text read off a webpage, not a
verified database lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

HERMES_PROVIDER_ID = "hermes-icp-search-v1"

HERMES_AUTH_FAILED = "HERMES_AUTH_FAILED"
HERMES_QUEUE_FULL = "HERMES_QUEUE_FULL"
HERMES_INVALID_REQUEST = "HERMES_INVALID_REQUEST"
HERMES_UNAVAILABLE = "HERMES_UNAVAILABLE"
HERMES_JOB_NOT_FOUND = "HERMES_JOB_NOT_FOUND"

# The vendor's own documented terminal states (done: true) that are NOT a
# successful completion — see this module's own docstring's Job states
# table. "completed" is the only state with usable `records`.
_TERMINAL_NON_SUCCESS_STATUSES = frozenset({"failed", "timeout", "rate_limited", "cancelled"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HermesSubmitError:
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class HermesSubmitResult:
    success: bool
    job_id: str | None = None
    status: str | None = None
    queue_position: int | None = None
    poll_after_seconds: float | None = None
    error: HermesSubmitError | None = None


@dataclass(frozen=True)
class HermesStatusResult:
    success: bool
    job_id: str | None = None
    status: str | None = None
    done: bool = False
    records: tuple[dict[str, Any], ...] = ()
    error: HermesSubmitError | None = None


def submit_hermes_job(
    *,
    base_url: str,
    api_token: str,
    criteria: dict[str, Any],
    num_records: int,
    instructions: str | None = None,
    timeout_seconds: float = 15.0,
) -> HermesSubmitResult:
    """POST /search_icp. Returns immediately (202) — never waits for the
    search itself. `criteria` is forwarded as-is alongside num_records/
    instructions: per the vendor's own docs, there is no fixed request
    schema, so this function does not (and must not) invent one — the
    caller (app/services/batch_orchestration.py) is responsible for
    building criteria from the canonical ICP's own real fields, never a
    hardcoded industry-specific shape."""
    body: dict[str, Any] = dict(criteria)
    body["num_records"] = num_records
    if instructions:
        body["instructions"] = instructions

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/search_icp",
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        return HermesSubmitResult(success=False, error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=str(exc), retryable=True))
    except httpx.HTTPError as exc:
        return HermesSubmitResult(success=False, error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=str(exc), retryable=True))

    if response.status_code == 401:
        return HermesSubmitResult(success=False, error=HermesSubmitError(code=HERMES_AUTH_FAILED, message="Hermes rejected the bearer token", retryable=False))
    if response.status_code == 422:
        return HermesSubmitResult(
            success=False,
            error=HermesSubmitError(code=HERMES_INVALID_REQUEST, message=f"Hermes rejected the request: {response.text[:500]}", retryable=False),
        )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        return HermesSubmitResult(
            success=False,
            error=HermesSubmitError(
                code=HERMES_QUEUE_FULL,
                message="Hermes job queue is full",
                retryable=True,
                retry_after_seconds=float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None,
            ),
        )
    if response.status_code != 202:
        return HermesSubmitResult(
            success=False,
            error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=f"Hermes returned HTTP {response.status_code}: {response.text[:500]}", retryable=True),
        )

    try:
        payload = response.json()
        job_id = payload["job_id"]
    except (ValueError, KeyError) as exc:
        return HermesSubmitResult(success=False, error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=f"could not read Hermes submit response: {exc}", retryable=False))

    return HermesSubmitResult(
        success=True,
        job_id=job_id,
        status=payload.get("status"),
        queue_position=payload.get("queue_position"),
        poll_after_seconds=payload.get("poll_after_seconds"),
    )


def check_hermes_job(
    *,
    base_url: str,
    api_token: str,
    job_id: str,
    timeout_seconds: float = 15.0,
) -> HermesStatusResult:
    """GET /status/{job_id}. ONE check, never a loop — the caller decides
    whether/when to check again on a LATER call (see this module's own
    docstring on why Hermes is driven across multiple HTTP requests, never
    polled in a blocking loop inside one)."""
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/status/{job_id}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        return HermesStatusResult(success=False, error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=str(exc), retryable=True))
    except httpx.HTTPError as exc:
        return HermesStatusResult(success=False, error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=str(exc), retryable=True))

    if response.status_code == 404:
        return HermesStatusResult(success=False, error=HermesSubmitError(code=HERMES_JOB_NOT_FOUND, message="Hermes job not found or expired", retryable=False))
    if response.status_code == 401:
        return HermesStatusResult(success=False, error=HermesSubmitError(code=HERMES_AUTH_FAILED, message="Hermes rejected the bearer token", retryable=False))
    if response.status_code != 200:
        return HermesStatusResult(
            success=False,
            error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=f"Hermes returned HTTP {response.status_code}: {response.text[:500]}", retryable=True),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return HermesStatusResult(success=False, error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=f"could not parse Hermes status response: {exc}", retryable=False))

    status = payload.get("status")
    done = bool(payload.get("done"))

    if done and status in _TERMINAL_NON_SUCCESS_STATUSES:
        # An honest, non-fabricated terminal failure — the vendor's own
        # error field (or a safe default) is surfaced as-is, never
        # invented, and no records are returned for a non-"completed"
        # terminal state even if the payload happens to carry a stray one.
        return HermesStatusResult(
            success=False,
            job_id=payload.get("job_id", job_id),
            status=status,
            done=True,
            error=HermesSubmitError(code=HERMES_UNAVAILABLE, message=str(payload.get("error") or f"Hermes job ended with status={status}"), retryable=False),
        )

    records = payload.get("records") if done and status == "completed" else None
    return HermesStatusResult(
        success=True,
        job_id=payload.get("job_id", job_id),
        status=status,
        done=done,
        records=tuple(records) if isinstance(records, list) else (),
    )
