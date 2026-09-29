"use client";

import { useCallback, useState } from "react";
import { getCompany, resolveDiscoveryRun, tryGetCompanyLinkedInId } from "@/lib/companies/api";
import type { CanonicalCompany } from "@/lib/companies/types";
import { startDiscoveryRun } from "@/lib/discovery/api";
import { createLeadDeduplication } from "@/lib/leadDeduplication/api";
import { createLeadQualification } from "@/lib/qualification/api";
import type { LeadQualification } from "@/lib/qualification/types";
import { getRanking } from "@/lib/ranking/api";
import type { RankedLead } from "@/lib/ranking/types";
import { createLeadScore } from "@/lib/scoring/api";

export interface CompanyPipelineResult {
  companies: Record<string, CanonicalCompany>;
  qualifications: Record<string, LeadQualification>;
  linkedInIds: Record<string, string | null>;
  rankedLeads: RankedLead[];
}

export type PipelinePhase =
  | "idle"
  | "discovering"
  | "resolving"
  | "scoring"
  | "ranking"
  | "done"
  | "error";

interface PipelineState {
  phase: PipelinePhase;
  statusLabel: string;
  error: string | null;
  result: CompanyPipelineResult | null;
}

const PHASE_LABELS: Record<PipelinePhase, string> = {
  idle: "",
  discovering: "Finding companies...",
  resolving: "Resolving company records...",
  scoring: "Scoring and qualifying companies...",
  ranking: "Ranking results...",
  done: "",
  error: "",
};

/**
 * Runs the full discovery -> resolve -> score/qualify -> dedup -> rank
 * pipeline against the existing granular backend endpoints (no new backend
 * endpoint, no pipeline-runs orchestrator — see the approved plan). Every
 * step is a real backend call; failures surface honestly rather than being
 * silently swallowed or faked with a synthetic progress percentage.
 */
export function useCompanyDiscoveryPipeline(icpId: string) {
  const [state, setState] = useState<PipelineState>({
    phase: "idle",
    statusLabel: "",
    error: null,
    result: null,
  });

  const run = useCallback(async () => {
    setState({ phase: "discovering", statusLabel: PHASE_LABELS.discovering, error: null, result: null });
    try {
      const discoveryRun = await startDiscoveryRun(icpId);

      if (discoveryRun.candidates.length === 0) {
        setState({
          phase: "done",
          statusLabel: "",
          error: null,
          result: { companies: {}, qualifications: {}, linkedInIds: {}, rankedLeads: [] },
        });
        return;
      }

      setState((prev) => ({ ...prev, phase: "resolving", statusLabel: PHASE_LABELS.resolving }));
      const resolutions = await resolveDiscoveryRun(discoveryRun.id);
      const companyIds = [...new Set(resolutions.map((r) => r.canonicalCompanyId).filter((id): id is string => Boolean(id)))];

      setState((prev) => ({ ...prev, phase: "scoring", statusLabel: PHASE_LABELS.scoring }));

      const companies: Record<string, CanonicalCompany> = {};
      const qualifications: Record<string, LeadQualification> = {};
      const linkedInIds: Record<string, string | null> = {};

      await Promise.all(
        companyIds.map(async (companyId) => {
          const [company, linkedInId] = await Promise.all([
            getCompany(companyId),
            tryGetCompanyLinkedInId(companyId),
          ]);
          companies[companyId] = company;
          linkedInIds[companyId] = linkedInId;

          // Score first (establishes hard_icp_result / final_score), then
          // qualify — both company-only (person_id omitted), then dedup so
          // the company has a lead row and appears in /rankings.
          await createLeadScore(icpId, companyId);
          const qualification = await createLeadQualification(icpId, companyId);
          qualifications[companyId] = qualification;
          await createLeadDeduplication(icpId, { companyId });
        }),
      );

      setState((prev) => ({ ...prev, phase: "ranking", statusLabel: PHASE_LABELS.ranking }));
      const ranking = await getRanking(icpId);

      setState({
        phase: "done",
        statusLabel: "",
        error: null,
        result: { companies, qualifications, linkedInIds, rankedLeads: ranking.rankedLeads },
      });
    } catch (error) {
      setState({
        phase: "error",
        statusLabel: "",
        error: error instanceof Error ? error.message : "Something went wrong while finding companies.",
        result: null,
      });
    }
  }, [icpId]);

  return { ...state, run };
}
