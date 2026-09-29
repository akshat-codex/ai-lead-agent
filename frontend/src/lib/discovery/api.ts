import { apiFetch } from "@/lib/api-client";
import type { CandidateCompany, DiscoveryRun } from "./types";

interface ApiCandidateCompany {
  id: string;
  icp_id: string;
  provider_id: string;
  external_id: string;
  name: string;
  domain: string | null;
  attributes: Record<string, unknown>;
  discovered_at: string;
}

interface ApiDiscoveryRun {
  id: string;
  icp_id: string;
  status: string;
  requested_limit: number;
  total_returned: number;
  candidates: ApiCandidateCompany[];
}

function fromApiCandidate(api: ApiCandidateCompany): CandidateCompany {
  return {
    id: api.id,
    icpId: api.icp_id,
    providerId: api.provider_id,
    externalId: api.external_id,
    name: api.name,
    domain: api.domain,
    attributes: api.attributes,
    discoveredAt: api.discovered_at,
  };
}

function fromApiDiscoveryRun(api: ApiDiscoveryRun): DiscoveryRun {
  return {
    id: api.id,
    icpId: api.icp_id,
    status: api.status,
    requestedLimit: api.requested_limit,
    totalReturned: api.total_returned,
    candidates: api.candidates.map(fromApiCandidate),
  };
}

export async function startDiscoveryRun(icpId: string, limit = 20): Promise<DiscoveryRun> {
  const api = await apiFetch<ApiDiscoveryRun>("/api/v1/discovery/runs", {
    method: "POST",
    body: { icp_id: icpId, limit },
  });
  return fromApiDiscoveryRun(api);
}

export async function getDiscoveryRun(runId: string): Promise<DiscoveryRun> {
  const api = await apiFetch<ApiDiscoveryRun>(`/api/v1/discovery/runs/${runId}`);
  return fromApiDiscoveryRun(api);
}
