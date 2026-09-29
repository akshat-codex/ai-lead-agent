import { apiFetch } from "@/lib/api-client";
import type { PersonEnrichmentRun, PersonEnrichmentRunStatus } from "./types";

interface ApiPersonEnrichmentRun {
  id: string;
  person_id: string;
  status: string;
  provider_id: string | null;
  error_code: string | null;
  error_message: string | null;
}

function fromApiRun(api: ApiPersonEnrichmentRun): PersonEnrichmentRun {
  return {
    id: api.id,
    personId: api.person_id,
    status: api.status as PersonEnrichmentRunStatus,
    providerId: api.provider_id,
    errorMessage: api.error_message,
  };
}

export async function enrichPerson(personId: string): Promise<PersonEnrichmentRun> {
  const api = await apiFetch<ApiPersonEnrichmentRun>(`/api/v1/people/${personId}/enrich`, {
    method: "POST",
  });
  return fromApiRun(api);
}
