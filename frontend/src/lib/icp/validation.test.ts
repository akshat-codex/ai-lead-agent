import { describe, expect, it } from "vitest";
import { createEmptyIcpDraft } from "./types";
import { isIcpDraftValid, validateIcpDraft } from "./validation";
import type { IcpDraft } from "./types";

function validDraft(): IcpDraft {
  const draft = createEmptyIcpDraft();
  draft.name = "D2C Skincare";
  draft.hardRules.industry = ["Skincare"];
  draft.hardRules.geography = ["United States"];
  draft.hardRules.minEmployees = 10;
  draft.hardRules.maxEmployees = 200;
  return draft;
}

describe("validateIcpDraft", () => {
  it("accepts a fully valid draft", () => {
    const errors = validateIcpDraft(validDraft());
    expect(isIcpDraftValid(errors)).toBe(true);
  });

  it("requires a name", () => {
    const draft = validDraft();
    draft.name = "   ";
    const errors = validateIcpDraft(draft);
    expect(errors.name).toBeDefined();
    expect(isIcpDraftValid(errors)).toBe(false);
  });

  it("rejects minEmployees greater than maxEmployees", () => {
    const draft = validDraft();
    draft.hardRules.minEmployees = 500;
    draft.hardRules.maxEmployees = 50;
    const errors = validateIcpDraft(draft);
    expect(errors.hardRules.employeeRange).toBeDefined();
    expect(isIcpDraftValid(errors)).toBe(false);
  });

  it("allows equal min and max employees", () => {
    const draft = validDraft();
    draft.hardRules.minEmployees = 50;
    draft.hardRules.maxEmployees = 50;
    const errors = validateIcpDraft(draft);
    expect(errors.hardRules.employeeRange).toBeUndefined();
  });

  it("rejects a completely empty ICP", () => {
    const errors = validateIcpDraft(createEmptyIcpDraft());
    expect(errors.general).toBeDefined();
    expect(isIcpDraftValid(errors)).toBe(false);
  });

  it("treats hard-rules-only as non-empty", () => {
    const draft = createEmptyIcpDraft();
    draft.name = "Hard Only";
    draft.hardRules.industry = ["Fintech"];
    const errors = validateIcpDraft(draft);
    expect(errors.general).toBeUndefined();
  });

  it("treats soft-preferences-only as non-empty", () => {
    const draft = createEmptyIcpDraft();
    draft.name = "Soft Only";
    draft.softPreferences.growthSignals = ["Recently raised a Series A"];
    const errors = validateIcpDraft(draft);
    expect(errors.general).toBeUndefined();
  });

  it("rejects duplicate geography entries case-insensitively", () => {
    const draft = validDraft();
    draft.hardRules.geography = ["United States", "united states"];
    const errors = validateIcpDraft(draft);
    expect(errors.hardRules.geography).toBeDefined();
  });

  it("rejects exclusions that contradict allowed industry values", () => {
    const draft = validDraft();
    draft.hardRules.industry = ["Healthcare"];
    draft.hardRules.exclusions = ["healthcare"];
    const errors = validateIcpDraft(draft);
    expect(errors.hardRules.exclusions).toBeDefined();
  });

  it("allows exclusions that do not overlap allowed values", () => {
    const draft = validDraft();
    draft.hardRules.exclusions = ["Wholesale-only brands"];
    const errors = validateIcpDraft(draft);
    expect(errors.hardRules.exclusions).toBeUndefined();
  });

  it("rejects a custom rule with an empty label or description", () => {
    const draft = validDraft();
    draft.hardRules.customRules = [{ id: "1", label: "", description: "Something" }];
    const errors = validateIcpDraft(draft);
    expect(errors.hardRules.customRules?.["1"].label).toBeDefined();
  });

  it("rejects an 'other preference' with an empty description", () => {
    const draft = validDraft();
    draft.softPreferences.otherPreferences = [{ id: "1", label: "Prefers", description: "" }];
    const errors = validateIcpDraft(draft);
    expect(errors.softPreferences.otherPreferences?.["1"].description).toBeDefined();
  });

  it("keeps hard-rule and soft-preference errors in separate namespaces", () => {
    const draft = validDraft();
    draft.hardRules.customRules = [{ id: "h1", label: "", description: "x" }];
    draft.softPreferences.otherPreferences = [{ id: "s1", label: "y", description: "" }];
    const errors = validateIcpDraft(draft);

    expect(errors.hardRules.customRules).toBeDefined();
    expect(errors.hardRules.customRules?.["s1"]).toBeUndefined();
    expect(errors.softPreferences.otherPreferences).toBeDefined();
    expect(errors.softPreferences.otherPreferences?.["h1"]).toBeUndefined();
  });
});
