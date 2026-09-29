"use client";

import SearchIcon from "@mui/icons-material/Search";
import InputAdornment from "@mui/material/InputAdornment";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import type { UiTier } from "@/components/ui/FitBadge";

export interface ReviewFilters {
  search: string;
  tier: UiTier | "all";
  enrichment: "all" | "enriched" | "not_enriched";
}

interface ReviewFilterBarProps {
  filters: ReviewFilters;
  onChange: (filters: ReviewFilters) => void;
}

export default function ReviewFilterBar({ filters, onChange }: ReviewFilterBarProps) {
  return (
    <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
      <TextField
        placeholder="Search company, person, title, email..."
        value={filters.search}
        onChange={(e) => onChange({ ...filters, search: e.target.value })}
        size="small"
        fullWidth
        slotProps={{
          input: {
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon fontSize="small" />
              </InputAdornment>
            ),
          },
        }}
      />
      <TextField
        select
        label="Fit"
        value={filters.tier}
        onChange={(e) => onChange({ ...filters, tier: e.target.value as ReviewFilters["tier"] })}
        size="small"
        sx={{ minWidth: 160 }}
      >
        <MenuItem value="all">All fits</MenuItem>
        <MenuItem value="strong">Strong fit</MenuItem>
        <MenuItem value="good">Good fit</MenuItem>
        <MenuItem value="weak">Weak fit</MenuItem>
      </TextField>
      <TextField
        select
        label="Enrichment"
        value={filters.enrichment}
        onChange={(e) => onChange({ ...filters, enrichment: e.target.value as ReviewFilters["enrichment"] })}
        size="small"
        sx={{ minWidth: 170 }}
      >
        <MenuItem value="all">All contacts</MenuItem>
        <MenuItem value="enriched">Enriched</MenuItem>
        <MenuItem value="not_enriched">Not enriched</MenuItem>
      </TextField>
    </Stack>
  );
}
