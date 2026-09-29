import { describe, expect, it } from "vitest";
import { rankTierToUiTier, type RankTier } from "./FitBadge";

describe("rankTierToUiTier", () => {
  it("maps ACCEPTED and QUALIFIED_STRONG to strong", () => {
    expect(rankTierToUiTier("ACCEPTED")).toBe("strong");
    expect(rankTierToUiTier("QUALIFIED_STRONG")).toBe("strong");
  });

  it("maps QUALIFIED_WEAK to good", () => {
    expect(rankTierToUiTier("QUALIFIED_WEAK")).toBe("good");
  });

  it("maps HOLD, REJECTED, and DUPLICATE to weak", () => {
    expect(rankTierToUiTier("HOLD")).toBe("weak");
    expect(rankTierToUiTier("REJECTED")).toBe("weak");
    expect(rankTierToUiTier("DUPLICATE")).toBe("weak");
  });

  it("maps HARD_FAILED to its own distinct 'rejected' tier, never 'weak'", () => {
    // Regression test: a company whose hard-rule validation FAILED was
    // previously silently collapsed into the same "Weak fit" bucket as a
    // HOLD/REJECTED/DUPLICATE lead via the switch's `default` case,
    // producing a real, reported bug — a company card labeled "Weak fit"
    // next to detail text reading "Hard ICP validation failed; rejected
    // without LLM involvement."
    expect(rankTierToUiTier("HARD_FAILED")).toBe("rejected");
    expect(rankTierToUiTier("HARD_FAILED")).not.toBe("weak");
  });

  it("never maps any tier to 'rejected' except HARD_FAILED", () => {
    const nonHardFailedTiers: RankTier[] = ["ACCEPTED", "QUALIFIED_STRONG", "QUALIFIED_WEAK", "HOLD", "REJECTED", "DUPLICATE"];
    for (const tier of nonHardFailedTiers) {
      expect(rankTierToUiTier(tier)).not.toBe("rejected");
    }
  });
});
