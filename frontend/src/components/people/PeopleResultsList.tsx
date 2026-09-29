"use client";

import { useMemo } from "react";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CompanyPeopleGroup from "./CompanyPeopleGroup";
import PersonSelectionActionBar from "./PersonSelectionActionBar";
import EmptyState from "@/components/ui/EmptyState";
import type { CanonicalCompany } from "@/lib/companies/types";
import { useLeadAgentSession } from "@/lib/leads/sessionContext";
import type { PeoplePipelineResult } from "@/lib/peopleDiscovery/usePeopleDiscoveryPipeline";
import type { RankedLead } from "@/lib/ranking/types";

interface PeopleResultsListProps {
  companies: Record<string, CanonicalCompany>;
  result: PeoplePipelineResult;
  onRetryCompany: (companyId: string) => void;
  onContinue: () => void;
}

export default function PeopleResultsList({ companies, result, onRetryCompany, onContinue }: PeopleResultsListProps) {
  const { selectedPersonIds, togglePersonSelected, setSelectedPersonIds } = useLeadAgentSession();

  const rankedByPersonId = useMemo(() => {
    const map: Record<string, RankedLead> = {};
    for (const lead of result.rankedLeads) {
      if (lead.personId) map[lead.personId] = lead;
    }
    return map;
  }, [result.rankedLeads]);

  const companyIds = Object.keys(result.outcomes);
  const totalPeople = Object.keys(result.people).length;

  const handleSelectAll = (personIds: string[]) => {
    setSelectedPersonIds([...new Set([...selectedPersonIds, ...personIds])]);
  };
  const handleDeselectAll = (personIds: string[]) => {
    setSelectedPersonIds(selectedPersonIds.filter((id) => !personIds.includes(id)));
  };

  if (companyIds.length === 0) {
    return <EmptyState title="No companies to search" description="Go back and select at least one company." />;
  }

  if (totalPeople === 0 && companyIds.every((id) => result.outcomes[id].status === "done")) {
    return (
      <EmptyState
        title="No decision-makers found"
        description="No people matched the ICP's allowed titles at the selected companies."
      />
    );
  }

  return (
    <Stack spacing={4} sx={{ pb: 12 }}>
      <Typography variant="body1" color="text.secondary">
        {totalPeople} {totalPeople === 1 ? "person" : "people"} found across {companyIds.length}{" "}
        {companyIds.length === 1 ? "company" : "companies"} &middot; select decision-makers to enrich
      </Typography>

      {companyIds.map((companyId) => {
        const company = companies[companyId];
        const outcome = result.outcomes[companyId];
        return (
          <CompanyPeopleGroup
            key={companyId}
            companyName={company?.canonicalName ?? "Unknown company"}
            personIds={outcome.personIds}
            people={result.people}
            candidatesByPersonId={result.candidatesByPersonId}
            qualifications={result.qualifications}
            rankedByPersonId={rankedByPersonId}
            selectedPersonIds={selectedPersonIds}
            onTogglePerson={togglePersonSelected}
            onSelectAll={handleSelectAll}
            onDeselectAll={handleDeselectAll}
            failed={outcome.status === "failed"}
            errorMessage={outcome.error}
            onRetry={() => onRetryCompany(companyId)}
          />
        );
      })}

      <PersonSelectionActionBar count={selectedPersonIds.length} onContinue={onContinue} />
    </Stack>
  );
}
