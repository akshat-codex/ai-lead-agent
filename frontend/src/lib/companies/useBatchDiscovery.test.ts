import { describe, expect, it } from "vitest";
import { ADD_MORE_LEADS_INCREMENT, DEFAULT_TARGET_COUNT, phaseAfterRound } from "./useBatchDiscovery";
import type { Batch } from "@/lib/batch/api";

function batch(overrides: Partial<Batch> = {}): Batch {
  return {
    id: "batch-1",
    icpId: "icp-1",
    icpVersion: 1,
    requestedTargetCount: 10,
    discoveryLimit: 20,
    peopleLimitPerCompany: 5,
    discoveryMode: "fast",
    status: "COMPLETED",
    discoveredCount: 5,
    deduplicatedLeadCount: 5,
    acceptedCount: 2,
    heldCount: 3,
    rejectedCount: 0,
    duplicateCount: 0,
    failedCount: 0,
    discoveryExhaustedProviders: [],
    discoveryErrorCode: null,
    discoveryRoundsRun: 1,
    discoveryPoolExhausted: false,
    startedAt: "2026-01-01T00:00:00Z",
    completedAt: "2026-01-01T00:00:01Z",
    hermesJobId: null,
    hermesJobStatus: null,
    providerCallCounts: {},
    estimatedExploriumCredits: null,
    ...overrides,
  };
}

describe("phaseAfterRound", () => {
  it("returns provider-error when the batch has a non-retryable discovery error and no Hermes job is pending", () => {
    expect(phaseAfterRound(batch({ discoveryErrorCode: "EXPLORIUM_CREDITS_EXHAUSTED", discoveryPoolExhausted: true }), null)).toBe(
      "provider-error",
    );
  });

  it("returns hermes-searching when Explorium errored but a Hermes job is still queued/running", () => {
    // The core fix: Explorium being unavailable must never present as a
    // dead-end failure while Hermes (app/providers/hermes.py) is still
    // genuinely working on this batch — see backend's
    // _maybe_advance_hermes_job, called unconditionally after the
    // Explorium round loop regardless of discoveryErrorCode.
    expect(
      phaseAfterRound(batch({ discoveryErrorCode: "EXPLORIUM_CREDITS_EXHAUSTED", hermesJobId: "job-1", hermesJobStatus: "queued" }), null),
    ).toBe("hermes-searching");
    expect(
      phaseAfterRound(batch({ discoveryErrorCode: "EXPLORIUM_AUTH_FAILED", hermesJobId: "job-2", hermesJobStatus: "running" }), null),
    ).toBe("hermes-searching");
  });

  it("returns provider-error (not hermes-searching) once the Hermes job has resolved and hermesJobId is cleared", () => {
    // A resolved job (completed/failed/timeout/...) clears hermesJobId on
    // the backend (see _maybe_advance_hermes_job) — if Explorium's error
    // still stands and Hermes has nothing further pending, this must
    // correctly fall back to the honest provider-error state, never stay
    // stuck showing "still searching" forever.
    expect(phaseAfterRound(batch({ discoveryErrorCode: "EXPLORIUM_CREDITS_EXHAUSTED", hermesJobId: null, hermesJobStatus: "completed" }), null)).toBe(
      "provider-error",
    );
  });

  it("hermes-searching never depends on the round-limit-reached code — that stays its own distinct phase", () => {
    // DISCOVERY_ROUND_LIMIT_REACHED is a backend safety stop, not an
    // Explorium-unavailable condition — even with a Hermes job pending,
    // this must still resolve to round-limit-reached first (checked
    // before hermes-searching in phaseAfterRound).
    expect(
      phaseAfterRound(batch({ discoveryErrorCode: "DISCOVERY_ROUND_LIMIT_REACHED", hermesJobId: "job-1", hermesJobStatus: "queued" }), null),
    ).toBe("round-limit-reached");
  });

  it("returns target-reached only once acceptedCount meets the target — held/rejected never count", () => {
    expect(phaseAfterRound(batch({ acceptedCount: 2 }), 5)).toBe("ready");
    expect(phaseAfterRound(batch({ acceptedCount: 5 }), 5)).toBe("target-reached");
    expect(phaseAfterRound(batch({ acceptedCount: 6 }), 5)).toBe("target-reached");
  });

  it("ignores target-reached logic entirely when no target was requested", () => {
    expect(phaseAfterRound(batch({ acceptedCount: 1000 }), null)).toBe("ready");
  });

  it("returns exhausted when the provider pool is exhausted and no target blocks it first", () => {
    expect(phaseAfterRound(batch({ discoveryPoolExhausted: true }), null)).toBe("exhausted");
  });

  it("prefers target-reached over exhausted when both are true", () => {
    expect(phaseAfterRound(batch({ acceptedCount: 5, discoveryPoolExhausted: true }), 5)).toBe("target-reached");
  });

  it("returns ready when nothing terminal has happened yet", () => {
    expect(phaseAfterRound(batch({ acceptedCount: 1, discoveryPoolExhausted: false, discoveryErrorCode: null }), 10)).toBe("ready");
  });

  // --- controlled-batch safety limit (never loop indefinitely) ----------

  it("returns the dedicated round-limit-reached phase for the backend's own safety stop, never provider-error", () => {
    expect(phaseAfterRound(batch({ discoveryErrorCode: "DISCOVERY_ROUND_LIMIT_REACHED" }), 10)).toBe("round-limit-reached");
  });

  it("still distinguishes a real provider error from the round-limit safety stop", () => {
    expect(phaseAfterRound(batch({ discoveryErrorCode: "EXPLORIUM_AUTH_FAILED" }), 10)).toBe("provider-error");
    expect(phaseAfterRound(batch({ discoveryErrorCode: "EXPLORIUM_CREDITS_EXHAUSTED" }), 10)).toBe("provider-error");
  });

  it("round-limit-reached takes priority even if target/pool-exhaustion conditions also happen to be true", () => {
    expect(
      phaseAfterRound(
        batch({ discoveryErrorCode: "DISCOVERY_ROUND_LIMIT_REACHED", acceptedCount: 10, discoveryPoolExhausted: true }),
        10,
      ),
    ).toBe("round-limit-reached");
  });
});

describe("controlled-batch defaults", () => {
  it("defaults every initial search to a small, finite target count", () => {
    expect(DEFAULT_TARGET_COUNT).toBe(10);
    expect(DEFAULT_TARGET_COUNT).toBeGreaterThan(0);
    expect(DEFAULT_TARGET_COUNT).toBeLessThanOrEqual(50); // sanity bound — never "effectively unlimited"
  });

  it("Add More Leads increments by a small, finite amount, never re-requesting an unbounded target", () => {
    expect(ADD_MORE_LEADS_INCREMENT).toBe(10);
    expect(ADD_MORE_LEADS_INCREMENT).toBeGreaterThan(0);
    expect(ADD_MORE_LEADS_INCREMENT).toBeLessThanOrEqual(50);
  });
});
