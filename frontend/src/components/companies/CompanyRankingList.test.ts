import { describe, expect, it } from "vitest";
import { computeHeadlineCounts, emptyStateCopy, groupLeadsByUiTier } from "./CompanyRankingList";
import type { RankedLead } from "@/lib/ranking/types";
import type { RankTier } from "@/components/ui/FitBadge";

function lead(companyId: string, tier: RankTier): RankedLead {
  return {
    rank: 0,
    leadId: `lead-${companyId}`,
    companyId,
    personId: null,
    tier,
    reasonCodes: [],
    signals: {
      hardRuleResult: null,
      finalScore: null,
      icpScore: null,
      commercialScore: null,
      evidenceScore: null,
      qualificationDecision: null,
      qualificationConfidence: null,
    },
    explanation: "",
  };
}

describe("groupLeadsByUiTier", () => {
  it("buckets ACCEPTED/QUALIFIED_STRONG as strong and QUALIFIED_WEAK as good", () => {
    const leads = [lead("c1", "ACCEPTED"), lead("c2", "QUALIFIED_STRONG"), lead("c3", "QUALIFIED_WEAK")];
    const grouped = groupLeadsByUiTier(leads);
    expect(grouped.strong.map((l) => l.companyId)).toEqual(["c1", "c2"]);
    expect(grouped.good.map((l) => l.companyId)).toEqual(["c3"]);
    expect(grouped.weak).toEqual([]);
    expect(grouped.rejected).toEqual([]);
  });

  it("buckets HOLD/REJECTED/DUPLICATE as weak", () => {
    const leads = [lead("c1", "HOLD"), lead("c2", "REJECTED"), lead("c3", "DUPLICATE")];
    const grouped = groupLeadsByUiTier(leads);
    expect(grouped.weak.map((l) => l.companyId)).toEqual(["c1", "c2", "c3"]);
    expect(grouped.rejected).toEqual([]);
  });

  it("buckets HARD_FAILED into its own 'rejected' group, never into 'weak'", () => {
    // The core regression this fix addresses: HARD_FAILED companies must
    // never land in the same bucket as HOLD/REJECTED/DUPLICATE.
    const leads = [lead("c1", "HARD_FAILED"), lead("c2", "HOLD")];
    const grouped = groupLeadsByUiTier(leads);
    expect(grouped.rejected.map((l) => l.companyId)).toEqual(["c1"]);
    expect(grouped.weak.map((l) => l.companyId)).toEqual(["c2"]);
    expect(grouped.rejected).not.toContainEqual(expect.objectContaining({ companyId: "c2" }));
  });

  it("a mixed batch places every lead in exactly one bucket, none dropped", () => {
    const leads = [
      lead("c1", "ACCEPTED"),
      lead("c2", "QUALIFIED_WEAK"),
      lead("c3", "HOLD"),
      lead("c4", "HARD_FAILED"),
      lead("c5", "HARD_FAILED"),
    ];
    const grouped = groupLeadsByUiTier(leads);
    const totalBucketed = grouped.strong.length + grouped.good.length + grouped.weak.length + grouped.rejected.length;
    expect(totalBucketed).toBe(leads.length);
    expect(grouped.rejected.map((l) => l.companyId)).toEqual(["c4", "c5"]);
  });
});

describe("computeHeadlineCounts", () => {
  it("qualifiedFound counts only strong + good, never weak or rejected", () => {
    const grouped = groupLeadsByUiTier([
      lead("c1", "ACCEPTED"),
      lead("c2", "QUALIFIED_WEAK"),
      lead("c3", "HOLD"),
      lead("c4", "HARD_FAILED"),
    ]);
    const { qualifiedFound } = computeHeadlineCounts(4, grouped);
    expect(qualifiedFound).toBe(2);
  });

  it("totalFound reflects every resolved company regardless of outcome", () => {
    // Regression test for the "65 companies found" bug: totalFound must
    // stay a raw discovered/resolved count, independent of qualifiedFound.
    const grouped = groupLeadsByUiTier([lead("c1", "HARD_FAILED"), lead("c2", "HARD_FAILED")]);
    const { totalFound, qualifiedFound } = computeHeadlineCounts(2, grouped);
    expect(totalFound).toBe(2);
    expect(qualifiedFound).toBe(0);
    // The headline must be able to show these as two DIFFERENT numbers —
    // this is exactly what let "65 companies found" misleadingly imply 65
    // genuine matches when all 65 were actually HARD_FAILED.
    expect(totalFound).not.toBe(qualifiedFound);
  });

  it("totalFound and qualifiedFound agree when every company genuinely qualifies", () => {
    const grouped = groupLeadsByUiTier([lead("c1", "ACCEPTED"), lead("c2", "QUALIFIED_STRONG")]);
    const { totalFound, qualifiedFound } = computeHeadlineCounts(2, grouped);
    expect(totalFound).toBe(qualifiedFound);
  });

  it("totalFound can exceed the sum of strong+good+weak+rejected when some companies aren't yet ranked", () => {
    // totalFound is derived from Object.keys(result.companies).length, a
    // superset of companyLeads (which additionally requires a ranked
    // lead) — this asserts computeHeadlineCounts doesn't assume they're
    // always equal.
    const grouped = groupLeadsByUiTier([lead("c1", "ACCEPTED")]);
    const { totalFound, qualifiedFound } = computeHeadlineCounts(5, grouped);
    expect(totalFound).toBe(5);
    expect(qualifiedFound).toBe(1);
  });
});

describe("emptyStateCopy", () => {
  it("shows the honest 'no companies found' message when Hermes is not searching", () => {
    const { title } = emptyStateCopy(false);
    expect(title).toBe("No companies found");
  });

  it("shows the 'Hermes is searching' message instead of a dead-end failure when Hermes is still pending", () => {
    // The core fix: zero results while Explorium is unavailable and
    // Hermes is still working must never read as "search is over, found
    // nothing" — it's still in progress.
    const { title, description } = emptyStateCopy(true);
    expect(title).toBe("Explorium unavailable — Hermes is searching for additional companies");
    expect(description.length).toBeGreaterThan(0);
  });

  it("the two states are never the same message", () => {
    expect(emptyStateCopy(false).title).not.toBe(emptyStateCopy(true).title);
  });
});
