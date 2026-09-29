/**
 * Frontend ICP draft model.
 *
 * This is the UI's working shape only. It is deliberately kept separate from
 * the future canonical normalized ICP that Phase 2 (ICP Normalization) will
 * produce — this draft is what a user edits in the builder; the API payload
 * derived from it (see api.ts) is what Phase 2 will eventually consume and
 * transform.
 */

export interface CustomRuleItem {
  /** Client-side only, used for list rendering/removal. Never sent to the API. */
  id: string;
  label: string;
  description: string;
}

export interface HardRules {
  industry: string[];
  geography: string[];
  minEmployees: number | null;
  maxEmployees: number | null;
  allowedTitles: string[];
  companyType: string[];
  exclusions: string[];
  customRules: CustomRuleItem[];
}

export interface SoftPreferences {
  businessModelPreferences: string[];
  commercialSignals: string[];
  growthSignals: string[];
  marketingSignals: string[];
  otherPreferences: CustomRuleItem[];
}

export interface IcpDraft {
  name: string;
  hardRules: HardRules;
  softPreferences: SoftPreferences;
}

/** An ICP draft that has been saved and assigned an id/version by the backend. */
export interface SavedIcp extends IcpDraft {
  id: string;
  version: number;
  createdAt: string;
}

export function createEmptyHardRules(): HardRules {
  return {
    industry: [],
    geography: [],
    minEmployees: null,
    maxEmployees: null,
    allowedTitles: [],
    companyType: [],
    exclusions: [],
    customRules: [],
  };
}

export function createEmptySoftPreferences(): SoftPreferences {
  return {
    businessModelPreferences: [],
    commercialSignals: [],
    growthSignals: [],
    marketingSignals: [],
    otherPreferences: [],
  };
}

export function createEmptyIcpDraft(): IcpDraft {
  return {
    name: "",
    hardRules: createEmptyHardRules(),
    softPreferences: createEmptySoftPreferences(),
  };
}
