import { describe, expect, it } from "vitest";
import { buildReviewCsv } from "./csv";
import { buildReviewLead } from "./reviewLead";
import type { EvidenceRecord } from "@/lib/evidence/types";
import type { ExportedLead } from "@/lib/export/types";

function evidence(field: string, value: unknown): EvidenceRecord {
  return {
    id: `${field}-1`,
    entityType: "PERSON",
    entityId: "person-1",
    field,
    value,
    sourceProviderId: "apollo-person-enrichment-v1",
    sourceType: "provider",
    externalId: null,
    retrievedAt: "2026-01-01T00:00:00Z",
    confidence: "UNKNOWN",
  };
}

function exportedLead(overrides: Partial<ExportedLead> = {}): ExportedLead {
  return {
    leadId: "lead-1",
    identity: {
      companyId: "company-1",
      companyName: "Example Co",
      companyDomain: "example.invalid",
      personId: "person-1",
      personName: "Jane Testperson",
      personLinkedinId: null,
    },
    hardRuleResult: null,
    scores: { finalScore: 0.82, icpScore: 1, commercialScore: 0.7 },
    qualificationDecision: "GOOD_FIT",
    qualificationSummary: "Strong fit.",
    rank: 1,
    tier: "QUALIFIED_STRONG",
    ...overrides,
  };
}

describe("buildReviewLead", () => {
  it("populates email/title/linkedin from real evidence, never invents them", () => {
    const lead = buildReviewLead(
      exportedLead(),
      undefined,
      [evidence("current_title", "CMO"), evidence("email", "jane@example.invalid"), evidence("linkedin_url", "https://linkedin.com/in/jane")],
      undefined,
      undefined,
    );

    expect(lead.title).toBe("CMO");
    expect(lead.email).toBe("jane@example.invalid");
    expect(lead.personLinkedinUrl).toBe("https://linkedin.com/in/jane");
    expect(lead.enrichmentStatus).toBe("enriched");
  });

  it("leaves email/phone/linkedin null when no evidence exists — never fabricated", () => {
    const lead = buildReviewLead(exportedLead(), undefined, [], undefined, undefined);

    expect(lead.email).toBeNull();
    expect(lead.phone).toBeNull();
    expect(lead.personLinkedinUrl).toBeNull();
    expect(lead.title).toBeNull();
    expect(lead.enrichmentStatus).toBe("not enriched");
  });

  it("falls back to the export's own company name when no separate company row was fetched", () => {
    const lead = buildReviewLead(exportedLead(), undefined, [], undefined, undefined);
    expect(lead.companyName).toBe("Example Co");
  });

  it("company-only leads (no person) have null person fields, not fabricated placeholders", () => {
    const lead = buildReviewLead(
      exportedLead({ identity: { companyId: "company-1", companyName: "Example Co", companyDomain: null, personId: null, personName: null, personLinkedinId: null } }),
      undefined,
      [],
      undefined,
      undefined,
    );
    expect(lead.personId).toBeNull();
    expect(lead.personName).toBeNull();
    expect(lead.title).toBeNull();
  });
});

describe("buildReviewCsv", () => {
  it("includes a header row and one row per lead", () => {
    const lead = buildReviewLead(exportedLead(), undefined, [evidence("current_title", "CMO")], undefined, undefined);
    const csv = buildReviewCsv([lead]);
    const lines = csv.split("\n");

    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain("Company");
    expect(lines[0]).toContain("Email");
    expect(lines[1]).toContain("Example Co");
    expect(lines[1]).toContain("CMO");
  });

  it("renders missing fields as empty CSV cells, never as a placeholder string", () => {
    const lead = buildReviewLead(exportedLead(), undefined, [], undefined, undefined);
    const csv = buildReviewCsv([lead]);
    const dataRow = csv.split("\n")[1];

    expect(dataRow).not.toContain("null");
    expect(dataRow).not.toContain("undefined");
    expect(dataRow).not.toContain("N/A");
  });

  it("escapes commas and quotes in field values", () => {
    const lead = buildReviewLead(
      exportedLead({ identity: { companyId: "c1", companyName: 'Example, "The" Co', companyDomain: null, personId: null, personName: null, personLinkedinId: null } }),
      undefined,
      [],
      undefined,
      undefined,
    );
    const csv = buildReviewCsv([lead]);
    expect(csv).toContain('"Example, ""The"" Co"');
  });
});
