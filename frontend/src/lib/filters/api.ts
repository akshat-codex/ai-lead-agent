import { apiFetch, ApiError } from "@/lib/api-client";
import type { FilterCriterion, FilterDefinition, FilterOperator, FilterValueType } from "./types";

interface ApiFilterDefinition {
  key: string;
  display_label: string;
  description: string;
  category: "hard" | "soft";
  value_type: string;
  allowed_operators: string[];
  options: string[] | null;
  has_specialized_ui: boolean;
}

const VALUE_TYPE_MAP: Record<string, FilterValueType> = {
  multi_select: "multi-select",
  single_select: "single-select",
  range: "number-range",
  text: "text",
  boolean: "boolean",
  enum: "single-select",
};

const OPERATOR_MAP: Record<string, FilterOperator> = {
  equals: "equals",
  not_equals: "equals",
  in: "in",
  not_in: "not_in",
  contains: "contains",
  range: "range",
  gte: "gte",
  lte: "lte",
  exists: "exists",
};

function fromApiFilterDefinition(api: ApiFilterDefinition): FilterDefinition {
  return {
    key: api.key,
    label: api.display_label,
    description: api.description,
    valueType: VALUE_TYPE_MAP[api.value_type] ?? "text",
    options: api.options?.map((value) => ({ value, label: value })),
    operators: api.allowed_operators.map((op) => OPERATOR_MAP[op] ?? "equals"),
    hasSpecializedUi: api.has_specialized_ui,
  };
}

/**
 * The 6 fields the ICP schema has always supported, used only as a fallback
 * when the backend filter-catalog endpoint (GET /api/v1/icps/filter-catalog)
 * isn't reachable - keeps Step 1 usable/demoable independent of backend
 * timing. Not a permanent second source of truth for the catalog.
 */
export const FALLBACK_KNOWN_FILTERS: FilterDefinition[] = [
  { key: "industry", label: "Industry", description: "Allowed industries.", valueType: "multi-select", operators: ["in"], hasSpecializedUi: true },
  { key: "geography", label: "Location", description: "Allowed countries/regions.", valueType: "multi-select", operators: ["in"], hasSpecializedUi: true },
  { key: "employee_range", label: "Employees", description: "Employee headcount range.", valueType: "number-range", operators: ["range"], hasSpecializedUi: true },
  { key: "allowed_titles", label: "Titles / Decision makers", description: "Allowed job titles for the decision-maker being evaluated.", valueType: "multi-select", operators: ["in"], hasSpecializedUi: true },
  { key: "company_type", label: "Company type", description: "Allowed company types (e.g. D2C, B2B).", valueType: "multi-select", operators: ["in"], hasSpecializedUi: true },
  { key: "exclusions", label: "Exclusions", description: "Companies/domains/terms to always reject.", valueType: "multi-select", operators: ["not_in"], hasSpecializedUi: true },
  { key: "custom", label: "Custom requirement", description: "Any requirement with no dedicated filter yet.", valueType: "text", operators: ["contains"], hasSpecializedUi: true },
];

export interface FilterCatalogResult {
  definitions: FilterDefinition[];
  /** True when the real backend catalog wasn't reachable and the fallback was used. */
  isFallback: boolean;
}

export async function getFilterCatalog(): Promise<FilterCatalogResult> {
  try {
    const items = await apiFetch<ApiFilterDefinition[]>("/api/v1/icps/filter-catalog");
    return { definitions: items.map(fromApiFilterDefinition), isFallback: false };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return { definitions: FALLBACK_KNOWN_FILTERS, isFallback: true };
    }
    throw error;
  }
}

interface ApiFilterCriterion {
  key: string;
  operator: string;
  value: unknown;
  label: string | null;
}

interface IcpCreatePayload {
  name: string;
  filters: ApiFilterCriterion[];
}

/**
 * Translates the frontend's generic FilterCriterion[] into the backend's
 * ICPCreate.filters[] wire shape (see backend/app/schemas/filter_criterion.py).
 * This is the ONLY function allowed to know the current backend ICP wire
 * shape - if that shape ever changes again, only this function's body needs
 * updating.
 */
export function toIcpCreatePayload(criteria: FilterCriterion[], name: string): IcpCreatePayload {
  return {
    name: name.trim(),
    filters: criteria.map((c) => ({
      key: c.key,
      operator: c.operator,
      value: c.value,
      label: c.label ?? null,
    })),
  };
}

export async function createIcpFromCriteria(criteria: FilterCriterion[], name: string) {
  return apiFetch<{ id: string; name: string; version: number }>("/api/v1/icps", {
    method: "POST",
    body: toIcpCreatePayload(criteria, name),
  });
}
