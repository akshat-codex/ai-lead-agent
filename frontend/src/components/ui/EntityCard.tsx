"use client";

import type { ReactNode } from "react";
import Box from "@mui/material/Box";
import Checkbox from "@mui/material/Checkbox";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import { alpha, useTheme } from "@mui/material/styles";
import { motion } from "framer-motion";
import { staggerItem } from "./FadeIn";

interface EntityCardProps {
  /** Whether this card is currently selected — drives the border/tint
   * treatment only. This is a pure visual/presentation flag; the
   * selection SEMANTICS (what "selected" means for the pipeline) remain
   * entirely owned by the caller. */
  selected: boolean;
  onToggleSelected: () => void;
  children: ReactNode;
}

const MotionPaper = motion.create(Paper);

/**
 * Shared visual shell for a selectable entity row (a discovered company or
 * person). Consolidates what used to be two independently-drifted card
 * styles (CompanyRankCard vs PersonCard: different radius, hover
 * elevation, transition duration, selected-state tint) into one definition
 * — CompanyRankCard and PersonCard now both render their own content
 * (name, badges, evidence, etc. — completely unchanged) inside this shell.
 *
 * Presentation only: owns no state beyond `selected`'s visual treatment,
 * and never interprets what "selected" means for the pipeline — that
 * remains entirely the caller's responsibility via onToggleSelected.
 */
export default function EntityCard({ selected, onToggleSelected, children }: EntityCardProps) {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";

  return (
    <MotionPaper
      variant="outlined"
      variants={staggerItem}
      whileHover={{ y: -2 }}
      transition={{ duration: 0.16, ease: "easeOut" }}
      sx={{
        p: { xs: 2, sm: 2.5 },
        borderRadius: 2.5,
        borderWidth: selected ? 1.5 : 1,
        borderColor: selected ? "primary.main" : "divider",
        bgcolor: selected ? (t) => alpha(t.palette.primary.main, isDark ? 0.1 : 0.04) : "background.paper",
        transition: "border-color 150ms ease, box-shadow 150ms ease, background-color 150ms ease",
        "&:hover": {
          boxShadow: (t) => `0 10px 24px ${alpha(t.palette.primary.main, isDark ? 0.18 : 0.1)}`,
          borderColor: selected ? "primary.main" : (t) => alpha(t.palette.primary.main, 0.4),
        },
      }}
    >
      <Stack direction="row" spacing={1.5} sx={{ alignItems: "flex-start" }}>
        <Checkbox checked={selected} onChange={onToggleSelected} sx={{ mt: -0.5 }} />
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>{children}</Box>
      </Stack>
    </MotionPaper>
  );
}
