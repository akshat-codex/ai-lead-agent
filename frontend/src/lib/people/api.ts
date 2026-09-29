import { apiFetch } from "@/lib/api-client";
import type { CanonicalPerson, PersonResolution } from "./types";

interface ApiPersonResolution {
  id: string;
  candidate_id: string;
  people_discovery_run_id: string;
  status: "MATCH" | "NEW" | "UNRESOLVED";
  canonical_person_id: string | null;
  reason_code: string;
  explanation: string;
}

interface ApiCanonicalPerson {
  id: string;
  canonical_name: string;
  canonical_company_id: string | null;
  linkedin_id: string | null;
}

function fromApiResolution(api: ApiPersonResolution): PersonResolution {
  return {
    id: api.id,
    candidateId: api.candidate_id,
    peopleDiscoveryRunId: api.people_discovery_run_id,
    status: api.status,
    canonicalPersonId: api.canonical_person_id,
    reasonCode: api.reason_code,
    explanation: api.explanation,
  };
}

function fromApiPerson(api: ApiCanonicalPerson): CanonicalPerson {
  return {
    id: api.id,
    canonicalName: api.canonical_name,
    canonicalCompanyId: api.canonical_company_id,
    linkedinId: api.linkedin_id,
  };
}

export async function resolvePeopleDiscoveryRun(peopleDiscoveryRunId: string): Promise<PersonResolution[]> {
  const api = await apiFetch<ApiPersonResolution[]>("/api/v1/people/resolve", {
    method: "POST",
    body: { people_discovery_run_id: peopleDiscoveryRunId },
  });
  return api.map(fromApiResolution);
}

export async function getPerson(personId: string): Promise<CanonicalPerson> {
  const api = await apiFetch<ApiCanonicalPerson>(`/api/v1/people/${personId}`);
  return fromApiPerson(api);
}
