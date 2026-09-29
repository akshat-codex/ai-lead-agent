"use client";

import { useCallback, useState } from "react";
import { createLeadDeduplication } from "@/lib/leadDeduplication/api";
import { getPerson, resolvePeopleDiscoveryRun } from "@/lib/people/api";
import type { CanonicalPerson } from "@/lib/people/types";
import { startPeopleDiscoveryRun } from "@/lib/peopleDiscovery/api";
import type { CandidatePerson } from "@/lib/peopleDiscovery/types";
import { createLeadQualification } from "@/lib/qualification/api";
import type { LeadQualification } from "@/lib/qualification/types";
import { getRanking } from "@/lib/ranking/api";
import type { RankedLead } from "@/lib/ranking/types";
import { createLeadScore } from "@/lib/scoring/api";

export interface CompanyPeopleOutcome {
  companyId: string;
  status: "done" | "failed";
  error?: string;
  /** People discovered for this company, keyed by canonical person id. */
  personIds: string[];
}

export interface PeoplePipelineResult {
  /** company id -> its per-company outcome (so one company's failure never
   * blocks the others, and is surfaced explicitly, not as one blanket error). */
  outcomes: Record<string, CompanyPeopleOutcome>;
  people: Record<string, CanonicalPerson>;
  /** The discovery-time candidate each canonical person resolved from — the
   * only place `title` is available (canonical people don't persist one). */
  candidatesByPersonId: Record<string, CandidatePerson>;
  qualifications: Record<string, LeadQualification>;
  rankedLeads: RankedLead[];
}

export type PeoplePipelinePhase = "idle" | "running" | "done" | "error";

interface PipelineState {
  phase: PeoplePipelinePhase;
  statusLabel: string;
  error: string | null;
  result: PeoplePipelineResult | null;
}

/**
 * Runs people-discovery -> resolve -> score/qualify/dedup -> rank for each
 * selected company, against the existing granular endpoints (no bulk
 * endpoint exists — people-discovery is strictly one company per call). One
 * company's failure is captured in its own outcome and never blocks the
 * others; the overall pipeline only reaches "error" if something outside any
 * single company's processing fails (e.g. the final ranking call).
 */
export function usePeopleDiscoveryPipeline(icpId: string) {
  const [state, setState] = useState<PipelineState>({
    phase: "idle",
    statusLabel: "",
    error: null,
    result: null,
  });

  const run = useCallback(
    async (companyIds: string[]) => {
      setState({ phase: "running", statusLabel: "Finding decision-makers...", error: null, result: null });

      const outcomes: Record<string, CompanyPeopleOutcome> = {};
      const people: Record<string, CanonicalPerson> = {};
      const candidatesByPersonId: Record<string, CandidatePerson> = {};
      const qualifications: Record<string, LeadQualification> = {};

      await Promise.allSettled(
        companyIds.map(async (companyId) => {
          try {
            const discoveryRun = await startPeopleDiscoveryRun(icpId, companyId);

            if (discoveryRun.candidates.length === 0) {
              outcomes[companyId] = { companyId, status: "done", personIds: [] };
              return;
            }

            const candidatesByExternalId = new Map(discoveryRun.candidates.map((c) => [c.id, c]));
            const resolutions = await resolvePeopleDiscoveryRun(discoveryRun.id);
            const personIds: string[] = [];

            for (const resolution of resolutions) {
              if (!resolution.canonicalPersonId) continue;
              const personId = resolution.canonicalPersonId;
              const candidate = candidatesByExternalId.get(resolution.candidateId);
              if (candidate) candidatesByPersonId[personId] = candidate;

              const person = await getPerson(personId);
              people[personId] = person;

              await createLeadScore(icpId, companyId, personId);
              const qualification = await createLeadQualification(icpId, companyId, personId);
              qualifications[personId] = qualification;
              await createLeadDeduplication(icpId, { companyId, personId });

              personIds.push(personId);
            }

            outcomes[companyId] = { companyId, status: "done", personIds };
          } catch (error) {
            outcomes[companyId] = {
              companyId,
              status: "failed",
              error: error instanceof Error ? error.message : "Could not find decision-makers for this company.",
              personIds: [],
            };
          }
        }),
      );

      try {
        setState((prev) => ({ ...prev, statusLabel: "Ranking results..." }));
        const ranking = await getRanking(icpId);
        setState({
          phase: "done",
          statusLabel: "",
          error: null,
          result: { outcomes, people, candidatesByPersonId, qualifications, rankedLeads: ranking.rankedLeads },
        });
      } catch (error) {
        setState({
          phase: "error",
          statusLabel: "",
          error: error instanceof Error ? error.message : "Something went wrong while ranking results.",
          result: null,
        });
      }
    },
    [icpId],
  );

  return { ...state, run };
}
