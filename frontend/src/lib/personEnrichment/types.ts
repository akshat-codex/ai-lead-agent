export type PersonEnrichmentRunStatus = "COMPLETED" | "PARTIAL_FAILURE" | "FAILED" | "UNAVAILABLE";

export interface PersonEnrichmentRun {
  id: string;
  personId: string;
  status: PersonEnrichmentRunStatus;
  providerId: string | null;
  errorMessage: string | null;
}
