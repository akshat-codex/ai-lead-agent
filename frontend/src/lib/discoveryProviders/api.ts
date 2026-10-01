import { apiFetch } from "@/lib/api-client";

export type EvidenceKind = "structured" | "web_search" | "research_agent";

export interface DiscoveryProviderStatus {
  providerId: string;
  providerName: string;
  configured: boolean;
  evidenceKind: EvidenceKind;
  description: string;
  envVars: string[];
}

interface ApiDiscoveryProviderStatus {
  provider_id: string;
  provider_name: string;
  configured: boolean;
  evidence_kind: EvidenceKind;
  description: string;
  env_vars: string[];
}

function fromApi(api: ApiDiscoveryProviderStatus): DiscoveryProviderStatus {
  return {
    providerId: api.provider_id,
    providerName: api.provider_name,
    configured: api.configured,
    evidenceKind: api.evidence_kind,
    description: api.description,
    envVars: api.env_vars,
  };
}

/** Real, honest configuration status for every COMPANY_DISCOVERY provider
 * this codebase knows how to integrate — see backend/app/api/
 * discovery_providers.py's own module docstring. Deliberately carries no
 * "accuracy %" field: there is no real measured accuracy anywhere in this
 * codebase to report, only genuine configuration status and a factual
 * description of what kind of evidence each provider contributes. */
export async function listDiscoveryProviders(): Promise<DiscoveryProviderStatus[]> {
  const body = await apiFetch<ApiDiscoveryProviderStatus[]>("/api/v1/discovery-providers");
  return body.map(fromApi);
}
