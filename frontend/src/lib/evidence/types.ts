export type EntityType = "COMPANY" | "PERSON";

/** Matches the backend's SourceType enum (backend/app/schemas/evidence.py). */
export type SourceType =
  | "provider"
  | "official_company_site"
  | "linkedin"
  | "search"
  | "news"
  | "funding"
  | "hiring"
  | "directory"
  | "other";

export type ConfidenceLevel = "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN";

export interface EvidenceRecord {
  id: string;
  entityType: EntityType;
  entityId: string;
  field: string;
  value: unknown;
  sourceProviderId: string | null;
  sourceType: SourceType;
  externalId: string | null;
  retrievedAt: string;
  confidence: ConfidenceLevel;
}

export interface EvidenceImportResult {
  added: EvidenceRecord[];
  skippedDuplicates: number;
}
