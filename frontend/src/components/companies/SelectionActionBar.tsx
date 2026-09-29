"use client";

import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import StickyActionBar from "@/components/ui/StickyActionBar";

interface SelectionActionBarProps {
  count: number;
  onContinue: () => void;
}

export default function SelectionActionBar({ count, onContinue }: SelectionActionBarProps) {
  return (
    <StickyActionBar
      count={count}
      label={`${count} ${count === 1 ? "company" : "companies"} selected`}
      actionLabel="Find decision-makers"
      actionIcon={<ArrowForwardIcon />}
      onAction={onContinue}
    />
  );
}
