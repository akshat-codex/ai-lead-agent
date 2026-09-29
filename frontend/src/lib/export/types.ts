export interface ExportIdentity {
  companyId: string;
  companyName: string | null;
  companyDomain: string | null;
  personId: string | null;
  personName: string | null;
  personLinkedinId: string | null;
}

export interface ExportScores {
  finalScore: number | null;
  icpScore: number | null;
  commercialScore: number | null;
}

export interface ExportedLead {
  leadId: string;
  identity: ExportIdentity;
  hardRuleResult: string | null;
  scores: ExportScores;
  qualificationDecision: string | null;
  qualificationSummary: string | null;
  rank: number | null;
  /** Matches backend RankTier (backend/app/schemas/ranking.py). */
  tier: string | null;
}

export interface ExportResult {
  icpId: string;
  leadCount: number;
  leads: ExportedLead[];
}
