"use client";

import Autocomplete from "@mui/material/Autocomplete";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import TagsField from "@/components/icp/TagsField";
import SearchableMultiSelectField from "./SearchableMultiSelectField";
import { suggestionsForFilterKey } from "@/lib/filters/suggestions";
import type { FilterCriterion, FilterDefinition, FilterValue } from "@/lib/filters/types";

interface FilterControlProps {
  definition: FilterDefinition;
  criterion: FilterCriterion;
  onChange: (value: FilterValue) => void;
  error?: string;
}

/**
 * Renders the right input for a criterion, dispatched purely by the catalog's
 * declared valueType. Any filter key the frontend doesn't specially recognize
 * still gets a sensible control here — "known" widgets (see knownControls
 * usage in AddFilterPopover/FilterCriterionChip) only make a nicer version of
 * one of these five, they never replace this fallback.
 */
export default function FilterControl({ definition, criterion, onChange, error }: FilterControlProps) {
  switch (definition.valueType) {
    case "multi-select": {
      const value = Array.isArray(criterion.value) ? criterion.value : [];

      if (definition.options && definition.options.length > 0) {
        return (
          <Autocomplete
            multiple
            options={definition.options.map((o) => o.value)}
            value={value}
            onChange={(_e, newValue) => onChange(newValue)}
            renderInput={(params) => <TextField {...params} label={definition.label} error={Boolean(error)} helperText={error} />}
          />
        );
      }

      // The backend filter catalog leaves these fields free-form (no
      // options), but Location/Industry/Company type still benefit from a
      // searchable local suggestion list — see lib/filters/suggestions.ts
      // for why these are local/static, not a vendor-verified taxonomy.
      const localSuggestions = suggestionsForFilterKey(definition.key);
      if (localSuggestions) {
        return (
          <SearchableMultiSelectField
            label={definition.label}
            helperText={definition.description}
            error={error}
            options={localSuggestions}
            value={value}
            onChange={onChange}
          />
        );
      }

      return (
        <TagsField
          label={definition.label}
          value={value}
          onChange={onChange}
          error={error}
        />
      );
    }

    case "single-select": {
      const value = typeof criterion.value === "string" ? criterion.value : "";
      return (
        <Autocomplete
          options={definition.options?.map((o) => o.value) ?? []}
          value={value || null}
          onChange={(_e, newValue) => onChange(newValue ?? "")}
          renderInput={(params) => <TextField {...params} label={definition.label} error={Boolean(error)} helperText={error} />}
        />
      );
    }

    case "number-range": {
      const range = typeof criterion.value === "object" && criterion.value !== null && !Array.isArray(criterion.value)
        ? (criterion.value as { min: number | null; max: number | null })
        : { min: null, max: null };
      return (
        <Stack spacing={1}>
          <Stack direction="row" spacing={1.5} sx={{ alignItems: "center" }}>
            <TextField
              label="Min"
              type="number"
              size="small"
              value={range.min ?? ""}
              onChange={(e) => onChange({ min: e.target.value === "" ? null : Number(e.target.value), max: range.max })}
              slotProps={{ htmlInput: { min: 0 } }}
              fullWidth
            />
            <Typography variant="body2" color="text.secondary">
              to
            </Typography>
            <TextField
              label="Max"
              type="number"
              size="small"
              value={range.max ?? ""}
              onChange={(e) => onChange({ min: range.min, max: e.target.value === "" ? null : Number(e.target.value) })}
              slotProps={{ htmlInput: { min: 0 } }}
              fullWidth
            />
          </Stack>
          {(error || definition.description) && (
            <Typography variant="caption" color={error ? "error" : "text.secondary"}>
              {error ?? definition.description}
            </Typography>
          )}
        </Stack>
      );
    }

    case "boolean": {
      const value = criterion.value === true;
      return (
        <FormControlLabel
          control={<Switch checked={value} onChange={(e) => onChange(e.target.checked)} />}
          label={definition.label}
        />
      );
    }

    case "text":
    default: {
      const value = typeof criterion.value === "string" ? criterion.value : "";
      return (
        <TextField
          label={definition.label}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          error={Boolean(error)}
          helperText={error ?? definition.description}
          fullWidth
          multiline={definition.key === "custom"}
          minRows={definition.key === "custom" ? 2 : undefined}
        />
      );
    }
  }
}
