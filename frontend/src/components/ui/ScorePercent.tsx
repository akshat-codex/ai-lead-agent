"use client";

import Typography from "@mui/material/Typography";

interface ScorePercentProps {
  /** A 0-100 score, exactly as every backend scoring field already is
   * (see backend/app/services/lead_scoring.py::_clamp, which bounds every
   * component score to [0, 100] — confirmed live: a real final_score of
   * 77.69 is stored and returned as-is, never as a 0-1 fraction). null
   * when the backend never scored this lead (e.g. hard_icp_result
   * FAIL/HOLD) — null means "not eligible," not "0%". */
  value: number | null;
}

export default function ScorePercent({ value }: ScorePercentProps) {
  if (value === null) {
    return (
      <Typography variant="body2" color="text.secondary">
        —
      </Typography>
    );
  }
  return (
    <Typography
      variant="body2"
      sx={{
        fontWeight: 700,
        color: "text.primary",
        px: 1,
        py: 0.25,
        borderRadius: 999,
        bgcolor: "background.default",
        border: "1px solid",
        borderColor: "divider",
      }}
    >
      {Math.round(value)}%
    </Typography>
  );
}
