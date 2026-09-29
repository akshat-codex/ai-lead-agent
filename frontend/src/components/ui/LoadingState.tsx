"use client";

import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface LoadingStateProps {
  label: string;
}

export default function LoadingState({ label }: LoadingStateProps) {
  return (
    <Stack direction="row" spacing={1.5} sx={{ alignItems: "center", py: 1 }}>
      <CircularProgress size={20} thickness={4.5} />
      <Typography variant="body2" color="text.secondary">
        {label}
      </Typography>
    </Stack>
  );
}
