export interface CompanyResolution {
  id: string;
  candidateId: string;
  discoveryRunId: string;
  status: "MATCH" | "NEW" | "UNRESOLVED";
  canonicalCompanyId: string | null;
  reasonCode: string;
  explanation: string;
}

export interface CanonicalCompany {
  id: string;
  canonicalName: string;
  canonicalDomain: string | null;
  aliases: string[];
  providerIdentities: Record<string, string>;
}

export interface EnrichmentFact {
  field: string;
  value: unknown;
  providerId: string;
  confidence: number | null;
}

export interface EnrichmentField {
  field: string;
  facts: EnrichmentFact[];
  conflict: boolean;
}

export interface CompanyFacts {
  companyId: string;
  fields: EnrichmentField[];
}
