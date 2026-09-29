"use client";

import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { ExtractedFilter, FilterDefinition } from "@/lib/filters/types";

function formatValue(value: ExtractedFilter["value"]): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object" && value !== null) {
    const range = value as { min: number | null; max: number | null };
    return `${range.min ?? "any"}–${range.max ?? "any"}`;
  }
  return String(value);
}

interface ExtractedFiltersReviewProps {
  filters: ExtractedFilter[];
  unmatched: string[];
  catalogByKey: Map<string, FilterDefinition>;
  onDismiss: (id: string) => void;
  onAcceptAll: () => void;
  onDismissAll: () => void;
}

/**
 * The mandatory review gate for NL-extracted filters — nothing here is part
 * of the structured search definition yet. The user must explicitly accept
 * (individually, via onDismiss removing the rest, or all at once) before
 * these become real criteria.
 */
export default function ExtractedFiltersReview({
  filters,
  unmatched,
  catalogByKey,
  onDismiss,
  onAcceptAll,
  onDismissAll,
}: ExtractedFiltersReviewProps) {
  if (filters.length === 0 && unmatched.length === 0) {
    return (
      <Alert severity="info">
        Nothing recognizable in that description yet — use &quot;Add filter&quot; below to build
        your search criteria directly.
      </Alert>
    );
  }

  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack spacing={1.5}>
        <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
          Review extracted filters
        </Typography>
        <Typography variant="body2" color="text.secondary">
          These were guessed from your description — review and confirm before they become part of
          your search.
        </Typography>

        {filters.length > 0 && (
          <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
            {filters.map((f) => (
              <Chip
                key={f.id}
                label={`${f.label ?? catalogByKey.get(f.key)?.label ?? f.key} → ${formatValue(f.value)}`}
                onDelete={() => onDismiss(f.id)}
                variant="outlined"
                color="primary"
              />
            ))}
          </Stack>
        )}

        {unmatched.length > 0 && (
          <Stack spacing={0.5}>
            <Typography variant="caption" color="text.secondary">
              Unmatched — add manually with &quot;Add filter&quot; if relevant:
            </Typography>
            <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
              {unmatched.map((text) => (
                <Chip key={text} label={text} size="small" variant="outlined" disabled />
              ))}
            </Stack>
          </Stack>
        )}

        {filters.length > 0 && (
          <Stack direction="row" spacing={1}>
            <Button size="small" startIcon={<CheckIcon />} variant="contained" onClick={onAcceptAll}>
              Accept all
            </Button>
            <Button size="small" startIcon={<CloseIcon />} onClick={onDismissAll}>
              Dismiss all
            </Button>
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}
