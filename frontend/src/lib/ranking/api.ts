import { apiFetch } from "@/lib/api-client";
import type { RankTier } from "@/components/ui/FitBadge";
import type { RankedLead, RankingResult } from "./types";

interface ApiRankedLeadSignals {
  hard_rule_result: string | null;
  final_score: number | null;
  icp_score: number | null;
  commercial_score: number | null;
  evidence_score: number | null;
  qualification_decision: string | null;
  qualification_confidence: number | null;
}

interface ApiRankedLead {
  rank: number;
  lead_id: string;
  company_id: string;
  person_id: string | null;
  tier: RankTier;
  reason_codes: string[];
  signals: ApiRankedLeadSignals;
  explanation: string;
}

interface ApiRankingResult {
  icp_id: string;
  batch_id: string | null;
  ranked_leads: ApiRankedLead[];
}

function fromApiRankedLead(api: ApiRankedLead): RankedLead {
  return {
    rank: api.rank,
    leadId: api.lead_id,
    companyId: api.company_id,
    personId: api.person_id,
    tier: api.tier,
    reasonCodes: api.reason_codes,
    signals: {
      hardRuleResult: api.signals.hard_rule_result,
      finalScore: api.signals.final_score,
      icpScore: api.signals.icp_score,
      commercialScore: api.signals.commercial_score,
      evidenceScore: api.signals.evidence_score,
      qualificationDecision: api.signals.qualification_decision,
      qualificationConfidence: api.signals.qualification_confidence,
    },
    explanation: api.explanation,
  };
}

export async function getRanking(icpId: string, batchId?: string): Promise<RankingResult> {
  const api = await apiFetch<ApiRankingResult>("/api/v1/rankings", {
    query: { icp_id: icpId, batch_id: batchId },
  });
  return {
    icpId: api.icp_id,
    batchId: api.batch_id,
    rankedLeads: api.ranked_leads.map(fromApiRankedLead),
  };
}
