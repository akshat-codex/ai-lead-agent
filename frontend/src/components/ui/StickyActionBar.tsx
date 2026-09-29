"use client";

import type { ReactNode } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import { AnimatePresence, motion } from "framer-motion";

interface StickyActionBarProps {
  count: number;
  label: ReactNode;
  actionLabel: string;
  onAction: () => void;
  actionIcon?: ReactNode;
}

/**
 * Shared sticky bottom action bar for a "N selected, continue" pattern —
 * consolidates what used to be two independently-drifted implementations
 * (SelectionActionBar for companies, PersonSelectionActionBar for people:
 * different elevation, one had a border+radius treatment and the other
 * didn't). Presentation only — `count === 0` hides the bar exactly as
 * both prior components did; callers own what "count"/"label"/the action
 * callback mean.
 */
export default function StickyActionBar({ count, label, actionLabel, onAction, actionIcon }: StickyActionBarProps) {
  return (
    <AnimatePresence>
      {count > 0 && (
        <Box
          component={motion.div}
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 16 }}
          transition={{ duration: 0.22, ease: "easeOut" }}
          sx={{ position: "sticky", bottom: 0, py: { xs: 1.5, sm: 2 }, px: { xs: 2, sm: 0 }, zIndex: 1 }}
        >
          <Paper
            elevation={4}
            sx={{
              p: { xs: 1.5, sm: 2 },
              display: "flex",
              justifyContent: "center",
              borderRadius: 3,
              border: "1px solid",
              borderColor: "divider",
              boxShadow: (t) => `0 12px 32px ${alpha(t.palette.common.black, t.palette.mode === "dark" ? 0.5 : 0.14)}`,
            }}
          >
            <Stack
              direction={{ xs: "column", sm: "row" }}
              spacing={{ xs: 1, sm: 2.5 }}
              sx={{ alignItems: "center", width: { xs: "100%", sm: "auto" } }}
            >
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {label}
              </Typography>
              <Button variant="contained" endIcon={actionIcon} onClick={onAction} fullWidth={false} sx={{ width: { xs: "100%", sm: "auto" } }}>
                {actionLabel}
              </Button>
            </Stack>
          </Paper>
        </Box>
      )}
    </AnimatePresence>
  );
}
