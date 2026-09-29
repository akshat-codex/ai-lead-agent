import { apiFetch } from "@/lib/api-client";

/** Which COMPANY_DISCOVERY provider(s) a batch calls each round — see
 * backend/app/schemas/batch.py::DiscoveryMode's own docstring. "fast" is
 * today's pre-existing Explorium-only behavior; the backend also defaults
 * to "fast" when this is omitted, so every caller that doesn't yet pass
 * one gets byte-for-byte identical behavior to before this existed. */
export type DiscoveryMode = "fast" | "safe" | "hard";

export interface Batch {
  id: string;
  icpId: string;
  icpVersion: number;
  requestedTargetCount: number;
  discoveryLimit: number;
  peopleLimitPerCompany: number;
  discoveryMode: DiscoveryMode;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "COMPLETED_WITH_ERRORS";
  discoveredCount: number;
  deduplicatedLeadCount: number;
  acceptedCount: number;
  heldCount: number;
  rejectedCount: number;
  duplicateCount: number;
  failedCount: number;
  discoveryExhaustedProviders: string[];
  discoveryErrorCode: string | null;
  discoveryRoundsRun: number;
  discoveryPoolExhausted: boolean;
  startedAt: string;
  completedAt: string | null;
  /** Non-null while a Hermes (app/providers/hermes.py) async discovery job
   * is submitted but not yet resolved — see backend/app/models/batch.py's
   * own hermes_job_id/hermes_job_status columns. Null once the job
   * completes/fails and its records (if any) have been imported. */
  hermesJobId: string | null;
  hermesJobStatus: string | null;
  /** Real per-provider call counts this batch made (COMPANY_DISCOVERY
   * calls plus the one discovery-strategy LLM call) — e.g.
   * {"explorium-company-discovery-v1": 2, "tavily-company-discovery-v1": 1}.
   * See backend/app/schemas/batch.py::BatchDetailRead.provider_call_counts's
   * own docstring: never a dollar estimate, only real call counts. Only
   * present on the detail response (create/resume/get-one), not on the
   * plain list response — see backend/app/api/batch.py's BatchRead vs.
   * BatchDetailRead split. */
  providerCallCounts: Record<string, number>;
  /** A rough, user-configured ESTIMATE of Explorium credits spent this
   * batch (real companies returned * an observed credits-per-company
   * rate) — never a vendor-confirmed figure, since Explorium exposes no
   * API for real credit balance. Null when Explorium made no calls this
   * batch. Same detail-only availability as providerCallCounts above. */
  estimatedExploriumCredits: number | null;
}

interface ApiBatch {
  id: string;
  icp_id: string;
  icp_version: number;
  requested_target_count: number;
  discovery_limit: number;
  people_limit_per_company: number;
  discovery_mode: string;
  status: string;
  discovered_count: number;
  deduplicated_lead_count: number;
  accepted_count: number;
  held_count: number;
  rejected_count: number;
  duplicate_count: number;
  failed_count: number;
  discovery_exhausted_providers: string[];
  discovery_error_code: string | null;
  discovery_rounds_run: number;
  discovery_pool_exhausted: boolean;
  started_at: string;
  completed_at: string | null;
  hermes_job_id: string | null;
  hermes_job_status: string | null;
  provider_call_counts?: Record<string, number>;
  estimated_explorium_credits?: number | null;
}

function fromApiBatch(api: ApiBatch): Batch {
  return {
    id: api.id,
    icpId: api.icp_id,
    icpVersion: api.icp_version,
    requestedTargetCount: api.requested_target_count,
    discoveryLimit: api.discovery_limit,
    peopleLimitPerCompany: api.people_limit_per_company,
    discoveryMode: (api.discovery_mode as DiscoveryMode) ?? "fast",
    status: api.status as Batch["status"],
    discoveredCount: api.discovered_count,
    deduplicatedLeadCount: api.deduplicated_lead_count,
    acceptedCount: api.accepted_count,
    heldCount: api.held_count,
    rejectedCount: api.rejected_count,
    duplicateCount: api.duplicate_count,
    failedCount: api.failed_count,
    discoveryExhaustedProviders: api.discovery_exhausted_providers,
    discoveryErrorCode: api.discovery_error_code,
    discoveryRoundsRun: api.discovery_rounds_run,
    discoveryPoolExhausted: api.discovery_pool_exhausted,
    startedAt: api.started_at,
    completedAt: api.completed_at,
    hermesJobId: api.hermes_job_id,
    hermesJobStatus: api.hermes_job_status,
    providerCallCounts: api.provider_call_counts ?? {},
    estimatedExploriumCredits: api.estimated_explorium_credits ?? null,
  };
}

/** Creates a batch and runs it synchronously — the real quality pipeline
 * (discovery -> resolution -> evidence import -> hard ICP validation ->
 * scoring -> qualification -> dedup) for the first page of candidates,
 * capped at `targetCount`. */
export async function createBatch(
  icpId: string,
  targetCount: number,
  discoveryLimit = 20,
  discoveryMode: DiscoveryMode = "fast",
): Promise<Batch> {
  const api = await apiFetch<ApiBatch>("/api/v1/batches", {
    method: "POST",
    body: { icp_id: icpId, target_count: targetCount, discovery_limit: discoveryLimit, discovery_mode: discoveryMode },
  });
  return fromApiBatch(api);
}

/** "Find More Leads" / target-count continuation: seeds up to
 * `maxDiscoveryRounds` NEW discovery rounds into an already-created batch,
 * using each provider's persisted continuation cursor (never restarting
 * from page 1), and runs every newly-discovered candidate through the same
 * full pipeline. Existing results are never replaced — only appended to. */
export async function resumeBatch(batchId: string, maxDiscoveryRounds = 1): Promise<Batch> {
  const api = await apiFetch<ApiBatch>(`/api/v1/batches/${batchId}/resume`, {
    method: "POST",
    body: { max_discovery_rounds: maxDiscoveryRounds },
  });
  return fromApiBatch(api);
}

export async function getBatch(batchId: string): Promise<Batch> {
  const api = await apiFetch<ApiBatch>(`/api/v1/batches/${batchId}`);
  return fromApiBatch(api);
}
