import { describe, expect, it } from "vitest";
import { filterReviewLeads } from "./filterLeads";
import type { ReviewLead } from "./reviewLead";

function lead(overrides: Partial<ReviewLead> = {}): ReviewLead {
  return {
    leadId: "lead-1",
    companyId: "company-1",
    companyName: "Example Co",
    companyDomain: "example.invalid",
    companyLinkedinUrl: null,
    personId: "person-1",
    personName: "Jane Testperson",
    title: "CMO",
    email: null,
    emailStatus: null,
    phone: null,
    personLinkedinUrl: null,
    tier: "QUALIFIED_STRONG",
    finalScore: 0.9,
    qualificationDecision: "GOOD_FIT",
    qualificationSummary: null,
    rank: 1,
    enrichmentStatus: "not enriched",
    ...overrides,
  };
}

describe("filterReviewLeads", () => {
  it("returns everything when filters are empty/all", () => {
    const leads = [lead(), lead({ leadId: "lead-2" })];
    expect(filterReviewLeads(leads, { search: "", tier: "all", enrichment: "all" })).toHaveLength(2);
  });

  it("matches search against company, person, title, email, domain case-insensitively", () => {
    const leads = [lead({ companyName: "Acme Corp" }), lead({ leadId: "lead-2", companyName: "Other Co" })];
    const result = filterReviewLeads(leads, { search: "acme", tier: "all", enrichment: "all" });
    expect(result).toHaveLength(1);
    expect(result[0].companyName).toBe("Acme Corp");
  });

  it("filters by tier using the same rankTierToUiTier mapping used elsewhere", () => {
    const leads = [lead({ tier: "QUALIFIED_STRONG" }), lead({ leadId: "lead-2", tier: "HOLD" })];
    const strongOnly = filterReviewLeads(leads, { search: "", tier: "strong", enrichment: "all" });
    expect(strongOnly).toHaveLength(1);
    expect(strongOnly[0].tier).toBe("QUALIFIED_STRONG");
  });

  it("excludes leads with no tier when a specific tier filter is active", () => {
    const leads = [lead({ tier: null })];
    expect(filterReviewLeads(leads, { search: "", tier: "strong", enrichment: "all" })).toHaveLength(0);
  });

  it("filters by enrichment status", () => {
    const leads = [lead({ enrichmentStatus: "enriched" }), lead({ leadId: "lead-2", enrichmentStatus: "not enriched" })];
    const enrichedOnly = filterReviewLeads(leads, { search: "", tier: "all", enrichment: "enriched" });
    expect(enrichedOnly).toHaveLength(1);
    expect(enrichedOnly[0].enrichmentStatus).toBe("enriched");
  });

  it("combines search, tier, and enrichment filters together", () => {
    const leads = [
      lead({ companyName: "Acme Corp", tier: "QUALIFIED_STRONG", enrichmentStatus: "enriched" }),
      lead({ leadId: "lead-2", companyName: "Acme Corp", tier: "HOLD", enrichmentStatus: "enriched" }),
    ];
    const result = filterReviewLeads(leads, { search: "acme", tier: "strong", enrichment: "enriched" });
    expect(result).toHaveLength(1);
  });
});
