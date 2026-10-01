import { latestValueForField } from "@/lib/evidence/api";
import type { EvidenceRecord } from "@/lib/evidence/types";
import type { CanonicalCompany, CompanyFacts } from "@/lib/companies/types";
import { findLinkedInId } from "@/lib/companies/api";
import type { CanonicalPerson } from "@/lib/people/types";
import type { ExportedLead } from "@/lib/export/types";

/**
 * One row of the final, review-ready lead table — assembled client-side
 * from three real, already-existing sources, never fabricated:
 *  - the export endpoint (Phase 23): identity, rank, tier, score, qualification
 *  - raw evidence (Phase 11 + Phase 5 enrichment): title, email, phone,
 *    person LinkedIn — read directly rather than through the export's
 *    conservative multi-source-corroboration "verified_fields" filter,
 *    since a single Apollo/discovery observation is real and worth showing
 *    even though it doesn't meet that stricter "SUPPORTED" bar.
 *  - company facts (Phase 8 enrichment): company LinkedIn.
 * Every optional field is null exactly when the underlying source never
 * produced a value — nothing here guesses or fills a gap.
 */
export interface ReviewLead {
  leadId: string;
  companyId: string;
  companyName: string | null;
  companyDomain: string | null;
  companyLinkedinUrl: string | null;
  personId: string | null;
  personName: string | null;
  title: string | null;
  email: string | null;
  emailStatus: string | null;
  phone: string | null;
  personLinkedinUrl: string | null;
  tier: string | null;
  finalScore: number | null;
  /** How confident the system is this is genuinely the same real-world
   * (company, person) across providers — distinct from finalScore's
   * overall fit judgment (see ExportScores.identityConfidence's own
   * docstring). */
  identityConfidence: number | null;
  /** Component scores, surfaced only as a confidence-tooltip breakdown,
   * never as their own top-level columns (keeps the table scannable). */
  icpScore: number | null;
  commercialScore: number | null;
  evidenceScore: number | null;
  /** Real, machine-readable "why" behind rank/tier — e.g.
   * ["HARD_RULE_PASS", "QUALIFICATION_GOOD_FIT"]. */
  rankingReasonCodes: string[];
  /** Fields where independent sources disagree (e.g. industry) — shown as
   * an honest quality warning, never silently resolved. */
  conflictingFields: string[];
  qualificationDecision: string | null;
  qualificationSummary: string | null;
  rank: number | null;
  enrichmentStatus: "enriched" | "not enriched";
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function buildReviewLead(
  exported: ExportedLead,
  person: CanonicalPerson | undefined,
  personEvidence: EvidenceRecord[],
  company: CanonicalCompany | undefined,
  companyFacts: CompanyFacts | undefined,
): ReviewLead {
  const title = asString(latestValueForField(personEvidence, "current_title"));
  const email = asString(latestValueForField(personEvidence, "email"));
  const emailStatus = asString(latestValueForField(personEvidence, "email_status"));
  const phone = asString(latestValueForField(personEvidence, "phone"));
  const linkedinUrl = asString(latestValueForField(personEvidence, "linkedin_url"));
  const linkedinIdFromEvidence = asString(latestValueForField(personEvidence, "linkedin_id"));
  const personLinkedinId = person?.linkedinId ?? linkedinIdFromEvidence;

  const companyLinkedinId = companyFacts ? findLinkedInId(companyFacts) : null;

  const hasApolloEvidence = personEvidence.some((r) => r.field === "email" || r.field === "linkedin_url");

  return {
    leadId: exported.leadId,
    companyId: exported.identity.companyId,
    companyName: exported.identity.companyName ?? company?.canonicalName ?? null,
    companyDomain: exported.identity.companyDomain ?? company?.canonicalDomain ?? null,
    companyLinkedinUrl: companyLinkedinId ? `https://linkedin.com/${companyLinkedinId}` : null,
    personId: exported.identity.personId,
    personName: exported.identity.personName,
    title,
    email,
    emailStatus,
    phone,
    personLinkedinUrl: linkedinUrl ?? (personLinkedinId ? `https://linkedin.com/in/${personLinkedinId}` : null),
    tier: exported.tier,
    finalScore: exported.scores.finalScore,
    identityConfidence: exported.scores.identityConfidence,
    icpScore: exported.scores.icpScore,
    commercialScore: exported.scores.commercialScore,
    evidenceScore: exported.scores.evidenceScore,
    rankingReasonCodes: exported.rankingReasonCodes,
    conflictingFields: exported.evidence.conflictingFields,
    qualificationDecision: exported.qualificationDecision,
    qualificationSummary: exported.qualificationSummary,
    rank: exported.rank,
    enrichmentStatus: hasApolloEvidence ? "enriched" : "not enriched",
  };
}
