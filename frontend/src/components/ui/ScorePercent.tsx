"use client";

import Typography from "@mui/material/Typography";

interface ScorePercentProps {
  /** 0-1 fractional score, or null when the backend never scored this lead
   * (e.g. hard_icp_result FAIL/HOLD) — null means "not eligible," not "0%". */
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
      {Math.round(value * 100)}%
    </Typography>
  );
}
