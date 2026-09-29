"use client";

import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";

export type Status = "idle" | "pending" | "done" | "failed";

const STATUS_LABEL: Record<Status, string> = {
  idle: "Not started",
  pending: "In progress",
  done: "Done",
  failed: "Failed",
};

const STATUS_COLOR: Record<Status, "default" | "success" | "error"> = {
  idle: "default",
  pending: "default",
  done: "success",
  failed: "error",
};

interface StatusChipProps {
  status: Status;
  label?: string;
}

export default function StatusChip({ status, label }: StatusChipProps) {
  return (
    <Chip
      size="small"
      label={label ?? STATUS_LABEL[status]}
      color={STATUS_COLOR[status]}
      variant={status === "done" || status === "failed" ? "filled" : "outlined"}
      icon={status === "pending" ? <CircularProgress size={12} color="inherit" /> : undefined}
    />
  );
}
