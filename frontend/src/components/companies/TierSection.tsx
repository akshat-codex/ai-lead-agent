"use client";

import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import FitBadge from "@/components/ui/FitBadge";
import type { UiTier } from "@/components/ui/FitBadge";

interface TierSectionProps {
  tier: UiTier;
  title: string;
  count: number;
  children: React.ReactNode;
}

export default function TierSection({ tier, title, count, children }: TierSectionProps) {
  if (count === 0) return null;

  return (
    <Stack spacing={1.75}>
      <Stack direction="row" spacing={1.25} sx={{ alignItems: "center" }}>
        <FitBadge tier={tier} />
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
          {title}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {count}
        </Typography>
      </Stack>
      <Stack spacing={1.5}>{children}</Stack>
    </Stack>
  );
}
