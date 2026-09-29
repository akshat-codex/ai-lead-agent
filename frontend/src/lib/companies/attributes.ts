import { ApiError } from "@/lib/api-client";
import { getCompanyEvidence, importCompanyEvidence, latestValueForField } from "@/lib/evidence/api";

/** Company-level display attributes, read from real COMPANY evidence only
 * (see collect_company_evidence in backend/app/services/evidence_import.py).
 * Every field is null exactly when no provider ever supplied it — never
 * guessed or inferred client-side. */
export interface CompanyAttributes {
  industry: string | null;
  country: string | null;
  employeeRange: string | null;
  revenueRange: string | null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** Ensures this company's discovery/enrichment facts have been imported as
 * evidence at least once, then reads back the fields the company card wants
 * to show — same "import may legitimately find nothing" honesty as
 * tryGetCompanyLinkedInId in lib/companies/api.ts. */
export async function tryGetCompanyAttributes(companyId: string): Promise<CompanyAttributes> {
  try {
    await importCompanyEvidence(companyId);
  } catch {
    // A failed import just means no evidence will be found below.
  }
  try {
    const records = await getCompanyEvidence(companyId);
    return {
      industry: asString(latestValueForField(records, "industry")),
      country: asString(latestValueForField(records, "country")),
      employeeRange: asString(latestValueForField(records, "employee_range")),
      revenueRange: asString(latestValueForField(records, "revenue_range")),
    };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return { industry: null, country: null, employeeRange: null, revenueRange: null };
    }
    throw error;
  }
}
