"use client";

import Autocomplete, { createFilterOptions } from "@mui/material/Autocomplete";
import Chip from "@mui/material/Chip";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

interface SearchableMultiSelectFieldProps {
  label: string;
  placeholder?: string;
  helperText?: string;
  error?: string;
  /** Local suggestion list to filter against — see lib/filters/suggestions.ts
   * for why these are local/static rather than a vendor taxonomy. */
  options: string[];
  value: string[];
  onChange: (value: string[]) => void;
}

const filterOptions = createFilterOptions<string>({
  // Matches from the first character typed, anywhere case-insensitively in
  // the option — e.g. "hea" matches "Healthcare" and "Health Tech".
  matchFrom: "any",
  limit: 25,
  trim: true,
});

/**
 * A searchable, chip-based multi-select for categorical fields with a known
 * local suggestion list (Location, Industry, Company type). Built on MUI's
 * Autocomplete, which already provides: open-on-focus, filter-as-you-type,
 * full keyboard navigation, Enter/click selection, and an accessible
 * "no options" state — this component only supplies the option source,
 * filtering rule, and chip/empty-state presentation.
 *
 * `freeSolo` stays on: typing a value that isn't in the local list is still
 * accepted as-is (via "Add <value>") since these suggestions are a
 * convenience, never a closed/validated set.
 */
export default function SearchableMultiSelectField({
  label,
  placeholder,
  helperText,
  error,
  options,
  value,
  onChange,
}: SearchableMultiSelectFieldProps) {
  return (
    <Autocomplete
      multiple
      freeSolo
      openOnFocus
      autoHighlight
      options={options}
      value={value}
      filterOptions={filterOptions}
      onChange={(_event, newValue) => onChange(newValue as string[])}
      renderValue={(selected, getItemProps) =>
        selected.map((option, index) => {
          const { key, ...itemProps } = getItemProps({ index });
          return <Chip key={key} label={option} size="small" {...itemProps} />;
        })
      }
      noOptionsText={
        <Typography variant="body2" color="text.secondary">
          No matches — keep typing and press Enter to add it anyway.
        </Typography>
      }
      slotProps={{
        popper: { sx: { zIndex: 1400 } },
        paper: { elevation: 4 },
      }}
      renderInput={(params) => {
        // See TagsField.tsx for why shrink must be computed explicitly under
        // MUI 9's Autocomplete + TextField combination.
        const hasContent = value.length > 0 || Boolean(params.slotProps.htmlInput.value);
        return (
          <TextField
            {...params}
            label={label}
            placeholder={value.length === 0 ? (placeholder ?? "Start typing to search...") : undefined}
            error={Boolean(error)}
            helperText={error ?? helperText}
            slotProps={{
              ...params.slotProps,
              inputLabel: {
                ...params.slotProps.inputLabel,
                shrink: hasContent || undefined,
              },
            }}
          />
        );
      }}
    />
  );
}
