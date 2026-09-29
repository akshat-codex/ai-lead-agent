export interface LeadScore {
  id: string;
  icpId: string;
  companyId: string;
  personId: string | null;
  hardIcpResult: string;
  eligibleForScoring: boolean;
  icpScore: number;
  commercialScore: number;
  evidenceScore: number;
  finalScore: number | null;
  reasonCodes: string[];
  explanation: string;
}
