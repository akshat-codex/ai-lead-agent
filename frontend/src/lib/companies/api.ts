import { apiFetch, ApiError } from "@/lib/api-client";
import type { CanonicalCompany, CompanyFacts, CompanyResolution } from "./types";

interface ApiResolution {
  id: string;
  candidate_id: string;
  discovery_run_id: string;
  status: "MATCH" | "NEW" | "UNRESOLVED";
  canonical_company_id: string | null;
  reason_code: string;
  explanation: string;
}

interface ApiCanonicalCompany {
  id: string;
  canonical_name: string;
  canonical_domain: string | null;
  aliases: string[];
  provider_identities: Record<string, string>;
}

interface ApiEnrichmentFact {
  field: string;
  value: unknown;
  provider_id: string;
  confidence: number | null;
}

interface ApiEnrichmentField {
  field: string;
  facts: ApiEnrichmentFact[];
  conflict: boolean;
}

interface ApiCompanyFacts {
  company_id: string;
  fields: ApiEnrichmentField[];
}

function fromApiResolution(api: ApiResolution): CompanyResolution {
  return {
    id: api.id,
    candidateId: api.candidate_id,
    discoveryRunId: api.discovery_run_id,
    status: api.status,
    canonicalCompanyId: api.canonical_company_id,
    reasonCode: api.reason_code,
    explanation: api.explanation,
  };
}

function fromApiCompany(api: ApiCanonicalCompany): CanonicalCompany {
  return {
    id: api.id,
    canonicalName: api.canonical_name,
    canonicalDomain: api.canonical_domain,
    aliases: api.aliases,
    providerIdentities: api.provider_identities,
  };
}

function fromApiFacts(api: ApiCompanyFacts): CompanyFacts {
  return {
    companyId: api.company_id,
    fields: api.fields.map((f) => ({
      field: f.field,
      conflict: f.conflict,
      facts: f.facts.map((fact) => ({
        field: fact.field,
        value: fact.value,
        providerId: fact.provider_id,
        confidence: fact.confidence,
      })),
    })),
  };
}

export async function resolveDiscoveryRun(discoveryRunId: string): Promise<CompanyResolution[]> {
  const api = await apiFetch<ApiResolution[]>("/api/v1/companies/resolve", {
    method: "POST",
    body: { discovery_run_id: discoveryRunId },
  });
  return api.map(fromApiResolution);
}

export async function getCompany(companyId: string): Promise<CanonicalCompany> {
  const api = await apiFetch<ApiCanonicalCompany>(`/api/v1/companies/${companyId}`);
  return fromApiCompany(api);
}

export async function getCompanyFacts(companyId: string): Promise<CompanyFacts> {
  const api = await apiFetch<ApiCompanyFacts>(`/api/v1/companies/${companyId}/facts`);
  return fromApiFacts(api);
}

/** Runs one COMPANY_ENRICHMENT pass for this company across every
 * registered enrichment provider (mock, Explorium's discovery-time
 * attributes are NOT surfaced here — only a real COMPANY_ENRICHMENT
 * provider's response is, e.g. Unipile's LinkedIn company lookup fallback).
 * Never fails the caller: a provider failure is still a 201 with that
 * captured in provider_outcomes, matching the same contract every other
 * enrichment endpoint in this codebase already follows. */
export async function enrichCompany(companyId: string): Promise<void> {
  await apiFetch(`/api/v1/companies/${companyId}/enrich`, { method: "POST" });
}

/** The company's LinkedIn id, only when the backend has actually enriched
 * one — returns null rather than guessing or fabricating a URL. */
export function findLinkedInId(facts: CompanyFacts): string | null {
  const field = facts.fields.find((f) => f.field === "linkedin_id" && !f.conflict);
  const fact = field?.facts[0];
  return typeof fact?.value === "string" ? fact.value : null;
}

/** Ensures the company has actually been enriched at least once, then reads
 * back its LinkedIn id — without this, GET .../facts would always be empty
 * (enrichment is a separate, real backend call, never implied by discovery
 * alone). A failed enrichment attempt is treated the same as "no LinkedIn
 * available" rather than surfaced as a page-level error, since LinkedIn
 * being unavailable is an honest, expected outcome for many companies. */
export async function tryGetCompanyLinkedInId(companyId: string): Promise<string | null> {
  try {
    await enrichCompany(companyId);
  } catch {
    // Enrichment failing (e.g. no real provider configured, provider
    // outage) is not itself an error for the caller — it just means no
    // LinkedIn id will be found below, same as a company no provider has
    // ever enriched.
  }
  try {
    const facts = await getCompanyFacts(companyId);
    return findLinkedInId(facts);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}
