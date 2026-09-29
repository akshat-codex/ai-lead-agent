"use client";

import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { motion } from "framer-motion";
import FitBadge from "@/components/ui/FitBadge";
import type { UiTier } from "@/components/ui/FitBadge";
import { staggerContainer } from "@/components/ui/FadeIn";

interface TierSectionProps {
  tier: UiTier;
  title: string;
  count: number;
  children: React.ReactNode;
}

const MotionStack = motion.create(Stack);

export default function TierSection({ tier, title, count, children }: TierSectionProps) {
  if (count === 0) return null;

  return (
    <Stack spacing={1.75}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
        <FitBadge tier={tier} />
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
          {title}
        </Typography>
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ fontWeight: 700, px: 0.9, py: 0.15, borderRadius: 999, bgcolor: "background.default", border: "1px solid", borderColor: "divider" }}
        >
          {count}
        </Typography>
      </Stack>
      <MotionStack spacing={1.5} initial="hidden" animate="show" variants={staggerContainer}>
        {children}
      </MotionStack>
    </Stack>
  );
}
