/**
 * Human-readable labels for the backend's RankingReasonCode enum
 * (backend/app/schemas/ranking.py). This is a label lookup on a
 * backend-defined enum, not invented text — every label maps 1:1 to a real
 * backend value; unknown codes fall back to the raw code itself.
 */
const REASON_CODE_LABELS: Record<string, string> = {
  HARD_RULE_FAIL: "Fails a hard requirement",
  HARD_RULE_HOLD: "Hard requirement unresolved",
  HARD_RULE_PASS: "Meets all hard requirements",
  HARD_RULE_UNKNOWN: "Hard requirement result unknown",
  HUMAN_ACCEPTED: "Accepted by reviewer",
  HUMAN_REJECTED: "Rejected by reviewer",
  HUMAN_HELD: "Held by reviewer",
  HUMAN_DUPLICATE: "Marked duplicate by reviewer",
  QUALIFICATION_GOOD_FIT: "Strong overall fit",
  QUALIFICATION_WEAK_FIT: "Some fit, weaker signal",
  QUALIFICATION_NOT_FIT: "Not a fit",
  QUALIFICATION_HOLD: "Qualification held",
  QUALIFICATION_MISSING: "Not yet qualified",
  ADVERSARIAL_SURVIVES: "Fit confirmed under review",
  ADVERSARIAL_WEAKENED: "Fit weakened under review",
  ADVERSARIAL_DISPROVED: "Fit disproved under review",
  ADVERSARIAL_HOLD: "Adversarial review held",
  ADVERSARIAL_MISSING: "Not yet adversarially reviewed",
  SCORE_MISSING: "Not yet scored",
  EVIDENCE_CONFLICTS_PRESENT: "Conflicting evidence present",
  VERIFICATION_UNRESOLVED: "Verification unresolved",
  TIE_BROKEN_BY_EVIDENCE_SCORE: "Tie broken by evidence strength",
  TIE_BROKEN_BY_LEAD_ID: "Tie broken by lead id",
};

export function reasonCodeLabel(code: string): string {
  return REASON_CODE_LABELS[code] ?? code;
}

/** Reason codes that represent a positive/met signal, for evidence-checklist styling. */
const POSITIVE_CODES = new Set([
  "HARD_RULE_PASS",
  "HUMAN_ACCEPTED",
  "QUALIFICATION_GOOD_FIT",
  "QUALIFICATION_WEAK_FIT",
  "ADVERSARIAL_SURVIVES",
]);

const NEGATIVE_CODES = new Set([
  "HARD_RULE_FAIL",
  "HUMAN_REJECTED",
  "HUMAN_DUPLICATE",
  "QUALIFICATION_NOT_FIT",
  "ADVERSARIAL_DISPROVED",
  "EVIDENCE_CONFLICTS_PRESENT",
]);

export type ReasonCodeSentiment = "met" | "not_met" | "unknown";

export function reasonCodeSentiment(code: string): ReasonCodeSentiment {
  if (POSITIVE_CODES.has(code)) return "met";
  if (NEGATIVE_CODES.has(code)) return "not_met";
  return "unknown";
}
