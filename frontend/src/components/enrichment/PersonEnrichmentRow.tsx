"use client";

import LaunchIcon from "@mui/icons-material/Launch";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import EnrichmentStatusChip from "./EnrichmentStatusChip";
import { latestValueForField } from "@/lib/evidence/api";
import type { PersonEnrichmentState } from "@/lib/personEnrichment/useEnrichmentPipeline";

interface PersonEnrichmentRowProps {
  name: string;
  companyName: string | null;
  state: PersonEnrichmentState;
}

function Field({ label, value, href }: { label: string; value: string | null; href?: string }) {
  return (
    <Stack spacing={0}>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      {value ? (
        href ? (
          <Link href={href} target="_blank" rel="noopener noreferrer" variant="body2" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
            {value} <LaunchIcon fontSize="inherit" />
          </Link>
        ) : (
          <Typography variant="body2">{value}</Typography>
        )
      ) : (
        <Typography variant="body2" color="text.secondary" sx={{ fontStyle: "italic" }}>
          Unavailable
        </Typography>
      )}
    </Stack>
  );
}

export default function PersonEnrichmentRow({ name, companyName, state }: PersonEnrichmentRowProps) {
  const title = latestValueForField(state.evidence, "current_title");
  const email = latestValueForField(state.evidence, "email");
  const linkedinUrl = latestValueForField(state.evidence, "linkedin_url");
  const linkedinId = latestValueForField(state.evidence, "linkedin_id");
  const linkedinHref =
    typeof linkedinUrl === "string" ? linkedinUrl : typeof linkedinId === "string" ? `https://linkedin.com/in/${linkedinId}` : undefined;

  return (
    <Paper variant="outlined" sx={{ p: 2.25, borderRadius: 2.5 }}>
      <Stack direction={{ xs: "column", md: "row" }} spacing={2.5} sx={{ alignItems: { md: "center" } }}>
        <Stack sx={{ minWidth: 180 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            {name}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {companyName ?? "Unknown company"}
          </Typography>
        </Stack>

        <Stack direction="row" spacing={3} sx={{ flexWrap: "wrap", rowGap: 1.5, flexGrow: 1 }}>
          <Field label="Title" value={typeof title === "string" ? title : null} />
          <Field label="Email" value={typeof email === "string" ? email : null} />
          {/* Phone is never returned by this phase's Apollo integration —
              Apollo only delivers it asynchronously via a webhook this
              codebase has no receiver for. Always shown as Unavailable,
              never fabricated. */}
          <Field label="Phone" value={null} />
          <Field label="LinkedIn" value={linkedinHref ? "View profile" : null} href={linkedinHref} />

          <Stack sx={{ minWidth: 120 }}>
            <Typography variant="caption" color="text.secondary">
              Source
            </Typography>
            <Typography variant="body2">{state.providerId ?? "—"}</Typography>
          </Stack>
        </Stack>

        <EnrichmentStatusChip status={state.status} />
      </Stack>

      {state.status === "failed" && state.errorMessage && (
        <Typography variant="caption" color="error" sx={{ mt: 1.25, display: "block" }}>
          {state.errorMessage}
        </Typography>
      )}
      {state.status === "unavailable" && (
        <Typography variant="caption" color="text.secondary" sx={{ mt: 1.25, display: "block" }}>
          No enrichment provider is configured yet.
        </Typography>
      )}
    </Paper>
  );
}
