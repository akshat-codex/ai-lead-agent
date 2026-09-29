"use client";

import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { SEARCH_PRESETS } from "@/lib/filters/presets";
import type { SearchPreset } from "@/lib/filters/presets";

interface SearchPresetPickerProps {
  onApply: (preset: SearchPreset) => void;
}

/**
 * Presets are just a fast-fill for the same generic criteria list the rest
 * of the builder edits — nothing here is a special "industry mode". Applying
 * one adds its criteria on top of whatever's already there; every value
 * stays fully editable afterward.
 */
export default function SearchPresetPicker({ onApply }: SearchPresetPickerProps) {
  return (
    <Stack spacing={1}>
      <Typography variant="caption" color="text.secondary">
        Start from a template (optional) — everything stays editable
      </Typography>
      <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
        {SEARCH_PRESETS.map((preset) => (
          <Button key={preset.id} size="small" variant="outlined" onClick={() => onApply(preset)}>
            {preset.label}
          </Button>
        ))}
      </Stack>
    </Stack>
  );
}
