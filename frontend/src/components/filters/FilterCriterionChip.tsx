"use client";

import { useState } from "react";
import CloseIcon from "@mui/icons-material/Close";
import EditIcon from "@mui/icons-material/Edit";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Popover from "@mui/material/Popover";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import FilterControl from "./FilterControl";
import type { FilterCriterion, FilterDefinition, FilterValue } from "@/lib/filters/types";

function formatValue(value: FilterCriterion["value"]): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object" && value !== null) {
    const range = value as { min: number | null; max: number | null };
    return `${range.min ?? "any"}–${range.max ?? "any"}`;
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

interface FilterCriterionChipProps {
  criterion: FilterCriterion;
  definition: FilterDefinition | undefined;
  onChange: (value: FilterValue) => void;
  onRemove: () => void;
}

export default function FilterCriterionChip({ criterion, definition, onChange, onRemove }: FilterCriterionChipProps) {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);

  const label = criterion.label ?? definition?.label ?? criterion.key;
  const displayValue = formatValue(criterion.value);

  return (
    <>
      <Chip
        label={
          <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
            <Typography variant="body2" component="span" sx={{ fontWeight: 600 }}>
              {label}
            </Typography>
            <Typography variant="body2" component="span" color="text.secondary">
              → {displayValue || "(empty)"}
            </Typography>
          </Stack>
        }
        onDelete={onRemove}
        deleteIcon={<CloseIcon fontSize="small" />}
        onClick={(e) => setAnchorEl(e.currentTarget)}
        icon={<EditIcon fontSize="small" />}
        variant="outlined"
        sx={{ height: "auto", py: 0.5, "& .MuiChip-label": { py: 0.5 } }}
      />
      <Popover
        open={Boolean(anchorEl)}
        anchorEl={anchorEl}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        slotProps={{ paper: { elevation: 4 } }}
      >
        <Box sx={{ p: 2, width: 320, maxWidth: "90vw" }}>
          {definition ? (
            <FilterControl definition={definition} criterion={criterion} onChange={onChange} />
          ) : (
            <Typography variant="body2" color="text.secondary">
              Unknown filter — remove and re-add from the catalog.
            </Typography>
          )}
        </Box>
      </Popover>
    </>
  );
}
