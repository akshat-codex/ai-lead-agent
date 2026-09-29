"use client";

import { useState } from "react";
import SearchIcon from "@mui/icons-material/Search";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ErrorState from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import type { DiscoveryMode } from "@/lib/batch/api";
import { DEFAULT_TARGET_COUNT, type BatchDiscoveryPhase } from "@/lib/companies/useBatchDiscovery";

interface BatchDiscoveryTriggerProps {
  phase: BatchDiscoveryPhase;
  error: string | null;
  onRun: (targetCount?: number, discoveryMode?: DiscoveryMode) => void;
}

const DISCOVERY_MODE_OPTIONS: { value: DiscoveryMode; label: string; description: string }[] = [
  {
    value: "fast",
    label: "Fast",
    description: "Explorium only — structured company database search. Precise, but limited to industries Explorium's own taxonomy covers.",
  },
  {
    value: "safe",
    label: "Safe",
    description: "Web search only (Tavily + Serper), Explorium skipped — for industries Explorium's taxonomy can't resolve at all (e.g. niche or emerging categories).",
  },
  {
    value: "hard",
    label: "Hard",
    description: "Every configured source runs together (Explorium + Tavily + Serper) for the widest possible pool. Uses more provider credits per search.",
  },
];

// Every initial search is one controlled batch of at most this many
// companies — never an open-ended request. The user's own typed target
// count is respected exactly (never silently clamped down to a smaller
// default), up to this ceiling; getting more than this many requires the
// explicit, separate "Add More Leads" action (see FindMoreLeadsButton.tsx).
export const MAX_INITIAL_TARGET_COUNT = 50;

/** The idle-state trigger for the batch-pipeline-backed Companies page.
 * Reuses the exact LoadingState/ErrorState primitives DiscoveryRunTrigger
 * already used — this is a sibling for useBatchDiscovery's differently-
 * shaped phase enum, not a redesign of that component (which is left in
 * place, unmodified, for the older pipeline). The only new UI surface is
 * one optional "how many leads" number field next to the existing-style
 * "Find companies" button. */
export default function BatchDiscoveryTrigger({ phase, error, onRun }: BatchDiscoveryTriggerProps) {
  const [targetCountInput, setTargetCountInput] = useState("");
  const [discoveryMode, setDiscoveryMode] = useState<DiscoveryMode>("fast");

  const isRunning = phase === "first-page";

  if (isRunning) {
    return <LoadingState label="Finding companies..." />;
  }

  if (phase === "error" && error) {
    return <ErrorState message={error} onRetry={() => onRun(parseTargetCount(targetCountInput), discoveryMode)} />;
  }

  if (phase !== "idle") {
    return null;
  }

  return (
    <Paper
      variant="outlined"
      sx={{ p: 2.5, borderRadius: 2.5, bgcolor: "background.paper" }}
    >
      <Stack spacing={2}>
        <Typography variant="body2" color="text.secondary">
          Ready to search for companies matching your ICP. Each search finds up to {MAX_INITIAL_TARGET_COUNT}
          {" "}companies — use &quot;Add More Leads&quot; afterward for more.
        </Typography>
        <Stack direction="row" spacing={2} sx={{ alignItems: "center", flexWrap: "wrap" }}>
          <Button
            variant="contained"
            startIcon={<SearchIcon />}
            onClick={() => onRun(parseTargetCount(targetCountInput), discoveryMode)}
            disableElevation
            size="large"
          >
            Find companies
          </Button>
          <TextField
            type="number"
            size="small"
            label={`Target count (up to ${MAX_INITIAL_TARGET_COUNT})`}
            placeholder={`e.g. ${MAX_INITIAL_TARGET_COUNT}`}
            value={targetCountInput}
            onChange={(e) => setTargetCountInput(e.target.value)}
            slotProps={{ htmlInput: { min: 1, max: MAX_INITIAL_TARGET_COUNT } }}
            sx={{ width: 180 }}
          />
          <ToggleButtonGroup
            size="small"
            exclusive
            value={discoveryMode}
            onChange={(_e, value: DiscoveryMode | null) => {
              if (value) setDiscoveryMode(value);
            }}
            sx={{
              "& .MuiToggleButton-root": {
                textTransform: "none",
                fontWeight: 600,
                px: 1.75,
                borderColor: "divider",
              },
            }}
          >
            {DISCOVERY_MODE_OPTIONS.map((option) => (
              <ToggleButton key={option.value} value={option.value}>
                <Tooltip title={option.description}>
                  <span>{option.label}</span>
                </Tooltip>
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
        </Stack>
      </Stack>
    </Paper>
  );
}

export function parseTargetCount(raw: string): number | undefined {
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return undefined;
  // Clamp, never reject — a user who types a larger number still gets a
  // safe, controlled batch rather than an error or a silently-ignored
  // input; "Add More Leads" is the sanctioned path beyond this cap.
  return Math.min(parsed, MAX_INITIAL_TARGET_COUNT);
}
