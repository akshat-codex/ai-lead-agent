"use client";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { FilterCriterion, FilterDefinition } from "@/lib/filters/types";

function formatValue(value: FilterCriterion["value"]): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object" && value !== null) {
    const range = value as { min: number | null; max: number | null };
    if (range.min === null && range.max === null) return "any";
    return `${range.min ?? "any"}–${range.max ?? "any"}`;
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value) || "(empty)";
}

interface SearchCriteriaSummaryProps {
  name: string;
  criteria: FilterCriterion[];
  catalogByKey: Map<string, FilterDefinition>;
}

/**
 * A live, read-only preview of the search definition being built — always
 * reflects the same `criteria` state the builder itself edits, so there is
 * no separate "summary model" that can drift out of sync.
 */
export default function SearchCriteriaSummary({ name, criteria, catalogByKey }: SearchCriteriaSummaryProps) {
  return (
    <Paper variant="outlined" sx={{ p: 2.5, position: { md: "sticky" }, top: { md: 24 } }}>
      <Stack spacing={1.5}>
        <Typography variant="overline" color="text.secondary">
          ICP summary
        </Typography>
        <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
          {name.trim() || "Untitled search"}
        </Typography>

        {criteria.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No criteria yet — describe your ICP or add filters to see them summarized here.
          </Typography>
        ) : (
          <Stack spacing={1}>
            {criteria.map((c) => {
              const label = c.label ?? catalogByKey.get(c.key)?.label ?? c.key;
              return (
                <Box key={c.id}>
                  <Typography variant="caption" color="text.secondary">
                    {label}
                  </Typography>
                  <Stack direction="row" sx={{ flexWrap: "wrap", gap: 0.5, mt: 0.25 }}>
                    <Chip size="small" label={formatValue(c.value)} />
                  </Stack>
                </Box>
              );
            })}
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}
