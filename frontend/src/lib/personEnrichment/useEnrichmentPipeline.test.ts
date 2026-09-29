import { describe, expect, it } from "vitest";
import { statusFromRun } from "./useEnrichmentPipeline";
import { latestValueForField } from "@/lib/evidence/api";
import type { EvidenceRecord } from "@/lib/evidence/types";

function record(field: string, value: unknown, retrievedAt: string): EvidenceRecord {
  return {
    id: `${field}-${retrievedAt}`,
    entityType: "PERSON",
    entityId: "person-1",
    field,
    value,
    sourceProviderId: "apollo-person-enrichment-v1",
    sourceType: "provider",
    externalId: null,
    retrievedAt,
    confidence: "UNKNOWN",
  };
}

describe("statusFromRun", () => {
  it("maps every backend run status to the correct UI status", () => {
    expect(statusFromRun("COMPLETED")).toBe("enriched");
    expect(statusFromRun("PARTIAL_FAILURE")).toBe("partial");
    expect(statusFromRun("FAILED")).toBe("failed");
    expect(statusFromRun("UNAVAILABLE")).toBe("unavailable");
  });

  it("falls back to failed for an unrecognized status rather than pretending success", () => {
    expect(statusFromRun("SOMETHING_NEW")).toBe("failed");
  });
});

describe("latestValueForField", () => {
  it("returns null when the field was never observed", () => {
    expect(latestValueForField([], "email")).toBeNull();
    expect(latestValueForField([record("title", "CMO", "2026-01-01T00:00:00Z")], "email")).toBeNull();
  });

  it("returns the most recently retrieved value when multiple observations exist", () => {
    const records = [
      record("email", "old@x.invalid", "2026-01-01T00:00:00Z"),
      record("email", "new@x.invalid", "2026-01-02T00:00:00Z"),
    ];
    expect(latestValueForField(records, "email")).toBe("new@x.invalid");
  });

  it("only considers records matching the requested field", () => {
    const records = [record("title", "CMO", "2026-01-01T00:00:00Z"), record("email", "a@x.invalid", "2026-01-02T00:00:00Z")];
    expect(latestValueForField(records, "title")).toBe("CMO");
  });
});
