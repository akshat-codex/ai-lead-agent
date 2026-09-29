import type { CustomRuleItem, IcpDraft } from "./types";

export interface CustomRuleErrors {
  [itemId: string]: { label?: string; description?: string };
}

export interface IcpValidationErrors {
  name?: string;
  general?: string;
  hardRules: {
    employeeRange?: string;
    geography?: string;
    exclusions?: string;
    customRules?: CustomRuleErrors;
  };
  softPreferences: {
    otherPreferences?: CustomRuleErrors;
  };
}

function validateCustomRules(items: CustomRuleItem[]): CustomRuleErrors | undefined {
  const errors: CustomRuleErrors = {};

  for (const item of items) {
    const itemErrors: { label?: string; description?: string } = {};
    if (!item.label.trim()) itemErrors.label = "Label is required.";
    if (!item.description.trim()) itemErrors.description = "Description is required.";
    if (itemErrors.label || itemErrors.description) errors[item.id] = itemErrors;
  }

  return Object.keys(errors).length > 0 ? errors : undefined;
}

function isHardRulesEmpty(draft: IcpDraft): boolean {
  const h = draft.hardRules;
  return !(
    h.industry.length ||
    h.geography.length ||
    h.minEmployees !== null ||
    h.maxEmployees !== null ||
    h.allowedTitles.length ||
    h.companyType.length ||
    h.exclusions.length ||
    h.customRules.length
  );
}

function isSoftPreferencesEmpty(draft: IcpDraft): boolean {
  const s = draft.softPreferences;
  return !(
    s.businessModelPreferences.length ||
    s.commercialSignals.length ||
    s.growthSignals.length ||
    s.marketingSignals.length ||
    s.otherPreferences.length
  );
}

/**
 * Pure, synchronous validation of an ICP draft. Mirrors the rules enforced
 * server-side in backend/app/schemas/icp.py so the UI can give immediate
 * feedback before a save attempt round-trips to the API.
 */
export function validateIcpDraft(draft: IcpDraft): IcpValidationErrors {
  const errors: IcpValidationErrors = { hardRules: {}, softPreferences: {} };
  const { hardRules, softPreferences } = draft;

  if (!draft.name.trim()) {
    errors.name = "Name is required.";
  }

  if (hardRules.minEmployees !== null && hardRules.maxEmployees !== null) {
    if (hardRules.minEmployees > hardRules.maxEmployees) {
      errors.hardRules.employeeRange = "Minimum employees cannot be greater than maximum employees.";
    }
  }

  if (isHardRulesEmpty(draft) && isSoftPreferencesEmpty(draft)) {
    errors.general = "An ICP must include at least one hard rule or soft preference.";
  }

  const geographyLower = hardRules.geography.map((g) => g.trim().toLowerCase()).filter(Boolean);
  if (new Set(geographyLower).size !== geographyLower.length) {
    errors.hardRules.geography = "Geography list contains duplicate/contradictory entries.";
  }

  const exclusionsLower = new Set(
    hardRules.exclusions.map((e) => e.trim().toLowerCase()).filter(Boolean)
  );
  if (exclusionsLower.size > 0) {
    const allowedTerms = [
      ...hardRules.industry,
      ...hardRules.geography,
      ...hardRules.allowedTitles,
      ...hardRules.companyType,
    ]
      .map((v) => v.trim().toLowerCase())
      .filter(Boolean);

    const conflicts = allowedTerms.filter((term) => exclusionsLower.has(term));
    if (conflicts.length > 0) {
      errors.hardRules.exclusions = `Exclusions contradict allowed values: ${Array.from(
        new Set(conflicts)
      ).join(", ")}.`;
    }
  }

  const customRuleErrors = validateCustomRules(hardRules.customRules);
  if (customRuleErrors) errors.hardRules.customRules = customRuleErrors;

  const otherPreferenceErrors = validateCustomRules(softPreferences.otherPreferences);
  if (otherPreferenceErrors) errors.softPreferences.otherPreferences = otherPreferenceErrors;

  return errors;
}

export function isIcpDraftValid(errors: IcpValidationErrors): boolean {
  return !(
    errors.name ||
    errors.general ||
    errors.hardRules.employeeRange ||
    errors.hardRules.geography ||
    errors.hardRules.exclusions ||
    errors.hardRules.customRules ||
    errors.softPreferences.otherPreferences
  );
}

/** The earliest builder step (0 = hard rules, 1 = soft preferences) containing an error, if any. */
export function firstInvalidStep(errors: IcpValidationErrors): number | null {
  if (
    errors.hardRules.employeeRange ||
    errors.hardRules.geography ||
    errors.hardRules.exclusions ||
    errors.hardRules.customRules
  ) {
    return 0;
  }
  if (errors.softPreferences.otherPreferences) return 1;
  return null;
}

/** Flattens all current errors into human-readable messages, for a review-step summary. */
export function collectErrorMessages(errors: IcpValidationErrors): string[] {
  const messages: string[] = [];
  if (errors.name) messages.push(errors.name);
  if (errors.general) messages.push(errors.general);
  if (errors.hardRules.employeeRange) messages.push(errors.hardRules.employeeRange);
  if (errors.hardRules.geography) messages.push(errors.hardRules.geography);
  if (errors.hardRules.exclusions) messages.push(errors.hardRules.exclusions);
  if (errors.hardRules.customRules) {
    messages.push("One or more custom rules are missing a label or description.");
  }
  if (errors.softPreferences.otherPreferences) {
    messages.push("One or more other preferences are missing a label or description.");
  }
  return messages;
}
