import { apiFetch } from "@/lib/api-client";
import type { EntityType, EvidenceImportResult, EvidenceRecord } from "./types";

interface ApiEvidenceRecord {
  id: string;
  entity_type: EntityType;
  entity_id: string;
  field: string;
  value: unknown;
  source_provider_id: string | null;
  source_type: string;
  external_id: string | null;
  retrieved_at: string;
  confidence: string;
}

interface ApiEvidenceImportResult {
  added: ApiEvidenceRecord[];
  skipped_duplicates: number;
}

function fromApiRecord(api: ApiEvidenceRecord): EvidenceRecord {
  return {
    id: api.id,
    entityType: api.entity_type,
    entityId: api.entity_id,
    field: api.field,
    value: api.value,
    sourceProviderId: api.source_provider_id,
    sourceType: api.source_type as EvidenceRecord["sourceType"],
    externalId: api.external_id,
    retrievedAt: api.retrieved_at,
    confidence: api.confidence as EvidenceRecord["confidence"],
  };
}

/** Idempotent: re-importing already-collected data is safe (the backend
 * skips exact duplicates rather than re-adding them). */
export async function importPersonEvidence(personId: string): Promise<EvidenceImportResult> {
  const api = await apiFetch<ApiEvidenceImportResult>("/api/v1/evidence/import", {
    method: "POST",
    body: { entity_type: "PERSON", entity_id: personId },
  });
  return { added: api.added.map(fromApiRecord), skippedDuplicates: api.skipped_duplicates };
}

export async function getPersonEvidence(personId: string): Promise<EvidenceRecord[]> {
  const api = await apiFetch<ApiEvidenceRecord[]>("/api/v1/evidence", {
    query: { entity_type: "PERSON", entity_id: personId },
  });
  return api.map(fromApiRecord);
}

/** Idempotent, same contract as importPersonEvidence above, for COMPANY
 * entities (see collect_company_evidence in backend/app/services/evidence_import.py). */
export async function importCompanyEvidence(companyId: string): Promise<EvidenceImportResult> {
  const api = await apiFetch<ApiEvidenceImportResult>("/api/v1/evidence/import", {
    method: "POST",
    body: { entity_type: "COMPANY", entity_id: companyId },
  });
  return { added: api.added.map(fromApiRecord), skippedDuplicates: api.skipped_duplicates };
}

export async function getCompanyEvidence(companyId: string): Promise<EvidenceRecord[]> {
  const api = await apiFetch<ApiEvidenceRecord[]>("/api/v1/evidence", {
    query: { entity_type: "COMPANY", entity_id: companyId },
  });
  return api.map(fromApiRecord);
}

/** Most-recent observation wins (ties broken by array order, deterministic
 * given the backend already returns rows ordered by retrieved_at). Returns
 * null when the field was never observed — never guessed. */
export function latestValueForField(records: EvidenceRecord[], field: string): unknown | null {
  const matching = records.filter((r) => r.field === field);
  if (matching.length === 0) return null;
  return matching[matching.length - 1].value;
}
