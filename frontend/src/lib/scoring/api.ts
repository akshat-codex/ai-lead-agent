import { apiFetch } from "@/lib/api-client";
import type { LeadScore } from "./types";

interface ApiLeadScore {
  id: string;
  icp_id: string;
  company_id: string;
  person_id: string | null;
  hard_icp_result: string;
  eligible_for_scoring: boolean;
  icp_score: number;
  commercial_score: number;
  evidence_score: number;
  final_score: number | null;
  reason_codes: string[];
  explanation: string;
}

function fromApiLeadScore(api: ApiLeadScore): LeadScore {
  return {
    id: api.id,
    icpId: api.icp_id,
    companyId: api.company_id,
    personId: api.person_id,
    hardIcpResult: api.hard_icp_result,
    eligibleForScoring: api.eligible_for_scoring,
    icpScore: api.icp_score,
    commercialScore: api.commercial_score,
    evidenceScore: api.evidence_score,
    finalScore: api.final_score,
    reasonCodes: api.reason_codes,
    explanation: api.explanation,
  };
}

export async function createLeadScore(icpId: string, companyId: string, personId?: string): Promise<LeadScore> {
  const api = await apiFetch<ApiLeadScore>("/api/v1/lead-scores", {
    method: "POST",
    body: { icp_id: icpId, company_id: companyId, person_id: personId ?? null },
  });
  return fromApiLeadScore(api);
}
