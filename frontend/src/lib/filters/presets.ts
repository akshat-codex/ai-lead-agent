import { newClientId } from "./types";
import type { FilterCriterion, FilterValue } from "./types";

/**
 * Starting points for the Manual Builder — every value here is just a normal
 * FilterCriterion the user can edit or remove afterward. Presets are
 * examples, not a closed list: the builder itself has no hardcoded notion of
 * "industries" beyond whatever the backend filter catalog (see
 * lib/filters/api.ts) declares, so adding/removing a preset here never
 * requires touching the builder.
 */
export interface SearchPreset {
  id: string;
  label: string;
  description: string;
  criteria: { key: string; operator: FilterCriterion["operator"]; value: FilterValue }[];
}

export const SEARCH_PRESETS: SearchPreset[] = [
  {
    id: "b2b-saas",
    label: "B2B SaaS",
    description: "Mid-market SaaS companies, common decision-maker titles.",
    criteria: [
      { key: "industry", operator: "in", value: ["SaaS"] },
      { key: "employee_range", operator: "range", value: { min: 50, max: 500 } },
      { key: "allowed_titles", operator: "in", value: ["Founder", "VP Marketing", "Head of Growth"] },
    ],
  },
  {
    id: "d2c-brand",
    label: "D2C Brand",
    description: "Direct-to-consumer brands with a marketing-led buying motion.",
    criteria: [
      { key: "industry", operator: "in", value: ["D2C"] },
      { key: "employee_range", operator: "range", value: { min: 10, max: 300 } },
      { key: "allowed_titles", operator: "in", value: ["Founder", "CMO", "Marketing Head"] },
    ],
  },
  {
    id: "healthcare",
    label: "Healthcare",
    description: "Healthcare organizations, compliance-aware sizing.",
    criteria: [
      { key: "industry", operator: "in", value: ["Healthcare"] },
      { key: "employee_range", operator: "range", value: { min: 100, max: 2000 } },
      { key: "allowed_titles", operator: "in", value: ["CEO", "Director of Marketing"] },
    ],
  },
  {
    id: "enterprise",
    label: "Enterprise",
    description: "Large organizations with longer sales cycles.",
    criteria: [
      { key: "employee_range", operator: "range", value: { min: 1000, max: null } },
      { key: "allowed_titles", operator: "in", value: ["VP Marketing", "Director of Marketing"] },
    ],
  },
];

export function presetToCriteria(preset: SearchPreset): FilterCriterion[] {
  return preset.criteria.map((c) => ({
    id: newClientId(),
    key: c.key,
    operator: c.operator,
    value: c.value,
    source: "manual",
  }));
}
