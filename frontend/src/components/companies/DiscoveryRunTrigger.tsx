"use client";

import SearchIcon from "@mui/icons-material/Search";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import ErrorState from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import type { PipelinePhase } from "@/lib/companies/useCompanyDiscoveryPipeline";

interface DiscoveryRunTriggerProps {
  phase: PipelinePhase;
  statusLabel: string;
  error: string | null;
  onRun: () => void;
}

export default function DiscoveryRunTrigger({ phase, statusLabel, error, onRun }: DiscoveryRunTriggerProps) {
  const isRunning = phase !== "idle" && phase !== "done" && phase !== "error";

  if (isRunning) {
    return <LoadingState label={statusLabel} />;
  }

  if (phase === "error" && error) {
    return <ErrorState message={error} onRetry={onRun} />;
  }

  if (phase === "idle") {
    return (
      <Stack spacing={1.5}>
        <Typography variant="body2" color="text.secondary">
          Ready to search for companies matching your ICP.
        </Typography>
        <Stack direction="row">
          <Button variant="contained" startIcon={<SearchIcon />} onClick={onRun}>
            Find companies
          </Button>
        </Stack>
      </Stack>
    );
  }

  return null;
}
