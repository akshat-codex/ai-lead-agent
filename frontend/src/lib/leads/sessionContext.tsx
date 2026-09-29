"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";

interface LeadAgentSession {
  icpId: string | null;
  discoveryRunId: string | null;
  selectedCompanyIds: string[];
  /** company id -> its people-discovery run id (people-discovery is per-company on this backend). */
  peopleDiscoveryRunIds: Record<string, string>;
  selectedPersonIds: string[];
  batchId: string | null;
}

interface LeadAgentSessionContextValue extends LeadAgentSession {
  setIcpId: (id: string) => void;
  setDiscoveryRunId: (id: string) => void;
  setBatchId: (id: string) => void;
  toggleCompanySelected: (companyId: string) => void;
  setSelectedCompanyIds: (ids: string[]) => void;
  setPeopleDiscoveryRunId: (companyId: string, runId: string) => void;
  togglePersonSelected: (personId: string) => void;
  setSelectedPersonIds: (ids: string[]) => void;
  reset: () => void;
}

const initialSession: LeadAgentSession = {
  icpId: null,
  discoveryRunId: null,
  selectedCompanyIds: [],
  peopleDiscoveryRunIds: {},
  selectedPersonIds: [],
  batchId: null,
};

const LeadAgentSessionContext = createContext<LeadAgentSessionContextValue | null>(null);

export function LeadAgentSessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<LeadAgentSession>(initialSession);

  const setIcpId = useCallback((id: string) => {
    setSession((prev) => (prev.icpId === id ? prev : { ...prev, icpId: id }));
  }, []);

  const setDiscoveryRunId = useCallback((id: string) => {
    setSession((prev) => ({ ...prev, discoveryRunId: id }));
  }, []);

  const setBatchId = useCallback((id: string) => {
    setSession((prev) => ({ ...prev, batchId: id }));
  }, []);

  const toggleCompanySelected = useCallback((companyId: string) => {
    setSession((prev) => ({
      ...prev,
      selectedCompanyIds: prev.selectedCompanyIds.includes(companyId)
        ? prev.selectedCompanyIds.filter((id) => id !== companyId)
        : [...prev.selectedCompanyIds, companyId],
    }));
  }, []);

  const setSelectedCompanyIds = useCallback((ids: string[]) => {
    setSession((prev) => ({ ...prev, selectedCompanyIds: ids }));
  }, []);

  const setPeopleDiscoveryRunId = useCallback((companyId: string, runId: string) => {
    setSession((prev) => ({
      ...prev,
      peopleDiscoveryRunIds: { ...prev.peopleDiscoveryRunIds, [companyId]: runId },
    }));
  }, []);

  const togglePersonSelected = useCallback((personId: string) => {
    setSession((prev) => ({
      ...prev,
      selectedPersonIds: prev.selectedPersonIds.includes(personId)
        ? prev.selectedPersonIds.filter((id) => id !== personId)
        : [...prev.selectedPersonIds, personId],
    }));
  }, []);

  const setSelectedPersonIds = useCallback((ids: string[]) => {
    setSession((prev) => ({ ...prev, selectedPersonIds: ids }));
  }, []);

  const reset = useCallback(() => setSession(initialSession), []);

  const value = useMemo<LeadAgentSessionContextValue>(
    () => ({
      ...session,
      setIcpId,
      setDiscoveryRunId,
      setBatchId,
      toggleCompanySelected,
      setSelectedCompanyIds,
      setPeopleDiscoveryRunId,
      togglePersonSelected,
      setSelectedPersonIds,
      reset,
    }),
    [
      session,
      setIcpId,
      setDiscoveryRunId,
      setBatchId,
      toggleCompanySelected,
      setSelectedCompanyIds,
      setPeopleDiscoveryRunId,
      togglePersonSelected,
      setSelectedPersonIds,
      reset,
    ],
  );

  return <LeadAgentSessionContext.Provider value={value}>{children}</LeadAgentSessionContext.Provider>;
}

export function useLeadAgentSession(): LeadAgentSessionContextValue {
  const ctx = useContext(LeadAgentSessionContext);
  if (!ctx) {
    throw new Error("useLeadAgentSession must be used within a LeadAgentSessionProvider");
  }
  return ctx;
}
