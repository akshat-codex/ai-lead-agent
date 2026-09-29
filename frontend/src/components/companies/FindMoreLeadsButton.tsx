"use client";

import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutlined";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import SearchOffOutlinedIcon from "@mui/icons-material/SearchOffOutlined";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import EmptyState from "@/components/ui/EmptyState";
import ErrorState from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import type { BatchDiscoveryPhase } from "@/lib/companies/useBatchDiscovery";

interface FindMoreLeadsButtonProps {
  phase: BatchDiscoveryPhase;
  resultCount: number;
  targetCount: number | null;
  acceptedCount: number;
  errorCode: string | null;
  onFindMore: () => void;
}

export const ERROR_MESSAGES: Record<string, string> = {
  EXPLORIUM_CREDITS_EXHAUSTED:
    "Your Explorium account has run out of credits — more leads can't be found until credits are added. The companies already found above are unaffected.",
  EXPLORIUM_AUTH_FAILED: "Explorium rejected the configured API key — this needs to be fixed before more leads can be found.",
};

export function countLabel(resultCount: number, acceptedCount: number, targetCount: number | null): string {
  if (targetCount != null) {
    return `${acceptedCount} of ${targetCount} qualified leads found`;
  }
  return `${resultCount} ${resultCount === 1 ? "lead" : "leads"} found`;
}

export default function FindMoreLeadsButton({
  phase,
  resultCount,
  targetCount,
  acceptedCount,
  errorCode,
  onFindMore,
}: FindMoreLeadsButtonProps) {
  if (phase === "target-reached") {
    return (
      <EmptyState
        icon={<CheckCircleOutlineIcon fontSize="small" />}
        title={`Found ${acceptedCount} qualified leads`}
        description={`Your target of ${targetCount} has been reached.`}
      />
    );
  }

  if (phase === "exhausted") {
    return (
      <EmptyState
        icon={<SearchOffOutlinedIcon fontSize="small" />}
        title="No more qualified leads"
        description={
          targetCount != null
            ? `Found ${acceptedCount} of your requested ${targetCount} — the candidate pool has been fully searched.`
            : "The candidate pool for this search has been fully searched."
        }
      />
    );
  }

  if (phase === "round-limit-reached") {
    // An honest, working-as-designed safety stop — never presented as an
    // error or a retry-able failure. Clicking "Add More Leads" again is
    // still offered: it starts a FRESH bounded batch of its own (its own
    // round budget), never resumes past the limit that already applied to
    // this search.
    return (
      <Stack spacing={1.5}>
        <EmptyState
          icon={<InfoOutlinedIcon fontSize="small" />}
          title={`Found ${acceptedCount} qualified lead${acceptedCount === 1 ? "" : "s"} so far`}
          description="This search reached its safety limit on how many rounds it searches at once. Add more leads to continue with a fresh, controlled batch."
          action={
            <Button variant="outlined" onClick={onFindMore}>
              Add More Leads
            </Button>
          }
        />
      </Stack>
    );
  }

  if (phase === "provider-error") {
    return (
      <ErrorState
        message={errorCode && ERROR_MESSAGES[errorCode] ? ERROR_MESSAGES[errorCode] : "Something went wrong while looking for more leads."}
        onRetry={onFindMore}
      />
    );
  }

  if (phase === "hermes-searching") {
    // Explorium is unavailable but a Hermes discovery job is still
    // queued/running for this batch — an honest "still working" state,
    // never presented as a dead-end failure. "Add More Leads" is still
    // offered: clicking it resumes the batch, which checks the Hermes
    // job's status (see backend's _maybe_advance_hermes_job) rather than
    // starting anything new.
    return (
      <Stack
        direction="row"
        spacing={1.5}
        sx={{ alignItems: "center", flexWrap: "wrap", rowGap: 1, p: 1.5, borderRadius: 2, bgcolor: "background.paper", border: "1px solid", borderColor: "divider" }}
      >
        <LoadingState label="Explorium unavailable — Hermes is searching for additional companies." />
        <Button variant="outlined" onClick={onFindMore} size="small">
          Check for results
        </Button>
      </Stack>
    );
  }

  return (
    <Stack direction="row" spacing={2} sx={{ alignItems: "center", flexWrap: "wrap" }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 500 }}>
        {countLabel(resultCount, acceptedCount, targetCount)}
      </Typography>
      {phase === "loading-more" ? (
        <LoadingState label="Finding more leads..." />
      ) : (
        <Button variant="outlined" onClick={onFindMore} sx={{ fontWeight: 600 }}>
          Add More Leads
        </Button>
      )}
    </Stack>
  );
}
