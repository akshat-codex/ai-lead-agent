"use client";

import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import type { Batch } from "@/lib/batch/api";

interface BatchCostSummaryProps {
  batch: Batch;
}

const PROVIDER_LABELS: Record<string, string> = {
  "explorium-company-discovery-v1": "Explorium",
  "tavily-company-discovery-v1": "Tavily",
  "serper-company-discovery-v1": "Serper",
  "gemini-llm-v1": "Gemini",
  "openai-v1": "OpenAI",
};

function providerLabel(providerId: string): string {
  return PROVIDER_LABELS[providerId] ?? providerId;
}

/** Real, honest per-batch provider usage — never a fabricated dollar
 * figure. Call counts come straight from the backend's own record of
 * what actually happened (see backend/app/schemas/batch.py::
 * BatchDetailRead.provider_call_counts's own docstring); the Explorium
 * credits number is explicitly labeled as an estimate, since Explorium
 * exposes no API to check real credit consumption. */
export default function BatchCostSummary({ batch }: BatchCostSummaryProps) {
  const entries = Object.entries(batch.providerCallCounts).filter(([, count]) => count > 0);
  if (entries.length === 0) return null;

  return (
    <Stack
      direction="row"
      spacing={1}
      sx={{
        alignItems: "center",
        flexWrap: "wrap",
        rowGap: 1,
        px: 1.75,
        py: 1,
        borderRadius: 2,
        bgcolor: "background.paper",
        border: "1px solid",
        borderColor: "divider",
      }}
    >
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, letterSpacing: "0.02em", textTransform: "uppercase" }}>
        This search used
      </Typography>
      {entries.map(([providerId, count]) => (
        <Chip
          key={providerId}
          size="small"
          variant="outlined"
          label={`${providerLabel(providerId)} × ${count}`}
          sx={{ borderColor: "divider", bgcolor: "background.default" }}
        />
      ))}
      {batch.estimatedExploriumCredits != null && (
        <Tooltip title="Explorium exposes no API to check real credit balance — this is an estimate based on an observed rate of roughly 1 credit per company found, not a vendor-confirmed figure.">
          <Chip
            size="small"
            variant="outlined"
            label={`~${batch.estimatedExploriumCredits} Explorium credits (est.)`}
            sx={{ borderColor: "divider", bgcolor: "background.default" }}
          />
        </Tooltip>
      )}
    </Stack>
  );
}
