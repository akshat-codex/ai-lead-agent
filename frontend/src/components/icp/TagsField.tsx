"use client";

import Autocomplete from "@mui/material/Autocomplete";
import TextField from "@mui/material/TextField";

interface TagsFieldProps {
  label: string;
  placeholder?: string;
  helperText?: string;
  error?: string;
  value: string[];
  onChange: (value: string[]) => void;
}

export default function TagsField({
  label,
  placeholder,
  helperText,
  error,
  value,
  onChange,
}: TagsFieldProps) {
  return (
    <Autocomplete
      multiple
      freeSolo
      options={[]}
      value={value}
      onChange={(_event, newValue) => onChange(newValue as string[])}
      renderInput={(params) => {
        // MUI 9's useAutocomplete no longer computes `shrink` in
        // getInputLabelProps() (it only returns id/htmlFor now), and this
        // Autocomplete's internal input value isn't passed through as
        // TextField's own `value` — so TextField has no signal to shrink the
        // label, and it renders on top of the selected chips/typed text.
        // Compute shrink explicitly from whether the field actually has
        // content (selected tags or in-progress typed text).
        const hasContent = value.length > 0 || Boolean(params.slotProps.htmlInput.value);
        return (
          <TextField
            {...params}
            label={label}
            placeholder={placeholder ?? "Type a value and press Enter"}
            helperText={error ?? helperText}
            error={Boolean(error)}
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
