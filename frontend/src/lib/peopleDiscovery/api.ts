import { apiFetch } from "@/lib/api-client";
import type { CandidatePerson, PeopleDiscoveryRun } from "./types";

interface ApiCandidatePerson {
  id: string;
  company_id: string;
  icp_id: string;
  provider_id: string;
  external_id: string;
  name: string;
  title: string | null;
  attributes: Record<string, unknown>;
  discovered_at: string;
}

interface ApiPeopleDiscoveryRun {
  id: string;
  icp_id: string;
  company_id: string;
  status: string;
  requested_limit: number;
  total_returned: number;
  candidates: ApiCandidatePerson[];
}

function fromApiCandidate(api: ApiCandidatePerson): CandidatePerson {
  return {
    id: api.id,
    companyId: api.company_id,
    icpId: api.icp_id,
    providerId: api.provider_id,
    externalId: api.external_id,
    name: api.name,
    title: api.title,
    attributes: api.attributes,
    discoveredAt: api.discovered_at,
  };
}

function fromApiRun(api: ApiPeopleDiscoveryRun): PeopleDiscoveryRun {
  return {
    id: api.id,
    icpId: api.icp_id,
    companyId: api.company_id,
    status: api.status,
    requestedLimit: api.requested_limit,
    totalReturned: api.total_returned,
    candidates: api.candidates.map(fromApiCandidate),
  };
}

/**
 * The backend derives allowed titles from the saved ICP's hard_rules
 * server-side (see build_people_discovery_query in
 * backend/app/services/people_discovery.py) — this request never sends
 * titles itself, only icp_id/company_id/limit.
 */
export async function startPeopleDiscoveryRun(
  icpId: string,
  companyId: string,
  limit = 20,
): Promise<PeopleDiscoveryRun> {
  const api = await apiFetch<ApiPeopleDiscoveryRun>("/api/v1/people-discovery/runs", {
    method: "POST",
    body: { icp_id: icpId, company_id: companyId, limit },
  });
  return fromApiRun(api);
}

export async function getPeopleDiscoveryRun(runId: string): Promise<PeopleDiscoveryRun> {
  const api = await apiFetch<ApiPeopleDiscoveryRun>(`/api/v1/people-discovery/runs/${runId}`);
  return fromApiRun(api);
}
