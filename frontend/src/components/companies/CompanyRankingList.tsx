"use client";

import { useMemo, useState } from "react";
import HourglassEmptyOutlinedIcon from "@mui/icons-material/HourglassEmptyOutlined";
import SearchOffOutlinedIcon from "@mui/icons-material/SearchOffOutlined";
import Alert from "@mui/material/Alert";
import Collapse from "@mui/material/Collapse";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CompanyRankCard from "./CompanyRankCard";
import SelectionActionBar from "./SelectionActionBar";
import TierSection from "./TierSection";
import EmptyState from "@/components/ui/EmptyState";
import { rankTierToUiTier } from "@/components/ui/FitBadge";
import type { CompanyAttributes } from "@/lib/companies/attributes";
import type { CompanyPipelineResult } from "@/lib/companies/useCompanyDiscoveryPipeline";
import type { RankedLead } from "@/lib/ranking/types";
import { useLeadAgentSession } from "@/lib/leads/sessionContext";

interface CompanyRankingListProps {
  result: CompanyPipelineResult;
  attributes?: Record<string, CompanyAttributes>;
  onContinue: () => void;
  /** True while Explorium is unavailable (a real discoveryErrorCode) but a
   * Hermes discovery job is still queued/running — see
   * useBatchDiscovery.ts's "hermes-searching" phase. Changes the empty
   * state's message so a zero-results moment mid-search never reads as
   * "no companies found" (search over, nothing matched) when discovery is
   * actually still in progress through Hermes. */
  hermesSearching?: boolean;
}

/** Buckets ranked leads by UI tier — pure, no React, so this is unit-testable
 * without a rendering environment (this codebase's frontend test suite has
 * no React Testing Library / jsdom set up; see vitest.config.mts). */
export function groupLeadsByUiTier(leads: RankedLead[]): {
  strong: RankedLead[];
  good: RankedLead[];
  weak: RankedLead[];
  rejected: RankedLead[];
} {
  const strong: RankedLead[] = [];
  const good: RankedLead[] = [];
  const weak: RankedLead[] = [];
  const rejected: RankedLead[] = [];
  for (const lead of leads) {
    const uiTier = rankTierToUiTier(lead.tier);
    if (uiTier === "strong") strong.push(lead);
    else if (uiTier === "good") good.push(lead);
    else if (uiTier === "rejected") rejected.push(lead);
    else weak.push(lead);
  }
  return { strong, good, weak, rejected };
}

/** The two headline numbers shown above the company list: `totalFound` is
 * every resolved candidate regardless of outcome (never implies a match);
 * `qualifiedFound` counts only Strong/Good-fit leads — the honest
 * "worth reviewing" number. Pure so it's unit-testable the same way. */
export function computeHeadlineCounts(
  totalCompanies: number,
  grouped: { strong: RankedLead[]; good: RankedLead[] },
): { totalFound: number; qualifiedFound: number } {
  return { totalFound: totalCompanies, qualifiedFound: grouped.strong.length + grouped.good.length };
}

/** What the empty state shows when zero companies have been found yet —
 * pure so the "still searching" vs. "search is over" distinction is
 * unit-testable without a rendering environment. `hermesSearching` is
 * true when Explorium is unavailable but a Hermes discovery job is still
 * queued/running (see useBatchDiscovery.ts's "hermes-searching" phase) —
 * in that case zero results does NOT mean the search is over. */
export function emptyStateCopy(hermesSearching: boolean): { title: string; description: string } {
  if (hermesSearching) {
    return {
      title: "Explorium unavailable — Hermes is searching for additional companies",
      description: "This can take a few minutes. Check back shortly or use Add More Leads to check progress.",
    };
  }
  return {
    title: "No companies found",
    description: "No companies matched this search. Try broadening your filters and search again.",
  };
}

export default function CompanyRankingList({ result, attributes, onContinue, hermesSearching = false }: CompanyRankingListProps) {
  const { selectedCompanyIds, toggleCompanySelected } = useLeadAgentSession();
  // Default OPEN, not collapsed: every discovered company (regardless of
  // outcome) must be visible without an extra click — a collapsed-by-
  // default section here previously read as "the backend only found N
  // companies" when it had actually found more, just bucketed into a
  // hidden section. Still collapsible (the Hide/Show link still works)
  // for a user who wants to declutter after reviewing everything once.
  const [showNotShown, setShowNotShown] = useState(true);
  const [showRejected, setShowRejected] = useState(true);

  // Only company-level leads (no person_id) matter for step 2's grouping —
  // this page never ran people-discovery, so every ranked lead here is a
  // company-only lead.
  const companyLeads = useMemo(
    () => result.rankedLeads.filter((lead) => lead.personId === null && result.companies[lead.companyId]),
    [result],
  );

  const grouped = useMemo(() => groupLeadsByUiTier(companyLeads), [companyLeads]);

  // totalFound: every resolved candidate, regardless of outcome — never
  // implies these are all genuinely matching companies. qualifiedFound:
  // only Strong/Good fit, the honest "worth reviewing" number. Kept
  // distinct so the headline never repeats the "65 companies found" bug:
  // a raw discovered count reading as if it were a qualified-match count.
  const { totalFound, qualifiedFound } = computeHeadlineCounts(Object.keys(result.companies).length, grouped);

  if (totalFound === 0) {
    const { title, description } = emptyStateCopy(hermesSearching);
    return (
      <EmptyState
        icon={hermesSearching ? <HourglassEmptyOutlinedIcon fontSize="small" /> : <SearchOffOutlinedIcon fontSize="small" />}
        title={title}
        description={description}
      />
    );
  }

  const renderCard = (lead: (typeof companyLeads)[number]) => {
    const company = result.companies[lead.companyId];
    const qualification = result.qualifications[lead.companyId];
    return (
      <CompanyRankCard
        key={lead.companyId}
        companyName={company.canonicalName}
        companyDomain={company.canonicalDomain}
        ranked={lead}
        attributes={attributes?.[lead.companyId] ?? null}
        qualificationSummary={qualification?.summary || null}
        qualificationExplanation={qualification?.commercialFitExplanation || null}
        linkedInId={result.linkedInIds[lead.companyId] ?? null}
        isEvaluationPending={!qualification}
        selected={selectedCompanyIds.includes(lead.companyId)}
        onToggleSelected={() => toggleCompanySelected(lead.companyId)}
      />
    );
  };

  return (
    <Stack spacing={4} sx={{ pb: 12 }}>
      <Stack
        direction="row"
        spacing={1}
        sx={{
          alignItems: "baseline",
          justifyContent: "space-between",
          flexWrap: "wrap",
          rowGap: 1,
          px: 2,
          py: 1.5,
          borderRadius: 2,
          bgcolor: "background.paper",
          border: "1px solid",
          borderColor: "divider",
        }}
      >
        <Typography variant="body2" color="text.secondary">
          <Typography component="span" variant="body2" sx={{ fontWeight: 700, color: "text.primary" }}>
            {totalFound} {totalFound === 1 ? "company" : "companies"} found
          </Typography>
          {totalFound !== qualifiedFound &&
            ` — ${qualifiedFound} strong/good fit, ${totalFound - qualifiedFound} shown below as weaker or rejected`}
          {totalFound === qualifiedFound && ` — all worth reviewing`} &middot; select the ones to research further
        </Typography>
      </Stack>

      <TierSection tier="strong" title="Strong Fit" count={grouped.strong.length}>
        {grouped.strong.map(renderCard)}
      </TierSection>

      <TierSection tier="good" title="Good Fit" count={grouped.good.length}>
        {grouped.good.map(renderCard)}
      </TierSection>

      {grouped.weak.length > 0 && (
        <Stack spacing={1.5}>
          <Link
            component="button"
            variant="body2"
            underline="hover"
            onClick={() => setShowNotShown((v) => !v)}
            sx={{ fontWeight: 600, alignSelf: "flex-start" }}
          >
            {showNotShown ? "Hide" : "Show"} weaker matches ({grouped.weak.length})
          </Link>
          <Collapse in={showNotShown}>
            <Stack spacing={1.5}>
              <TierSection tier="weak" title="Weak Fit" count={grouped.weak.length}>
                {grouped.weak.map(renderCard)}
              </TierSection>
            </Stack>
          </Collapse>
        </Stack>
      )}

      {grouped.rejected.length > 0 && (
        <Stack spacing={1.5}>
          <Link
            component="button"
            variant="body2"
            underline="hover"
            onClick={() => setShowRejected((v) => !v)}
            sx={{ fontWeight: 600, alignSelf: "flex-start" }}
          >
            {showRejected ? "Hide" : "Show"} rejected companies ({grouped.rejected.length})
          </Link>
          <Collapse in={showRejected}>
            <Stack spacing={1.5}>
              {/* Kept visible (never hidden entirely) for transparency —
                  these are real, honest hard-rule FAILures, distinct from
                  "Weak Fit" above: HARD_FAILED means actively disproven
                  against the ICP, not merely a weak match. See
                  rankTierToUiTier's own docstring in FitBadge.tsx. */}
              <TierSection tier="rejected" title="Rejected" count={grouped.rejected.length}>
                {grouped.rejected.map(renderCard)}
              </TierSection>
            </Stack>
          </Collapse>
        </Stack>
      )}

      {companyLeads.length === 0 && totalFound > 0 && (
        <Alert severity="info">
          Companies were found but haven&apos;t been ranked yet. Try again shortly.
        </Alert>
      )}

      <SelectionActionBar count={selectedCompanyIds.length} onContinue={onContinue} />
    </Stack>
  );
}
