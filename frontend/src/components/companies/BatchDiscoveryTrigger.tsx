"use client";

import { useState } from "react";
import SearchIcon from "@mui/icons-material/Search";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useQuery } from "@tanstack/react-query";
import ErrorState from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import type { DiscoveryMode } from "@/lib/batch/api";
import { DEFAULT_TARGET_COUNT, type BatchDiscoveryPhase } from "@/lib/companies/useBatchDiscovery";
import { listDiscoveryProviders } from "@/lib/discoveryProviders/api";
import type { DiscoveryProviderStatus } from "@/lib/discoveryProviders/api";

interface BatchDiscoveryTriggerProps {
  phase: BatchDiscoveryPhase;
  error: string | null;
  onRun: (targetCount?: number, discoveryMode?: DiscoveryMode) => void;
}

// Which provider_ids each mode actually calls — mirrors backend/app/
// services/batch_orchestration.py::_providers_allowed_for_mode exactly
// (fast = Explorium only; safe = Tavily+Serper only; hard = every
// registered COMPANY_DISCOVERY provider, including Hermes once
// configured). Used only to compute a live "N of M configured" summary —
// never a fabricated accuracy number, just a real count of what's
// actually active for that mode right now.
const MODE_PROVIDER_IDS: Record<DiscoveryMode, string[]> = {
  fast: ["explorium-company-discovery-v1"],
  safe: ["tavily-company-discovery-v1", "serper-company-discovery-v1"],
  hard: ["explorium-company-discovery-v1", "tavily-company-discovery-v1", "serper-company-discovery-v1", "hermes-icp-search-v1"],
};

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
    description: "Every configured source runs together (Explorium + Tavily + Serper + Hermes, whichever you've configured) for the widest possible pool. Uses more provider credits per search.",
  },
];

function configuredCountForMode(mode: DiscoveryMode, providers: DiscoveryProviderStatus[]): { configured: number; total: number } {
  const relevantIds = new Set(MODE_PROVIDER_IDS[mode]);
  const relevant = providers.filter((p) => relevantIds.has(p.providerId));
  return { configured: relevant.filter((p) => p.configured).length, total: relevant.length };
}

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

  // Real, live provider configuration status — never a fabricated
  // accuracy score (see backend/app/api/discovery_providers.py's own
  // module docstring for why no such number exists to show). Silently
  // degrades to "no status shown" on failure rather than blocking the
  // page — this is a helpful hint, never a requirement to run a search.
  const providersQuery = useQuery({ queryKey: ["discovery-providers"], queryFn: listDiscoveryProviders, retry: false });

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
      sx={{ p: { xs: 2.5, sm: 3 }, borderRadius: 2.5, bgcolor: "background.paper" }}
    >
      <Stack spacing={2.5}>
        <Stack spacing={0.5}>
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            Search for matching companies
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Each search finds up to {MAX_INITIAL_TARGET_COUNT} companies — use &quot;Add More Leads&quot;
            {" "}afterward for more.
          </Typography>
        </Stack>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ alignItems: { xs: "stretch", sm: "flex-end" }, flexWrap: "wrap" }}>
          <TextField
            type="number"
            label={`Target count (up to ${MAX_INITIAL_TARGET_COUNT})`}
            placeholder={`e.g. ${MAX_INITIAL_TARGET_COUNT}`}
            value={targetCountInput}
            onChange={(e) => setTargetCountInput(e.target.value)}
            slotProps={{ htmlInput: { min: 1, max: MAX_INITIAL_TARGET_COUNT } }}
            sx={{ width: { xs: "100%", sm: 200 } }}
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
          <Button
            variant="contained"
            startIcon={<SearchIcon />}
            onClick={() => onRun(parseTargetCount(targetCountInput), discoveryMode)}
            size="large"
            sx={{ ml: { sm: "auto" } }}
          >
            Find companies
          </Button>
        </Stack>

        {providersQuery.data && (
          <ProviderConfigSummary mode={discoveryMode} providers={providersQuery.data} />
        )}
      </Stack>
    </Paper>
  );
}

function ProviderConfigSummary({ mode, providers }: { mode: DiscoveryMode; providers: DiscoveryProviderStatus[] }) {
  const relevantIds = MODE_PROVIDER_IDS[mode];
  const relevant = providers.filter((p) => relevantIds.includes(p.providerId));
  const { configured, total } = configuredCountForMode(mode, providers);

  return (
    <Stack
      spacing={1}
      sx={{ p: 1.5, borderRadius: 2, bgcolor: "background.default", border: "1px solid", borderColor: "divider" }}
    >
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
        {configured} of {total} sources configured for {DISCOVERY_MODE_OPTIONS.find((o) => o.value === mode)?.label} mode
      </Typography>
      <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
        {relevant.map((provider) => (
          <Tooltip key={provider.providerId} title={provider.description}>
            <Chip
              size="small"
              label={provider.providerName}
              color={provider.configured ? "success" : "default"}
              variant={provider.configured ? "filled" : "outlined"}
              sx={{ fontWeight: 600 }}
            />
          </Tooltip>
        ))}
        {configured === 0 && (
          <Typography variant="caption" color="warning.main">
            No sources configured for this mode yet — see README.md for the env vars to set.
          </Typography>
        )}
      </Stack>
    </Stack>
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
