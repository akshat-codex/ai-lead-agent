"use client";

import TuneIcon from "@mui/icons-material/Tune";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CustomRuleListEditor from "./CustomRuleListEditor";
import TagsField from "./TagsField";
import type { SoftPreferences } from "@/lib/icp/types";
import type { IcpValidationErrors } from "@/lib/icp/validation";

interface SoftPreferencesSectionProps {
  value: SoftPreferences;
  errors: IcpValidationErrors["softPreferences"];
  onChange: (value: SoftPreferences) => void;
}

export default function SoftPreferencesSection({
  value,
  errors,
  onChange,
}: SoftPreferencesSectionProps) {
  const set = <K extends keyof SoftPreferences>(key: K, val: SoftPreferences[K]) =>
    onChange({ ...value, [key]: val });

  return (
    <Paper
      variant="outlined"
      sx={{ p: 3, borderLeft: "4px solid", borderLeftColor: "info.main" }}
    >
      <Stack spacing={2.5}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <TuneIcon color="info" fontSize="small" />
          <Typography variant="h6">Soft / commercial preferences</Typography>
          <Chip label="Prioritization only" color="info" size="small" variant="outlined" />
        </Stack>
        <Typography variant="body2" color="text.secondary">
          These never disqualify a lead — they only influence ranking and prioritization once
          discovery and scoring exist.
        </Typography>

        <TagsField
          label="Business model preferences"
          placeholder="e.g. Subscription, Marketplace"
          value={value.businessModelPreferences}
          onChange={(v) => set("businessModelPreferences", v)}
        />

        <TagsField
          label="Commercial signals"
          placeholder="e.g. Recent funding round"
          value={value.commercialSignals}
          onChange={(v) => set("commercialSignals", v)}
        />

        <TagsField
          label="Growth signals"
          placeholder="e.g. Hiring for growth roles"
          value={value.growthSignals}
          onChange={(v) => set("growthSignals", v)}
        />

        <TagsField
          label="Marketing signals"
          placeholder="e.g. Active on Instagram/TikTok"
          value={value.marketingSignals}
          onChange={(v) => set("marketingSignals", v)}
        />

        <CustomRuleListEditor
          title="Other prioritization preferences"
          addLabel="Add preference"
          emptyHint="No other preferences yet."
          items={value.otherPreferences}
          errors={errors.otherPreferences}
          onChange={(v) => set("otherPreferences", v)}
        />
      </Stack>
    </Paper>
  );
}
