import { apiFetch } from "@/lib/api-client";
import type { CustomRuleItem, HardRules, IcpDraft, SavedIcp, SoftPreferences } from "./types";

interface ApiCustomRule {
  label: string;
  description: string;
}

interface ApiHardRules {
  industry: string[];
  geography: string[];
  min_employees: number | null;
  max_employees: number | null;
  allowed_titles: string[];
  company_type: string[];
  exclusions: string[];
  custom_rules: ApiCustomRule[];
}

interface ApiSoftPreferences {
  business_model_preferences: string[];
  commercial_signals: string[];
  growth_signals: string[];
  marketing_signals: string[];
  other_preferences: ApiCustomRule[];
}

interface ApiIcp {
  id: string;
  name: string;
  version: number;
  hard_rules: ApiHardRules;
  soft_preferences: ApiSoftPreferences;
  created_at: string;
}

function stripId(items: CustomRuleItem[]): ApiCustomRule[] {
  return items.map(({ label, description }) => ({ label, description }));
}

function toApiHardRules(hardRules: HardRules): ApiHardRules {
  return {
    industry: hardRules.industry,
    geography: hardRules.geography,
    min_employees: hardRules.minEmployees,
    max_employees: hardRules.maxEmployees,
    allowed_titles: hardRules.allowedTitles,
    company_type: hardRules.companyType,
    exclusions: hardRules.exclusions,
    custom_rules: stripId(hardRules.customRules),
  };
}

function toApiSoftPreferences(softPreferences: SoftPreferences): ApiSoftPreferences {
  return {
    business_model_preferences: softPreferences.businessModelPreferences,
    commercial_signals: softPreferences.commercialSignals,
    growth_signals: softPreferences.growthSignals,
    marketing_signals: softPreferences.marketingSignals,
    other_preferences: stripId(softPreferences.otherPreferences),
  };
}

function withLocalIds(items: ApiCustomRule[]): CustomRuleItem[] {
  return items.map((item, index) => ({
    id: `${index}-${item.label}`,
    label: item.label,
    description: item.description,
  }));
}

function fromApiIcp(api: ApiIcp): SavedIcp {
  return {
    id: api.id,
    version: api.version,
    createdAt: api.created_at,
    name: api.name,
    hardRules: {
      industry: api.hard_rules.industry,
      geography: api.hard_rules.geography,
      minEmployees: api.hard_rules.min_employees,
      maxEmployees: api.hard_rules.max_employees,
      allowedTitles: api.hard_rules.allowed_titles,
      companyType: api.hard_rules.company_type,
      exclusions: api.hard_rules.exclusions,
      customRules: withLocalIds(api.hard_rules.custom_rules),
    },
    softPreferences: {
      businessModelPreferences: api.soft_preferences.business_model_preferences,
      commercialSignals: api.soft_preferences.commercial_signals,
      growthSignals: api.soft_preferences.growth_signals,
      marketingSignals: api.soft_preferences.marketing_signals,
      otherPreferences: withLocalIds(api.soft_preferences.other_preferences),
    },
  };
}

export async function saveIcp(draft: IcpDraft): Promise<SavedIcp> {
  const api = await apiFetch<ApiIcp>("/api/v1/icps", {
    method: "POST",
    body: {
      name: draft.name.trim(),
      hard_rules: toApiHardRules(draft.hardRules),
      soft_preferences: toApiSoftPreferences(draft.softPreferences),
    },
  });
  return fromApiIcp(api);
}

export async function listIcps(): Promise<SavedIcp[]> {
  const items = await apiFetch<ApiIcp[]>("/api/v1/icps");
  return items.map(fromApiIcp);
}

export async function getIcp(id: string): Promise<SavedIcp> {
  const api = await apiFetch<ApiIcp>(`/api/v1/icps/${id}`);
  return fromApiIcp(api);
}
