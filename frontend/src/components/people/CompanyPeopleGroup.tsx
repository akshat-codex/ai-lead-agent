"use client";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { motion } from "framer-motion";
import PersonCard from "./PersonCard";
import EmptyState from "@/components/ui/EmptyState";
import { staggerContainer } from "@/components/ui/FadeIn";
import type { CanonicalPerson } from "@/lib/people/types";
import type { CandidatePerson } from "@/lib/peopleDiscovery/types";
import type { LeadQualification } from "@/lib/qualification/types";
import type { RankedLead } from "@/lib/ranking/types";

interface CompanyPeopleGroupProps {
  companyName: string;
  personIds: string[];
  people: Record<string, CanonicalPerson>;
  candidatesByPersonId: Record<string, CandidatePerson>;
  qualifications: Record<string, LeadQualification>;
  rankedByPersonId: Record<string, RankedLead>;
  selectedPersonIds: string[];
  onTogglePerson: (personId: string) => void;
  onSelectAll: (personIds: string[]) => void;
  onDeselectAll: (personIds: string[]) => void;
  failed: boolean;
  errorMessage?: string;
  onRetry: () => void;
}

export default function CompanyPeopleGroup({
  companyName,
  personIds,
  people,
  candidatesByPersonId,
  qualifications,
  rankedByPersonId,
  selectedPersonIds,
  onTogglePerson,
  onSelectAll,
  onDeselectAll,
  failed,
  errorMessage,
  onRetry,
}: CompanyPeopleGroupProps) {
  const allSelected = personIds.length > 0 && personIds.every((id) => selectedPersonIds.includes(id));

  return (
    <Stack spacing={1.75} component={Paper} variant="outlined" sx={{ p: 2.5, borderRadius: 2.5, bgcolor: "background.default" }}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", rowGap: 1 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {companyName}
          </Typography>
          {personIds.length > 0 && (
            <Chip
              size="small"
              variant="outlined"
              label={`${personIds.length} ${personIds.length === 1 ? "person" : "people"}`}
              sx={{ borderColor: "divider", bgcolor: "background.paper" }}
            />
          )}
        </Stack>
        {personIds.length > 0 && (
          <Button
            size="small"
            onClick={() => (allSelected ? onDeselectAll(personIds) : onSelectAll(personIds))}
          >
            {allSelected ? "Deselect all" : "Select all"}
          </Button>
        )}
      </Stack>

      {failed && (
        <Alert
          severity="error"
          action={
            <Button color="inherit" size="small" onClick={onRetry}>
              Retry
            </Button>
          }
        >
          {errorMessage ?? "Could not find decision-makers for this company."}
        </Alert>
      )}

      {!failed && personIds.length === 0 && (
        <EmptyState title="No decision-makers found" description="No people matched this company for the ICP's allowed titles." />
      )}

      {personIds.length > 0 && (
        <Stack component={motion.div} initial="hidden" animate="show" variants={staggerContainer} spacing={1.75}>
          {personIds.map((personId) => {
            const person = people[personId];
            if (!person) return null;
            const candidate = candidatesByPersonId[personId];
            const qualification = qualifications[personId];
            const ranked = rankedByPersonId[personId] ?? null;
            return (
              <PersonCard
                key={personId}
                name={person.canonicalName}
                title={candidate?.title ?? null}
                ranked={ranked}
                qualificationSummary={qualification?.summary || null}
                qualificationExplanation={qualification?.commercialFitExplanation || null}
                linkedinId={person.linkedinId}
                isEvaluationPending={!qualification}
                selected={selectedPersonIds.includes(personId)}
                onToggleSelected={() => onTogglePerson(personId)}
              />
            );
          })}
        </Stack>
      )}
    </Stack>
  );
}
