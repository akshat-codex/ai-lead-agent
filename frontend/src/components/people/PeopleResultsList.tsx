"use client";

import { useMemo } from "react";
import GroupsOutlinedIcon from "@mui/icons-material/GroupsOutlined";
import PersonSearchOutlinedIcon from "@mui/icons-material/PersonSearchOutlined";
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
    return (
      <EmptyState
        icon={<PersonSearchOutlinedIcon fontSize="small" />}
        title="No companies to search"
        description="Go back and select at least one company."
      />
    );
  }

  if (totalPeople === 0 && companyIds.every((id) => result.outcomes[id].status === "done")) {
    return (
      <EmptyState
        icon={<PersonSearchOutlinedIcon fontSize="small" />}
        title="No decision-makers found"
        description="No people matched the ICP's allowed titles at the selected companies."
      />
    );
  }

  return (
    <Stack spacing={4} sx={{ pb: 12 }}>
      <Stack
        direction="row"
        spacing={1}
        sx={{
          alignItems: "center",
          px: 2,
          py: 1.5,
          borderRadius: 2,
          bgcolor: "background.paper",
          border: "1px solid",
          borderColor: "divider",
        }}
      >
        <GroupsOutlinedIcon fontSize="small" sx={{ color: "text.secondary" }} />
        <Typography variant="body2" color="text.secondary">
          <Typography component="span" variant="body2" sx={{ fontWeight: 700, color: "text.primary" }}>
            {totalPeople} {totalPeople === 1 ? "person" : "people"} found
          </Typography>
          {" "}across {companyIds.length} {companyIds.length === 1 ? "company" : "companies"} &middot; select decision-makers to enrich
        </Typography>
      </Stack>

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
