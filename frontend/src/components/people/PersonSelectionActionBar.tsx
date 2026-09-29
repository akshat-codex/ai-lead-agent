"use client";

import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import StickyActionBar from "@/components/ui/StickyActionBar";

interface PersonSelectionActionBarProps {
  count: number;
  onContinue: () => void;
}

export default function PersonSelectionActionBar({ count, onContinue }: PersonSelectionActionBarProps) {
  return (
    <StickyActionBar
      count={count}
      label={`${count} ${count === 1 ? "person" : "people"} selected`}
      actionLabel="Enrich contacts"
      actionIcon={<ArrowForwardIcon />}
      onAction={onContinue}
    />
  );
}
