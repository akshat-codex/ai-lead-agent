"use client";

import SearchIcon from "@mui/icons-material/Search";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import ErrorState from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import type { PeoplePipelinePhase } from "@/lib/peopleDiscovery/usePeopleDiscoveryPipeline";

interface PeopleDiscoveryTriggerProps {
  phase: PeoplePipelinePhase;
  statusLabel: string;
  error: string | null;
  companyCount: number;
  onRun: () => void;
}

export default function PeopleDiscoveryTrigger({
  phase,
  statusLabel,
  error,
  companyCount,
  onRun,
}: PeopleDiscoveryTriggerProps) {
  if (phase === "running") {
    return <LoadingState label={statusLabel} />;
  }

  if (phase === "error" && error) {
    return <ErrorState message={error} onRetry={onRun} />;
  }

  if (phase === "idle") {
    return (
      <Stack spacing={1.5}>
        <Typography variant="body2" color="text.secondary">
          Ready to find decision-makers at {companyCount} selected {companyCount === 1 ? "company" : "companies"}.
        </Typography>
        <Stack direction="row">
          <Button variant="contained" startIcon={<SearchIcon />} onClick={onRun}>
            Find decision-makers
          </Button>
        </Stack>
      </Stack>
    );
  }

  return null;
}
