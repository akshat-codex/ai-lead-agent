import { apiFetch } from "@/lib/api-client";
import type { LeadQualification } from "./types";

interface ApiLeadQualification {
  id: string;
  icp_id: string;
  company_id: string;
  person_id: string | null;
  hard_rule_result: string;
  status: string;
  decision: string | null;
  confidence: number | null;
  reason_codes: string[];
  summary: string;
  supporting_evidence_ids: string[];
  risk_evidence_ids: string[];
  commercial_fit_explanation: string;
  error_message: string | null;
}

function fromApiQualification(api: ApiLeadQualification): LeadQualification {
  return {
    id: api.id,
    icpId: api.icp_id,
    companyId: api.company_id,
    personId: api.person_id,
    hardRuleResult: api.hard_rule_result,
    status: api.status,
    decision: api.decision,
    confidence: api.confidence,
    reasonCodes: api.reason_codes,
    summary: api.summary,
    supportingEvidenceIds: api.supporting_evidence_ids,
    riskEvidenceIds: api.risk_evidence_ids,
    commercialFitExplanation: api.commercial_fit_explanation,
    errorMessage: api.error_message,
  };
}

export async function createLeadQualification(
  icpId: string,
  companyId: string,
  personId?: string,
): Promise<LeadQualification> {
  const api = await apiFetch<ApiLeadQualification>("/api/v1/lead-qualifications", {
    method: "POST",
    body: { icp_id: icpId, company_id: companyId, person_id: personId ?? null },
  });
  return fromApiQualification(api);
}

/** Reads back the most recent qualification already on record for this
 * company (e.g. one produced by the batch pipeline), without triggering a
 * new LLM qualification call. Returns null when none exists yet. */
export async function getLatestLeadQualification(icpId: string, companyId: string): Promise<LeadQualification | null> {
  const results = await apiFetch<ApiLeadQualification[]>("/api/v1/lead-qualifications", {
    query: { icp_id: icpId, company_id: companyId },
  });
  if (results.length === 0) return null;
  return fromApiQualification(results[results.length - 1]);
}
