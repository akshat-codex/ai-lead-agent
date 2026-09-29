export interface LeadQualification {
  id: string;
  icpId: string;
  companyId: string;
  personId: string | null;
  hardRuleResult: string;
  /** SUCCESS | HARD_REJECTED | HARD_HOLD | PROVIDER_ERROR | PROVIDER_TIMEOUT |
   * MALFORMED_OUTPUT | SCHEMA_INVALID | INVALID_EVIDENCE_IDS | EMPTY_RESPONSE */
  status: string;
  /** GOOD_FIT | WEAK_FIT | NOT_FIT | HOLD | REJECT | null */
  decision: string | null;
  confidence: number | null;
  reasonCodes: string[];
  summary: string;
  supportingEvidenceIds: string[];
  riskEvidenceIds: string[];
  commercialFitExplanation: string;
  errorMessage: string | null;
}
