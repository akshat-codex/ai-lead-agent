/**
 * Generic, extensible filter/search-criterion model. Deliberately decoupled
 * from the backend's fixed hard_rules/soft_preferences fields — new
 * backend-supported filters should appear here without any frontend
 * redesign, driven entirely by the filter catalog (see api.ts).
 */

export type FilterValueType =
  | "text"
  | "multi-select"
  | "single-select"
  | "number-range"
  | "boolean";

export type FilterOperator =
  | "equals"
  | "contains"
  | "in"
  | "not_in"
  | "range"
  | "gte"
  | "lte"
  | "exists";

export interface FilterOption {
  value: string;
  label: string;
}

/** What the backend catalog says about one supported filter key. */
export interface FilterDefinition {
  key: string;
  label: string;
  description?: string;
  valueType: FilterValueType;
  options?: FilterOption[];
  operators?: FilterOperator[];
  /** True for filters with a bespoke frontend control (see knownControls.tsx). */
  hasSpecializedUi?: boolean;
}

export type FilterValue =
  | string
  | string[]
  | { min: number | null; max: number | null }
  | boolean;

/** One filter as it exists in the structured search definition. */
export interface FilterCriterion {
  /** Client-only, for list rendering/removal. Never sent to the API. */
  id: string;
  key: string;
  operator: FilterOperator;
  value: FilterValue;
  /** Optional human-readable override — required when key === "custom". */
  label?: string;
  source: "manual" | "nl_extraction";
}

export interface ExtractedFilter extends FilterCriterion {
  source: "nl_extraction";
  /** Heuristic extraction never claims higher confidence than "low". */
  confidence: "low";
  /** The substring of the description that produced this filter. */
  matchedText?: string;
}

/** The full structured search definition — source of truth once confirmed. */
export interface SearchDefinition {
  description: string;
  criteria: FilterCriterion[];
}

export const CUSTOM_FILTER_KEY = "custom";

let nextClientId = 0;
export function newClientId(): string {
  nextClientId += 1;
  return `criterion-${nextClientId}-${Math.floor(Math.random() * 1_000_000)}`;
}
