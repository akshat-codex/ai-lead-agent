import { describe, expect, it } from "vitest";
import { FALLBACK_KNOWN_FILTERS } from "./api";
import { extractExclusionSpans, extractFiltersFromDescription } from "./nlExtraction";

const FULL_CATALOG = [
  ...FALLBACK_KNOWN_FILTERS,
  { key: "funding", label: "Funding", valueType: "multi-select" as const },
  { key: "hiring", label: "Hiring", valueType: "multi-select" as const },
  { key: "revenue", label: "Revenue", valueType: "number-range" as const },
];

describe("extractFiltersFromDescription", () => {
  it("extracts industry, geography, employee range, funding and hiring from the worked example", () => {
    const text =
      "Find D2C brands in the US with 10-300 employees that recently raised funding and are hiring marketing leaders.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);

    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toEqual(["D2C"]);
    expect(byKey.geography?.value).toEqual(["United States"]);
    expect(byKey.employee_range?.value).toEqual({ min: 10, max: 300 });
    expect(byKey.funding?.value).toEqual(["Recently raised"]);
    expect(byKey.hiring?.value).toEqual(["Marketing"]);

    for (const f of filters) {
      expect(f.confidence).toBe("low");
      expect(f.source).toBe("nl_extraction");
    }
  });

  it("does not extract a filter for a key absent from the catalog", () => {
    const text = "Find D2C brands that recently raised funding";
    const { filters } = extractFiltersFromDescription(text, FALLBACK_KNOWN_FILTERS);
    expect(filters.some((f) => f.key === "funding")).toBe(false);
  });

  it("returns an empty result for text with no recognizable criteria", () => {
    const { filters } = extractFiltersFromDescription("hello there", FALLBACK_KNOWN_FILTERS);
    expect(filters).toEqual([]);
  });
});

describe("positive criteria vs exclusion criteria", () => {
  it("a term stated ONLY inside an exclusion clause never becomes a positive industry criterion", () => {
    // The core reported bug: "SaaS" appears only inside "Exclude ...
    // SaaS vendors" and must never leak into the positive industry
    // filter.
    const text = "Find D2C brands. Exclude Healthcare and SaaS vendors.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toEqual(["D2C"]);
    expect(byKey.industry?.value).not.toContain("SaaS");
  });

  it("the same exclusion clause populates the exclusions filter as a negative constraint", () => {
    const text = "Find D2C brands. Exclude Healthcare and SaaS vendors.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.operator).toBe("not_in");
    // "SaaS vendors" survives as ONE meaningful multi-word exclusion
    // entry rather than being reduced to just "SaaS" — see the "multi-
    // word exclusions" describe block below for the dedicated coverage.
    expect(byKey.exclusions?.value).toEqual(expect.arrayContaining(["SaaS Vendors", "Healthcare"]));
  });

  it("a term mentioned BOTH positively and inside an exclusion clause still counts as positive", () => {
    // A genuinely separate positive mention outside the exclusion clause
    // must not be suppressed just because the same term also appears
    // inside an unrelated exclusion elsewhere in the text.
    const text = "Find SaaS companies. Exclude hospitals and legacy SaaS resellers with no self-serve signup.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toContain("SaaS");
  });

  it("generalizes to a different industry pair (Healthcare positive, Fintech excluded) with no hardcoded term list", () => {
    const text = "Find Healthcare brands. Exclude Fintech companies.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toEqual(["Healthcare"]);
  });

  it("a description with no exclusion clause never populates the exclusions filter", () => {
    const text = "Find D2C brands in the US with 10-300 employees.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    expect(filters.some((f) => f.key === "exclusions")).toBe(false);
  });

  it("extractExclusionSpans finds the trigger-to-clause-boundary span, not the whole remaining text", () => {
    const text = "Find D2C brands. Exclude hospitals and agencies. That require 50-200 employees.";
    const spans = extractExclusionSpans(text);
    expect(spans).toHaveLength(1);
    const excludedText = text.slice(spans[0].start, spans[0].end);
    expect(excludedText.toLowerCase()).toContain("exclude");
    expect(excludedText.toLowerCase()).not.toContain("50-200");
  });
});

describe("employee range extraction", () => {
  it("extracts a plain small range unchanged", () => {
    const { filters } = extractFiltersFromDescription("Companies with 10-300 employees.", FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.employee_range?.value).toEqual({ min: 10, max: 300 });
  });

  it("extracts a comma-formatted four-digit range (the reported '50-1,000 employees' case)", () => {
    const { filters } = extractFiltersFromDescription("Companies with 50-1,000 employees.", FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.employee_range?.value).toEqual({ min: 50, max: 1000 });
  });

  it("handles an en dash and the word 'to' as separators", () => {
    const { filters: enDash } = extractFiltersFromDescription("50–1,000 employees", FULL_CATALOG);
    expect(Object.fromEntries(enDash.map((f) => [f.key, f])).employee_range?.value).toEqual({ min: 50, max: 1000 });

    const { filters: toWord } = extractFiltersFromDescription("50 to 1,000 employees", FULL_CATALOG);
    expect(Object.fromEntries(toWord.map((f) => [f.key, f])).employee_range?.value).toEqual({ min: 50, max: 1000 });
  });
});

describe("revenue range extraction", () => {
  it("extracts a $XM-$YM revenue range (the reported '$10M-$500M revenue' case)", () => {
    const { filters } = extractFiltersFromDescription("Companies with $10M-$500M revenue.", FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });
    expect(byKey.revenue?.operator).toBe("range");
  });

  it("extracts a revenue range without a leading $ on the second bound", () => {
    const { filters } = extractFiltersFromDescription("$10M to 500M in revenue", FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });
  });

  it("extracts a billion-scale revenue range", () => {
    const { filters } = extractFiltersFromDescription("$1B-$5B revenue", FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.revenue?.value).toEqual({ min: 1_000_000_000, max: 5_000_000_000 });
  });

  it("does not extract a revenue filter when the catalog doesn't support the key", () => {
    const { filters } = extractFiltersFromDescription("$10M-$500M revenue", FALLBACK_KNOWN_FILTERS);
    expect(filters.some((f) => f.key === "revenue")).toBe(false);
  });

  it("extracts a revenue range phrased BEFORE the numbers (the reported 'estimated revenue $10M-$500M' case)", () => {
    const { filters } = extractFiltersFromDescription("Companies with estimated revenue $10M-$500M.", FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });
  });

  it("extracts 'revenue of $10M-$500M' and 'revenue around $10M-$500M' the same way", () => {
    const { filters: ofPhrasing } = extractFiltersFromDescription("revenue of $10M-$500M", FULL_CATALOG);
    expect(Object.fromEntries(ofPhrasing.map((f) => [f.key, f])).revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });

    const { filters: aroundPhrasing } = extractFiltersFromDescription("revenue around $10M-$500M", FULL_CATALOG);
    expect(Object.fromEntries(aroundPhrasing.map((f) => [f.key, f])).revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });
  });
});

describe("comma-separated exclusion lists (generic)", () => {
  it("preserves every item in a long comma-separated exclusion list, not just the first recognized term", () => {
    const text = "Find D2C brands. Exclude agencies, consultancies, marketing firms, SaaS vendors, staffing firms, and service providers.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.value).toEqual([
      "Agencies",
      "Consultancies",
      "Marketing Firms",
      "SaaS Vendors",
      "Staffing Firms",
      "Service Providers",
    ]);
  });

  it("none of the excluded list items ever become a positive industry/company_type criterion", () => {
    const text = "Find D2C brands. Exclude agencies, consultancies, marketing firms, SaaS vendors, staffing firms, and service providers.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toEqual(["D2C"]);
  });

  it("generalizes to a completely different exclusion list with no hardcoded business-type vocabulary", () => {
    // Proves the mechanism is generic list-splitting, not a lookup table
    // of known "bad" business types.
    const text = "Find Fintech brands. Exclude resellers, wholesalers, and franchise operators.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.value).toEqual(["Resellers", "Wholesalers", "Franchise Operators"]);
  });

  it("an excluded list item recognized by a term map uses that term's canonical label, not a re-title-cased copy", () => {
    const text = "Find D2C brands. Exclude healthcare and fmcg.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.value).toEqual(expect.arrayContaining(["Healthcare", "FMCG"]));
  });

  it("exclusion list items never leak into the unmatched list", () => {
    const text = "Find D2C brands. Exclude agencies, consultancies, marketing firms, SaaS vendors, staffing firms, and service providers.";
    const { unmatched } = extractFiltersFromDescription(text, FULL_CATALOG);
    for (const excludedTerm of ["agencies", "consultancies", "marketing firms", "SaaS vendors", "staffing firms", "service providers"]) {
      expect(unmatched.some((u) => u.toLowerCase().includes(excludedTerm.toLowerCase()))).toBe(false);
    }
  });
});

describe("multi-word exclusions are preserved as meaningful phrases", () => {
  it("'SaaS vendors' is preserved as one exclusion entry, never split into an unrelated 'SaaS' positive/negative pair", () => {
    const text = "Find D2C brands. Exclude SaaS vendors.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.value).toEqual(["SaaS Vendors"]);
    // D2C is a genuine, separate positive match (from "Find D2C brands"),
    // unaffected by the exclusion clause — the important assertion is
    // that "SaaS" itself never appears in the positive industry value.
    expect(byKey.industry?.value).toEqual(["D2C"]);
    expect(byKey.industry?.value).not.toContain("SaaS");
  });

  it("a standalone excluded term with no extra words still uses its known canonical label", () => {
    const text = "Find D2C brands. Exclude SaaS.";
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.value).toEqual(["SaaS"]);
  });
});

describe("business-model/ecommerce ownership requirement (custom filter fallback)", () => {
  it("a leftover requirement sentence becomes a custom filter when the catalog supports it", () => {
    const text = "Find D2C brands. Find companies that actually sell their own consumer products through ecommerce/DTC channels.";
    const catalogWithCustom = [...FULL_CATALOG, { key: "custom", label: "Custom requirement", valueType: "text" as const }];
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const customFilter = filters.find((f) => f.key === "custom");
    expect(customFilter).toBeDefined();
    expect(customFilter?.operator).toBe("contains");
    expect(customFilter?.value).toContain("actually sell their own consumer products");
    expect(customFilter?.label).toBeTruthy();
  });

  it("falls back to the unmatched list when the catalog has no custom escape hatch", () => {
    const text = "Find D2C brands. Find companies that actually sell their own consumer products through ecommerce/DTC channels.";
    const catalogWithoutCustom = FULL_CATALOG.filter((d) => d.key !== "custom");
    const { filters, unmatched } = extractFiltersFromDescription(text, catalogWithoutCustom);
    expect(filters.some((f) => f.key === "custom")).toBe(false);
    expect(unmatched.some((u) => u.toLowerCase().includes("consumer products"))).toBe(true);
  });

  it("a sentence whose content was already fully captured by structured rules is never duplicated as a custom filter", () => {
    const catalogWithCustom = [...FULL_CATALOG, { key: "custom", label: "Custom requirement", valueType: "text" as const }];
    const text = "Find D2C brands in the US with 10-300 employees.";
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    expect(filters.some((f) => f.key === "custom")).toBe(false);
  });

  it("generalizes to an unrelated business-model requirement sentence with no hardcoded phrase list", () => {
    const catalogWithCustom = [...FULL_CATALOG, { key: "custom", label: "Custom requirement", valueType: "text" as const }];
    const text = "Find Healthcare brands. Only include companies that operate their own physical clinic locations.";
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const customFilter = filters.find((f) => f.key === "custom");
    expect(customFilter?.value).toContain("own physical clinic locations");
  });
});

describe("the exact reported D2C ICP", () => {
  // Mirrors the reported ICP's structure exactly: D2C positive criterion,
  // explicit employee and revenue ranges, and an exclusion clause naming
  // "SaaS vendors" — "Healthcare" stands in for the exclusion clause's
  // other named category since this extractor's term map (INDUSTRY_TERMS)
  // has no entry for "hospitals" specifically; the reported bug (SaaS
  // leaking from the exclusion into the positive industry filter) is
  // fully exercised regardless of what the OTHER excluded term is.
  const text =
    "Find D2C brands in the US with 50-1,000 employees and $10M-$500M revenue. Exclude Healthcare and SaaS vendors.";

  it("industry is D2C only — SaaS never leaks in from the exclusion clause", () => {
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toEqual(["D2C"]);
  });

  it("employee range is captured as an explicit numeric constraint, not left unmatched", () => {
    const { filters, unmatched } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.employee_range?.value).toEqual({ min: 50, max: 1000 });
    expect(unmatched.some((u) => u.includes("1,000") || u.includes("employees"))).toBe(false);
  });

  it("revenue range is captured as an explicit numeric constraint, not left unmatched", () => {
    const { filters, unmatched } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });
    expect(unmatched.some((u) => u.includes("500M") || u.includes("revenue"))).toBe(false);
  });

  it("exclusions preserve Healthcare and SaaS Vendors as negative constraints", () => {
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.operator).toBe("not_in");
    expect(byKey.exclusions?.value).toEqual(expect.arrayContaining(["Healthcare", "SaaS Vendors"]));
  });

  it("geography is still correctly extracted alongside everything else", () => {
    const { filters } = extractFiltersFromDescription(text, FULL_CATALOG);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.geography?.value).toEqual(["United States"]);
  });
});

describe("the exact reported D2C ICP, full follow-up text (with custom escape hatch)", () => {
  const text =
    "US-based D2C brands, 50–1,000 employees, estimated revenue $10M–$500M. Exclude agencies, consultancies, marketing firms, SaaS vendors, staffing firms, and service providers. Find companies that actually sell their own consumer products through ecommerce/DTC channels.";
  const catalogWithCustom = [...FULL_CATALOG, { key: "custom", label: "Custom requirement", valueType: "text" as const }];

  it("industry is D2C only", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry?.value).toEqual(["D2C"]);
  });

  it("geography is United States", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.geography?.value).toEqual(["United States"]);
  });

  it("employee range is 50-1,000", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.employee_range?.value).toEqual({ min: 50, max: 1000 });
  });

  it("revenue range is $10M-$500M, extracted despite 'revenue' preceding the numbers", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.revenue?.value).toEqual({ min: 10_000_000, max: 500_000_000 });
  });

  it("all six excluded business types are preserved as negative constraints", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.exclusions?.operator).toBe("not_in");
    expect(byKey.exclusions?.value).toEqual([
      "Agencies",
      "Consultancies",
      "Marketing Firms",
      "SaaS Vendors",
      "Staffing Firms",
      "Service Providers",
    ]);
  });

  it("the ecommerce/DTC ownership requirement is preserved as a custom filter, not dropped", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const customFilter = filters.find((f) => f.key === "custom");
    expect(customFilter).toBeDefined();
    expect(customFilter?.value).toContain("actually sell their own consumer products");
    expect(customFilter?.value).toContain("ecommerce/DTC channels");
  });

  it("the complete ICP is usable end to end: every stated requirement maps to a real filter, nothing silently dropped", () => {
    const { filters } = extractFiltersFromDescription(text, catalogWithCustom);
    const byKey = Object.fromEntries(filters.map((f) => [f.key, f]));
    expect(byKey.industry).toBeDefined();
    expect(byKey.geography).toBeDefined();
    expect(byKey.employee_range).toBeDefined();
    expect(byKey.revenue).toBeDefined();
    expect(byKey.exclusions).toBeDefined();
    expect(byKey.custom).toBeDefined();
  });
});
