"use client";

import type { ReactNode } from "react";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}

export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <Stack
      spacing={1.25}
      sx={{
        alignItems: "center",
        textAlign: "center",
        py: 5,
        px: 3,
        border: "1px dashed",
        borderColor: "rgba(15, 23, 42, 0.16)",
        borderRadius: 2.5,
        bgcolor: "background.paper",
      }}
    >
      {icon}
      <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
        {title}
      </Typography>
      {description && (
        <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 420 }}>
          {description}
        </Typography>
      )}
      {action}
    </Stack>
  );
}
