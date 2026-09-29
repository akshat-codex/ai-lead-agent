"use client";

import Chip from "@mui/material/Chip";

/** Backend RankTier values (backend/app/schemas/ranking.py), verbatim. */
export type RankTier =
  | "ACCEPTED"
  | "QUALIFIED_STRONG"
  | "QUALIFIED_WEAK"
  | "HOLD"
  | "REJECTED"
  | "DUPLICATE"
  | "HARD_FAILED";

export type UiTier = "strong" | "good" | "weak" | "rejected";

/**
 * Single place mapping the backend's 7-value RankTier onto the UI's fit
 * vocabulary. ACCEPTED and QUALIFIED_STRONG both read as "Strong fit" (a
 * human-accepted lead is at least as strong as an algorithmically
 * qualified one); HOLD/REJECTED/DUPLICATE collapse into "Weak fit" (still
 * shown, never hidden).
 *
 * HARD_FAILED is deliberately its OWN branch, never routed through the
 * `default` case: a lead whose hard-rule validation FAILED was actively
 * disproven against the ICP, not merely a weak match — see
 * backend/app/services/lead_ranking.py's own "the hard gate is checked
 * FIRST and is absolute" comment. Before this branch existed, HARD_FAILED
 * silently fell into the same "weak" bucket as HOLD/REJECTED/DUPLICATE,
 * which produced a real, reported bug: a company card showing "Weak fit"
 * next to detail text reading "Hard ICP validation failed; rejected
 * without LLM involvement." Every OTHER tier's mapping is unchanged by
 * this fix.
 */
export function rankTierToUiTier(tier: RankTier): UiTier {
  switch (tier) {
    case "ACCEPTED":
    case "QUALIFIED_STRONG":
      return "strong";
    case "QUALIFIED_WEAK":
      return "good";
    case "HARD_FAILED":
      return "rejected";
    default:
      return "weak";
  }
}

const TIER_LABEL: Record<UiTier, string> = {
  strong: "Strong fit",
  good: "Good fit",
  weak: "Weak fit",
  rejected: "Rejected",
};

const TIER_COLOR: Record<UiTier, "success" | "warning" | "default" | "error"> = {
  strong: "success",
  good: "warning",
  weak: "default",
  rejected: "error",
};

// A soft, tinted look (light background + matching text, no hard fill) —
// premium/modern SaaS badge style rather than MUI's default saturated
// filled Chip. Colors are still driven entirely by the same semantic
// TIER_COLOR mapping above, just expressed as a soft tint per tier.
const TIER_TINT: Record<UiTier, { bg: string; fg: string; border: string }> = {
  strong: { bg: "rgba(30, 142, 90, 0.12)", fg: "#166a44", border: "rgba(30, 142, 90, 0.28)" },
  good: { bg: "rgba(185, 137, 0, 0.12)", fg: "#8a6600", border: "rgba(185, 137, 0, 0.28)" },
  weak: { bg: "rgba(93, 100, 114, 0.10)", fg: "#5d6472", border: "rgba(93, 100, 114, 0.24)" },
  rejected: { bg: "rgba(211, 47, 47, 0.10)", fg: "#a92a2a", border: "rgba(211, 47, 47, 0.26)" },
};

interface FitBadgeProps {
  tier: UiTier;
}

export default function FitBadge({ tier }: FitBadgeProps) {
  const tint = TIER_TINT[tier];
  return (
    <Chip
      label={TIER_LABEL[tier]}
      color={TIER_COLOR[tier]}
      size="small"
      sx={{
        bgcolor: tint.bg,
        color: tint.fg,
        border: "1px solid",
        borderColor: tint.border,
        "& .MuiChip-label": { px: 1.1 },
      }}
    />
  );
}
