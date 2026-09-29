import { apiFetch } from "@/lib/api-client";
import type { ExportedLead, ExportResult } from "./types";

interface ApiExportIdentity {
  company_id: string;
  company_name: string | null;
  company_domain: string | null;
  person_id: string | null;
  person_name: string | null;
  person_linkedin_id: string | null;
}

interface ApiExportScores {
  final_score: number | null;
  icp_score: number | null;
  commercial_score: number | null;
}

interface ApiExportedLead {
  lead_id: string;
  identity: ApiExportIdentity;
  hard_rule_result: string | null;
  scores: ApiExportScores;
  qualification_decision: string | null;
  qualification_summary: string | null;
  rank: number | null;
  tier: string | null;
}

interface ApiExportResult {
  metadata: { icp_id: string; lead_count: number };
  leads: ApiExportedLead[];
}

function fromApiLead(api: ApiExportedLead): ExportedLead {
  return {
    leadId: api.lead_id,
    identity: {
      companyId: api.identity.company_id,
      companyName: api.identity.company_name,
      companyDomain: api.identity.company_domain,
      personId: api.identity.person_id,
      personName: api.identity.person_name,
      personLinkedinId: api.identity.person_linkedin_id,
    },
    hardRuleResult: api.hard_rule_result,
    scores: {
      finalScore: api.scores.final_score,
      icpScore: api.scores.icp_score,
      commercialScore: api.scores.commercial_score,
    },
    qualificationDecision: api.qualification_decision,
    qualificationSummary: api.qualification_summary,
    rank: api.rank,
    tier: api.tier,
  };
}

/** Reuses the existing, already-comprehensive export endpoint (Phase 23) —
 * this is the same server-side assembly (ranking + score + qualification)
 * that powers the backend's own CSV/JSON export, not a re-derivation. */
export async function getExport(icpId: string, batchId?: string): Promise<ExportResult> {
  const api = await apiFetch<ApiExportResult>("/api/v1/exports", {
    query: { icp_id: icpId, batch_id: batchId, format: "JSON" },
  });
  return {
    icpId: api.metadata.icp_id,
    leadCount: api.metadata.lead_count,
    leads: api.leads.map(fromApiLead),
  };
}
