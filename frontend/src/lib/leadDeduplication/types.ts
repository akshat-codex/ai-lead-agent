export interface LeadDeduplicationResult {
  id: string;
  icpId: string;
  companyId: string | null;
  personId: string | null;
  /** MATCHED_EXISTING_LEAD | NEW_LEAD | UNRESOLVED */
  decision: string;
  leadId: string | null;
  reasonCode: string;
  explanation: string;
}
