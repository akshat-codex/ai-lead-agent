export interface CandidatePerson {
  id: string;
  companyId: string;
  icpId: string;
  providerId: string;
  externalId: string;
  name: string;
  /** As reported by the discovery provider — not independently verified. */
  title: string | null;
  attributes: Record<string, unknown>;
  discoveredAt: string;
}

export interface PeopleDiscoveryRun {
  id: string;
  icpId: string;
  companyId: string;
  status: string;
  requestedLimit: number;
  totalReturned: number;
  candidates: CandidatePerson[];
}
