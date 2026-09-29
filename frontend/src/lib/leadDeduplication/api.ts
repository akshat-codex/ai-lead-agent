import { apiFetch } from "@/lib/api-client";
import type { LeadDeduplicationResult } from "./types";

interface ApiLeadDeduplication {
  id: string;
  icp_id: string;
  company_id: string | null;
  person_id: string | null;
  decision: string;
  lead_id: string | null;
  reason_code: string;
  explanation: string;
}

function fromApiDeduplication(api: ApiLeadDeduplication): LeadDeduplicationResult {
  return {
    id: api.id,
    icpId: api.icp_id,
    companyId: api.company_id,
    personId: api.person_id,
    decision: api.decision,
    leadId: api.lead_id,
    reasonCode: api.reason_code,
    explanation: api.explanation,
  };
}

export async function createLeadDeduplication(
  icpId: string,
  options: { companyId?: string; personId?: string },
): Promise<LeadDeduplicationResult> {
  const api = await apiFetch<ApiLeadDeduplication>("/api/v1/lead-deduplications", {
    method: "POST",
    body: {
      icp_id: icpId,
      company_id: options.companyId ?? null,
      person_id: options.personId ?? null,
    },
  });
  return fromApiDeduplication(api);
}
