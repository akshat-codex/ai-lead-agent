import { rankTierToUiTier } from "@/components/ui/FitBadge";
import type { RankTier } from "@/components/ui/FitBadge";
import type { ReviewFilters } from "@/components/review/ReviewFilterBar";
import type { ReviewLead } from "./reviewLead";

function matchesSearch(lead: ReviewLead, query: string): boolean {
  if (!query.trim()) return true;
  const haystack = [lead.companyName, lead.personName, lead.title, lead.email, lead.companyDomain]
    .filter((v): v is string => Boolean(v))
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.trim().toLowerCase());
}

function matchesTier(lead: ReviewLead, tier: ReviewFilters["tier"]): boolean {
  if (tier === "all") return true;
  if (!lead.tier) return false;
  return rankTierToUiTier(lead.tier as RankTier) === tier;
}

function matchesEnrichment(lead: ReviewLead, enrichment: ReviewFilters["enrichment"]): boolean {
  if (enrichment === "all") return true;
  if (enrichment === "enriched") return lead.enrichmentStatus === "enriched";
  return lead.enrichmentStatus === "not enriched";
}

export function filterReviewLeads(leads: ReviewLead[], filters: ReviewFilters): ReviewLead[] {
  return leads.filter(
    (lead) => matchesSearch(lead, filters.search) && matchesTier(lead, filters.tier) && matchesEnrichment(lead, filters.enrichment),
  );
}
