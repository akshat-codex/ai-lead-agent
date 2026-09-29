"use client";

import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

interface NlDescriptionInputProps {
  value: string;
  onChange: (value: string) => void;
  onExtract: () => void;
  disabled?: boolean;
}

export default function NlDescriptionInput({ value, onChange, onExtract, disabled }: NlDescriptionInputProps) {
  return (
    <Stack spacing={1.5}>
      <Typography variant="h6">What kind of customers are you looking for?</Typography>
      <Typography variant="body2" color="text.secondary">
        Describe your ideal customer in plain language. For example: &quot;Find D2C brands in the
        US with 10-300 employees that recently raised funding and are hiring marketing leaders.&quot;
      </Typography>
      <TextField
        multiline
        minRows={3}
        placeholder="Describe who you want to reach..."
        value={value}
        onChange={(e) => onChange(e.target.value)}
        fullWidth
      />
      <Stack direction="row" spacing={1}>
        <Button
          variant="contained"
          startIcon={<AutoAwesomeIcon />}
          onClick={onExtract}
          disabled={disabled || !value.trim()}
        >
          Extract filters
        </Button>
      </Stack>
    </Stack>
  );
}
