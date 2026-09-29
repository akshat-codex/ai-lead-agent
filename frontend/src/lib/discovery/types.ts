export interface CandidateCompany {
  id: string;
  icpId: string;
  providerId: string;
  externalId: string;
  name: string;
  domain: string | null;
  attributes: Record<string, unknown>;
  discoveredAt: string;
}

export interface DiscoveryRun {
  id: string;
  icpId: string;
  status: string;
  requestedLimit: number;
  totalReturned: number;
  candidates: CandidateCompany[];
}
