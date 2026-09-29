"use client";

import StatusChip from "@/components/ui/StatusChip";
import type { Status as ChipColorStatus } from "@/components/ui/StatusChip";
import type { PersonUiStatus } from "@/lib/personEnrichment/useEnrichmentPipeline";

const LABELS: Record<PersonUiStatus, string> = {
  ready: "Ready",
  enriching: "Enriching",
  enriched: "Enriched",
  partial: "Partial",
  failed: "Failed",
  unavailable: "Unavailable",
};

/** Maps the six enrichment states onto StatusChip's existing color scheme
 * (idle/pending/done/failed) via its label prop — StatusChip itself is
 * unmodified. */
const CHIP_STATUS: Record<PersonUiStatus, ChipColorStatus> = {
  ready: "idle",
  enriching: "pending",
  enriched: "done",
  partial: "done",
  failed: "failed",
  unavailable: "idle",
};

interface EnrichmentStatusChipProps {
  status: PersonUiStatus;
}

export default function EnrichmentStatusChip({ status }: EnrichmentStatusChipProps) {
  return <StatusChip status={CHIP_STATUS[status]} label={LABELS[status]} />;
}
