import { describe, expect, it } from "vitest";
import { MAX_INITIAL_TARGET_COUNT, parseTargetCount } from "./BatchDiscoveryTrigger";

describe("parseTargetCount", () => {
  it("respects exactly what the user typed, within the controlled-batch cap", () => {
    expect(parseTargetCount("5")).toBe(5);
    expect(parseTargetCount("20")).toBe(20);
    expect(parseTargetCount(String(MAX_INITIAL_TARGET_COUNT))).toBe(MAX_INITIAL_TARGET_COUNT);
  });

  it("clamps any value above the controlled-batch cap down to the cap, never rejecting the input", () => {
    expect(parseTargetCount(String(MAX_INITIAL_TARGET_COUNT + 1))).toBe(MAX_INITIAL_TARGET_COUNT);
    expect(parseTargetCount("1000")).toBe(MAX_INITIAL_TARGET_COUNT);
  });

  it("returns undefined for empty, zero, negative, or non-numeric input", () => {
    expect(parseTargetCount("")).toBeUndefined();
    expect(parseTargetCount("0")).toBeUndefined();
    expect(parseTargetCount("-5")).toBeUndefined();
    expect(parseTargetCount("abc")).toBeUndefined();
  });
});

describe("MAX_INITIAL_TARGET_COUNT", () => {
  it("is a finite controlled-batch ceiling, not an effectively-unlimited value", () => {
    expect(MAX_INITIAL_TARGET_COUNT).toBe(50);
    expect(MAX_INITIAL_TARGET_COUNT).toBeGreaterThan(0);
    expect(MAX_INITIAL_TARGET_COUNT).toBeLessThanOrEqual(100);
  });
});
