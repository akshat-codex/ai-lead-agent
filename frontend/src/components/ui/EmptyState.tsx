"use client";

import type { ReactNode } from "react";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { borderColorStrong } from "@/theme";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}

export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  const theme = useTheme();
  return (
    <Stack
      spacing={1.25}
      sx={{
        alignItems: "center",
        textAlign: "center",
        py: 5,
        px: 3,
        border: "1px dashed",
        borderColor: borderColorStrong(theme.palette.mode),
        borderRadius: 2.5,
        bgcolor: "background.paper",
      }}
    >
      {icon && (
        <Stack
          sx={{
            alignItems: "center",
            justifyContent: "center",
            width: 44,
            height: 44,
            borderRadius: "50%",
            bgcolor: "background.default",
            color: "text.secondary",
          }}
        >
          {icon}
        </Stack>
      )}
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
