import type { ReviewLead } from "./reviewLead";

const COLUMNS: { key: keyof ReviewLead | string; label: string; get: (lead: ReviewLead) => string }[] = [
  { key: "companyName", label: "Company", get: (l) => l.companyName ?? "" },
  { key: "companyDomain", label: "Company Domain", get: (l) => l.companyDomain ?? "" },
  { key: "companyLinkedinUrl", label: "Company LinkedIn", get: (l) => l.companyLinkedinUrl ?? "" },
  { key: "personName", label: "Person", get: (l) => l.personName ?? "" },
  { key: "title", label: "Title", get: (l) => l.title ?? "" },
  { key: "email", label: "Email", get: (l) => l.email ?? "" },
  { key: "emailStatus", label: "Email Status", get: (l) => l.emailStatus ?? "" },
  { key: "phone", label: "Phone", get: (l) => l.phone ?? "" },
  { key: "personLinkedinUrl", label: "Person LinkedIn", get: (l) => l.personLinkedinUrl ?? "" },
  { key: "tier", label: "Tier", get: (l) => l.tier ?? "" },
  { key: "finalScore", label: "Final Score", get: (l) => (l.finalScore !== null ? String(l.finalScore) : "") },
  { key: "qualificationDecision", label: "Qualification", get: (l) => l.qualificationDecision ?? "" },
  { key: "enrichmentStatus", label: "Enrichment Status", get: (l) => l.enrichmentStatus },
];

function escapeCsvField(value: string): string {
  if (value.includes(",") || value.includes('"') || value.includes("\n")) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

/** Builds CSV from exactly the data already assembled/displayed on the
 * review page — never a re-fetch, never a value the user can't also see. */
export function buildReviewCsv(leads: ReviewLead[]): string {
  const header = COLUMNS.map((c) => escapeCsvField(c.label)).join(",");
  const rows = leads.map((lead) => COLUMNS.map((c) => escapeCsvField(c.get(lead))).join(","));
  return [header, ...rows].join("\n");
}
