/**
 * Local, static autocomplete suggestions for the Manual Builder's searchable
 * filter fields (Location, Industry, Company type).
 *
 * These are NOT a canonical taxonomy from Explorium, the backend filter
 * catalog, or any other provider — the backend filter catalog deliberately
 * leaves industry/geography/company_type as free-form (`options: null`, see
 * backend/app/services/filter_catalog.py), and Explorium itself has no
 * closed taxonomy for industry or company type either (it resolves each
 * term live against its own autocomplete API — see
 * backend/app/providers/explorium.py). So this list exists purely to make
 * typing in the UI faster; every value stays a normal free-text string the
 * user can edit or remove, and typing something not in this list is always
 * accepted as-is (see FilterControl's `freeSolo` usage).
 */

// A standard list of country/territory names — not Explorium- or
// backend-specific, just the common English short names used for the
// "Location" filter's suggestions.
export const LOCATION_SUGGESTIONS: string[] = [
  "Afghanistan", "Albania", "Algeria", "Argentina", "Armenia", "Australia", "Austria",
  "Azerbaijan", "Bahrain", "Bangladesh", "Belarus", "Belgium", "Bolivia", "Bosnia and Herzegovina",
  "Brazil", "Bulgaria", "Cambodia", "Cameroon", "Canada", "Chile", "China", "Colombia",
  "Costa Rica", "Croatia", "Cyprus", "Czech Republic", "Denmark", "Dominican Republic",
  "Ecuador", "Egypt", "Estonia", "Ethiopia", "Finland", "France", "Georgia", "Germany",
  "Ghana", "Greece", "Guatemala", "Honduras", "Hong Kong", "Hungary", "Iceland", "India",
  "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan", "Jordan",
  "Kazakhstan", "Kenya", "Kuwait", "Latvia", "Lebanon", "Lithuania", "Luxembourg", "Malaysia",
  "Malta", "Mexico", "Moldova", "Monaco", "Mongolia", "Morocco", "Nepal", "Netherlands",
  "New Zealand", "Nigeria", "North Macedonia", "Norway", "Oman", "Pakistan", "Panama",
  "Paraguay", "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
  "Saudi Arabia", "Serbia", "Singapore", "Slovakia", "Slovenia", "South Africa", "South Korea",
  "Spain", "Sri Lanka", "Sweden", "Switzerland", "Taiwan", "Thailand", "Tunisia", "Turkey",
  "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay",
  "Uzbekistan", "Venezuela", "Vietnam", "Zimbabwe",
];

// Illustrative industry categories — a starting point for the Manual
// Builder's suggestions, not an exhaustive or authoritative list. Any
// industry not shown here can still be typed and added as free text.
export const INDUSTRY_SUGGESTIONS: string[] = [
  "SaaS", "B2B SaaS", "D2C", "E-commerce", "FMCG", "Healthcare", "Health Tech",
  "Fintech", "Insurance", "Banking", "Real Estate", "Manufacturing", "Logistics",
  "Retail", "Hospitality", "Travel", "Media", "OTT Platforms", "Gaming", "Education",
  "EdTech", "Marketing & Advertising", "Cybersecurity", "Legal Services", "HR Tech",
  "Biotechnology", "Pharmaceuticals", "Telecommunications", "Automotive", "Energy",
  "Construction", "Agriculture", "Non-profit", "Government", "Consumer Electronics",
  "Food & Beverage", "Fashion", "Beauty & Skincare", "Sports & Fitness",
];

// Illustrative company-type/structure labels — same non-authoritative,
// starting-point status as INDUSTRY_SUGGESTIONS above.
export const COMPANY_TYPE_SUGGESTIONS: string[] = [
  "B2B", "B2C", "D2C", "Startup", "Enterprise", "SMB", "Mid-Market", "PE-backed",
  "VC-backed", "Bootstrapped", "Public", "Private", "Nonprofit", "Agency", "Marketplace",
  "Franchise", "Family-owned",
];

/** Maps a filter catalog key to its local suggestion list, when one exists. */
export function suggestionsForFilterKey(key: string): string[] | null {
  switch (key) {
    case "geography":
      return LOCATION_SUGGESTIONS;
    case "industry":
      return INDUSTRY_SUGGESTIONS;
    case "company_type":
      return COMPANY_TYPE_SUGGESTIONS;
    default:
      return null;
  }
}
