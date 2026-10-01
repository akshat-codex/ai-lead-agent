export interface ExportIdentity {
  companyId: string;
  companyName: string | null;
  companyDomain: string | null;
  personId: string | null;
  personName: string | null;
  personLinkedinId: string | null;
}

export interface ExportEvidenceSummary {
  /** Only fields whose Phase 11 status is SUPPORTED — see backend/app/
   * schemas/export.py::ExportEvidenceSummary's own docstring. Never
   * includes a conflicting or unconfirmed value. */
  verifiedFields: Record<string, string>;
  /** Fields where independent sources disagree (e.g. two providers
   * reporting a different industry) — an honest signal worth surfacing,
   * never silently resolved one way or the other. */
  conflictingFields: string[];
  /** Critical fields (industry, employee_range, country, ...) with no
   * evidence at all yet. */
  missingCriticalFields: string[];
}

export interface ExportScores {
  finalScore: number | null;
  icpScore: number | null;
  commercialScore: number | null;
  /** General evidence quality/coverage — distinct from identityConfidence
   * below (see backend/app/services/lead_scoring.py's own module
   * docstring: six independently-inspectable components, never one
   * opaque score). */
  evidenceScore: number | null;
  /** How recent the newest evidence is; null when there is no evidence to
   * date at all (never a fabricated number). */
  freshnessScore: number | null;
  /** How confident the system is that this (company, person) pair is
   * genuinely the same real-world entity across providers — reused
   * directly from entity-resolution history, never re-derived. This is
   * the real "confidence %" signal, distinct from finalScore's overall
   * fit judgment. */
  identityConfidence: number | null;
}

export interface ExportedLead {
  leadId: string;
  identity: ExportIdentity;
  evidence: ExportEvidenceSummary;
  hardRuleResult: string | null;
  /** e.g. "HARD_RULE_PASS", "QUALIFICATION_GOOD_FIT" — backend
   * RankingReasonCode values (backend/app/schemas/ranking.py), the real
   * machine-readable "why" behind rank/tier. */
  rankingReasonCodes: string[];
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
