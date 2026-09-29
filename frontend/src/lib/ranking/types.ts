import type { RankTier } from "@/components/ui/FitBadge";

export interface RankedLeadSignals {
  hardRuleResult: string | null;
  finalScore: number | null;
  icpScore: number | null;
  commercialScore: number | null;
  evidenceScore: number | null;
  qualificationDecision: string | null;
  qualificationConfidence: number | null;
}

export interface RankedLead {
  rank: number;
  leadId: string;
  companyId: string;
  personId: string | null;
  tier: RankTier;
  reasonCodes: string[];
  signals: RankedLeadSignals;
  explanation: string;
}

export interface RankingResult {
  icpId: string;
  batchId: string | null;
  rankedLeads: RankedLead[];
}
