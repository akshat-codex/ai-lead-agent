"use client";

import SearchIcon from "@mui/icons-material/Search";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
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
      <Paper variant="outlined" sx={{ p: { xs: 2.5, sm: 3 }, borderRadius: 2.5, bgcolor: "background.paper" }}>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ alignItems: { xs: "flex-start", sm: "center" }, justifyContent: "space-between" }}>
          <Typography variant="body2" color="text.secondary">
            Ready to find decision-makers at {companyCount} selected {companyCount === 1 ? "company" : "companies"}.
          </Typography>
          <Button variant="contained" startIcon={<SearchIcon />} onClick={onRun} size="large">
            Find decision-makers
          </Button>
        </Stack>
      </Paper>
    );
  }

  return null;
}
