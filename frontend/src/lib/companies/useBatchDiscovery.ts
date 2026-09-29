"use client";

import { useCallback, useRef, useState } from "react";
import { type Batch, type DiscoveryMode, createBatch, resumeBatch } from "@/lib/batch/api";
import { getCompany, tryGetCompanyLinkedInId } from "@/lib/companies/api";
import { tryGetCompanyAttributes } from "@/lib/companies/attributes";
import type { CompanyAttributes } from "@/lib/companies/attributes";
import type { CanonicalCompany } from "@/lib/companies/types";
import { getLatestLeadQualification } from "@/lib/qualification/api";
import type { LeadQualification } from "@/lib/qualification/types";
import { getRanking } from "@/lib/ranking/api";
import type { RankedLead } from "@/lib/ranking/types";
import type { CompanyPipelineResult } from "./useCompanyDiscoveryPipeline";

export type BatchDiscoveryPhase =
  | "idle"
  | "first-page"
  | "loading-more"
  | "ready"
  | "exhausted"
  | "target-reached"
  | "round-limit-reached"
  | "hermes-searching"
  | "provider-error"
  | "error";

interface BatchDiscoveryState {
  phase: BatchDiscoveryPhase;
  batch: Batch | null;
  result: CompanyPipelineResult | null;
  error: string | null;
  targetCount: number | null;
  attributes: Record<string, CompanyAttributes>;
}

// Every user-facing search is capped at this many companies by default —
// "controlled batch" per the product's own safety requirement: never let
// discovery loop indefinitely or burn provider credits trying to reach an
// arbitrary target. The backend's own settings.max_discovery_rounds_per_batch
// (app/core/config.py) is the TRUE, unbypassable ceiling regardless of
// what target this constant requests — this value only shapes what the UI
// asks for, never what the backend is willing to do.
export const DEFAULT_TARGET_COUNT = 10;
export const ADD_MORE_LEADS_INCREMENT = 10;

// How many NEW discovery rounds one "Add More Leads" click may request in
// a single resume call — deliberately small and finite (never unbounded
// auto-continue). The backend's own round-count ceiling
// (DISCOVERY_ROUND_LIMIT_REACHED, app/services/batch_orchestration.py)
// remains the authoritative stop condition across the batch's ENTIRE
// lifetime (initial search + every resume combined) regardless of this
// value — this only bounds how many rounds ONE click may seed.
const ADD_MORE_LEADS_ROUND_BUDGET = 3;

export function phaseAfterRound(batch: Batch, targetCount: number | null): BatchDiscoveryPhase {
  // A dedicated, honest phase for the backend's own safety stop — this is
  // NOT a provider failure (Explorium/Gemini are fine), so it must never
  // be presented with the same "something went wrong, retry" messaging as
  // provider-error; it is an intentional, working-as-designed limit.
  if (batch.discoveryErrorCode === "DISCOVERY_ROUND_LIMIT_REACHED") return "round-limit-reached";
  // Explorium hit a real, terminal error (e.g. EXPLORIUM_CREDITS_EXHAUSTED)
  // — but the backend still submits/checks a Hermes job regardless (see
  // backend/app/services/batch_orchestration.py::_maybe_advance_hermes_job,
  // called unconditionally after the Explorium round loop). A non-null
  // hermesJobId here means that job is still queued/running: discovery is
  // NOT over, it's just continuing through the other source. This must be
  // checked BEFORE the generic provider-error fallthrough below, so the
  // user sees "still searching" rather than a dead-end failure state while
  // Hermes is genuinely still working.
  if (batch.discoveryErrorCode && batch.hermesJobId) return "hermes-searching";
  if (batch.discoveryErrorCode) return "provider-error";
  if (targetCount != null && batch.acceptedCount >= targetCount) return "target-reached";
  if (batch.discoveryPoolExhausted) return "exhausted";
  return "ready";
}

/**
 * Drives the real batch/quality pipeline (discovery -> resolution ->
 * evidence import -> hard ICP validation -> scoring -> qualification ->
 * dedup -> ranking) via POST /api/v1/batches + POST /api/v1/batches/{id}/resume,
 * replacing the older useCompanyDiscoveryPipeline, which never ran hard
 * ICP validation or evidence import and left every lead stuck at HOLD.
 *
 * Every round's ranking is fetched fresh via getRanking(icpId, batch.id) —
 * already the full, deduplicated, accumulated set for this batch (ranking
 * is recomputed server-side from BatchItemModel membership, which only
 * ever grows) — so state is always REPLACED with that fresh result, never
 * concatenated client-side. This is deliberate: the backend is the single
 * source of truth for what has been deduplicated, and concatenating two
 * already-complete sets would itself introduce duplicates.
 */
export function useBatchDiscovery(icpId: string) {
  const [state, setState] = useState<BatchDiscoveryState>({
    phase: "idle",
    batch: null,
    result: null,
    error: null,
    targetCount: null,
    attributes: {},
  });

  // Accumulated per-company side lookups — keyed by companyId (already a
  // canonical, backend-deduplicated identifier), so merging across rounds
  // is always safe and never produces a visible duplicate.
  const companiesRef = useRef<Record<string, CanonicalCompany>>({});
  const qualificationsRef = useRef<Record<string, LeadQualification>>({});
  const linkedInIdsRef = useRef<Record<string, string | null>>({});
  const attributesRef = useRef<Record<string, CompanyAttributes>>({});

  const hydrateNewCompanies = useCallback(
    async (rankedLeads: RankedLead[]) => {
      const newCompanyIds = [...new Set(rankedLeads.map((lead) => lead.companyId))].filter(
        (id) => !(id in companiesRef.current),
      );
      if (newCompanyIds.length === 0) {
        return;
      }
      await Promise.all(
        newCompanyIds.map(async (companyId) => {
          const [company, qualification, linkedInId, attributes] = await Promise.all([
            getCompany(companyId),
            getLatestLeadQualification(icpId, companyId),
            tryGetCompanyLinkedInId(companyId),
            tryGetCompanyAttributes(companyId),
          ]);
          companiesRef.current[companyId] = company;
          if (qualification) qualificationsRef.current[companyId] = qualification;
          linkedInIdsRef.current[companyId] = linkedInId;
          attributesRef.current[companyId] = attributes;
        }),
      );
    },
    [icpId],
  );

  const fetchResultForBatch = useCallback(
    async (batch: Batch): Promise<CompanyPipelineResult> => {
      const ranking = await getRanking(icpId, batch.id);
      await hydrateNewCompanies(ranking.rankedLeads);
      return {
        companies: { ...companiesRef.current },
        qualifications: { ...qualificationsRef.current },
        linkedInIds: { ...linkedInIdsRef.current },
        rankedLeads: ranking.rankedLeads,
      };
    },
    [icpId, hydrateNewCompanies],
  );

  const start = useCallback(
    async (targetCount?: number, discoveryMode?: DiscoveryMode) => {
      // Never let a caller request an uncontrolled batch — DEFAULT_TARGET_COUNT
      // (10) whenever unspecified, and the ONE call this makes is exactly
      // one controlled batch (no automatic multi-round continuation loop
      // here at all — see this hook's own module docstring). The backend's
      // own round-count ceiling (app/services/batch_orchestration.py) is
      // what actually stops discovery within that one call; this hook
      // never loops on top of it.
      const effectiveTarget = targetCount ?? DEFAULT_TARGET_COUNT;
      companiesRef.current = {};
      qualificationsRef.current = {};
      linkedInIdsRef.current = {};
      attributesRef.current = {};
      setState({ phase: "first-page", batch: null, result: null, error: null, targetCount: effectiveTarget, attributes: {} });

      try {
        // discoveryLimit must scale with the requested target — a fixed
        // "20" here previously meant a caller requesting fewer than 20
        // wasted provider calls, and (more importantly) a caller who'd
        // raised MAX_INITIAL_TARGET_COUNT above 20 would still be capped
        // at a discovery pool of 20 regardless of what they asked for.
        // Some headroom above the target itself accounts for candidates
        // that don't survive hard-rule validation.
        const discoveryLimit = Math.max(effectiveTarget * 2, 20);
        const batch = await createBatch(icpId, effectiveTarget, discoveryLimit, discoveryMode ?? "fast");
        const result = await fetchResultForBatch(batch);
        const phase = phaseAfterRound(batch, effectiveTarget);
        setState({ phase, batch, result, error: null, targetCount: effectiveTarget, attributes: { ...attributesRef.current } });
      } catch (error) {
        setState((prev) => ({
          ...prev,
          phase: "error",
          error: error instanceof Error ? error.message : "Something went wrong while finding companies.",
        }));
      }
    },
    [icpId, fetchResultForBatch],
  );

  const findMore = useCallback(
    async (batchId: string) => {
      setState((prev) => ({ ...prev, phase: "loading-more" }));
      try {
        // "Add More Leads" is a single, bounded controlled batch of its
        // own — up to ADD_MORE_LEADS_ROUND_BUDGET new discovery rounds in
        // this one call (never an open-ended loop), targeting
        // ADD_MORE_LEADS_INCREMENT more ACCEPTED companies than whatever
        // has already been found. The backend's own lifetime round cap
        // still applies across this AND every prior call to this batch
        // combined, so this can never bypass it no matter how many times
        // the button is clicked.
        const batch = await resumeBatch(batchId, ADD_MORE_LEADS_ROUND_BUDGET);
        const result = await fetchResultForBatch(batch);
        setState((prev) => {
          const nextTarget = (prev.targetCount ?? batch.acceptedCount) + ADD_MORE_LEADS_INCREMENT;
          return {
            ...prev,
            phase: phaseAfterRound(batch, nextTarget),
            batch,
            result,
            error: null,
            targetCount: nextTarget,
            attributes: { ...attributesRef.current },
          };
        });
      } catch (error) {
        setState((prev) => ({
          ...prev,
          phase: "error",
          error: error instanceof Error ? error.message : "Could not load more leads.",
        }));
      }
    },
    [fetchResultForBatch],
  );

  return { ...state, start, findMore };
}
