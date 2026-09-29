"use client";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";

interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
}

export default function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <Alert
      severity="error"
      variant="outlined"
      sx={{ borderRadius: 2, alignItems: "center" }}
      action={
        onRetry ? (
          <Button color="inherit" size="small" onClick={onRetry} sx={{ fontWeight: 600 }}>
            Retry
          </Button>
        ) : undefined
      }
    >
      {message}
    </Alert>
  );
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error.";
}
