"use client";

import { useCallback, useState } from "react";
import { getPersonEvidence, importPersonEvidence } from "@/lib/evidence/api";
import type { EvidenceRecord } from "@/lib/evidence/types";
import { enrichPerson } from "@/lib/personEnrichment/api";

export type PersonUiStatus = "ready" | "enriching" | "enriched" | "partial" | "failed" | "unavailable";

export interface PersonEnrichmentState {
  status: PersonUiStatus;
  evidence: EvidenceRecord[];
  errorMessage: string | null;
  providerId: string | null;
}

export type EnrichmentPipelinePhase = "idle" | "running" | "done";

interface PipelineState {
  phase: EnrichmentPipelinePhase;
  byPersonId: Record<string, PersonEnrichmentState>;
}

export function statusFromRun(status: string): PersonUiStatus {
  switch (status) {
    case "COMPLETED":
      return "enriched";
    case "PARTIAL_FAILURE":
      return "partial";
    case "FAILED":
      return "failed";
    case "UNAVAILABLE":
      return "unavailable";
    default:
      return "failed";
  }
}

/**
 * Enriches each selected person independently — one failing never blocks the
 * others (mirrors usePeopleDiscoveryPipeline's per-item Promise.allSettled
 * shape). For each person: import any already-collected discovery evidence
 * (idempotent), call the real enrich endpoint, then read back everything
 * now known via the existing generic evidence endpoint. Status is derived
 * only from the real run status the backend returns — never a synthetic
 * percentage or a guessed "in progress" duration.
 */
export function useEnrichmentPipeline() {
  const [state, setState] = useState<PipelineState>({ phase: "idle", byPersonId: {} });

  const run = useCallback(async (personIds: string[]) => {
    if (personIds.length === 0) return;

    setState({
      phase: "running",
      byPersonId: Object.fromEntries(
        personIds.map((id) => [id, { status: "enriching", evidence: [], errorMessage: null, providerId: null }]),
      ),
    });

    await Promise.allSettled(
      personIds.map(async (personId) => {
        try {
          await importPersonEvidence(personId);
          const enrichmentRun = await enrichPerson(personId);
          const evidence = await getPersonEvidence(personId);

          setState((prev) => ({
            ...prev,
            byPersonId: {
              ...prev.byPersonId,
              [personId]: {
                status: statusFromRun(enrichmentRun.status),
                evidence,
                errorMessage: enrichmentRun.errorMessage,
                providerId: enrichmentRun.providerId,
              },
            },
          }));
        } catch (error) {
          setState((prev) => ({
            ...prev,
            byPersonId: {
              ...prev.byPersonId,
              [personId]: {
                status: "failed",
                evidence: [],
                errorMessage: error instanceof Error ? error.message : "Could not enrich this contact.",
                providerId: null,
              },
            },
          }));
        }
      }),
    );

    setState((prev) => ({ ...prev, phase: "done" }));
  }, []);

  return { ...state, run };
}
