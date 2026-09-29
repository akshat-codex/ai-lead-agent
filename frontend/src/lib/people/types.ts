export interface PersonResolution {
  id: string;
  candidateId: string;
  peopleDiscoveryRunId: string;
  status: "MATCH" | "NEW" | "UNRESOLVED";
  canonicalPersonId: string | null;
  reasonCode: string;
  explanation: string;
}

export interface CanonicalPerson {
  id: string;
  canonicalName: string;
  canonicalCompanyId: string | null;
  /** A normalized LinkedIn identifier, only when a provider has actually
   * supplied one — never fabricated. Absent for every mock-provider result
   * today (the mock people provider returns no LinkedIn field at all). */
  linkedinId: string | null;
}
