import { describe, expect, it } from "vitest";
import { countLabel, ERROR_MESSAGES } from "./FindMoreLeadsButton";

describe("countLabel", () => {
  it("shows the accepted/target ratio when a target count is set", () => {
    expect(countLabel(20, 7, 50)).toBe("7 of 50 qualified leads found");
  });

  it("falls back to a plain result count when no target was requested", () => {
    expect(countLabel(3, 3, null)).toBe("3 leads found");
  });

  it("uses singular phrasing for exactly one result", () => {
    expect(countLabel(1, 1, null)).toBe("1 lead found");
  });
});

describe("ERROR_MESSAGES", () => {
  it("has a distinct, actionable message for credits exhaustion vs auth failure", () => {
    expect(ERROR_MESSAGES.EXPLORIUM_CREDITS_EXHAUSTED).toContain("credits");
    expect(ERROR_MESSAGES.EXPLORIUM_AUTH_FAILED).toContain("API key");
    expect(ERROR_MESSAGES.EXPLORIUM_CREDITS_EXHAUSTED).not.toBe(ERROR_MESSAGES.EXPLORIUM_AUTH_FAILED);
  });
});
