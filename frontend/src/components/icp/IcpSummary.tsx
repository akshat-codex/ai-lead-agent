"use client";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { IcpDraft } from "@/lib/icp/types";

function ChipRow({ label, values }: { label: string; values: string[] }) {
  if (values.length === 0) return null;
  return (
    <Box>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1, mt: 0.5 }}>
        {values.map((v) => (
          <Chip key={v} label={v} size="small" />
        ))}
      </Stack>
    </Box>
  );
}

export default function IcpSummary({ draft }: { draft: IcpDraft }) {
  const { hardRules: h, softPreferences: s } = draft;
  const hasEmployeeRange = h.minEmployees !== null || h.maxEmployees !== null;

  return (
    <Stack spacing={3}>
      <Typography variant="h6">{draft.name || "Untitled ICP"}</Typography>

      <Paper variant="outlined" sx={{ p: 2.5, borderRadius: 2.5, borderLeft: "4px solid", borderLeftColor: "error.main" }}>
        <Typography variant="subtitle1" sx={{ mb: 1.5 }}>
          Hard rules
        </Typography>
        <Stack spacing={1.5}>
          <ChipRow label="Industry" values={h.industry} />
          <ChipRow label="Geography" values={h.geography} />
          {hasEmployeeRange && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Employee count
              </Typography>
              <Typography variant="body2">
                {h.minEmployees ?? "any"} – {h.maxEmployees ?? "any"}
              </Typography>
            </Box>
          )}
          <ChipRow label="Allowed decision-maker titles" values={h.allowedTitles} />
          <ChipRow label="Company type" values={h.companyType} />
          <ChipRow label="Explicit exclusions" values={h.exclusions} />
          {h.customRules.length > 0 && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Custom rules
              </Typography>
              <Stack spacing={0.5} sx={{ mt: 0.5 }}>
                {h.customRules.map((rule) => (
                  <Typography variant="body2" key={rule.id}>
                    <strong>{rule.label}:</strong> {rule.description}
                  </Typography>
                ))}
              </Stack>
            </Box>
          )}
          {!h.industry.length &&
            !h.geography.length &&
            !hasEmployeeRange &&
            !h.allowedTitles.length &&
            !h.companyType.length &&
            !h.exclusions.length &&
            !h.customRules.length && (
              <Typography variant="body2" color="text.secondary">
                No hard rules set.
              </Typography>
            )}
        </Stack>
      </Paper>

      <Divider />

      <Paper variant="outlined" sx={{ p: 2.5, borderRadius: 2.5, borderLeft: "4px solid", borderLeftColor: "info.main" }}>
        <Typography variant="subtitle1" sx={{ mb: 1.5 }}>
          Soft / commercial preferences
        </Typography>
        <Stack spacing={1.5}>
          <ChipRow label="Business model preferences" values={s.businessModelPreferences} />
          <ChipRow label="Commercial signals" values={s.commercialSignals} />
          <ChipRow label="Growth signals" values={s.growthSignals} />
          <ChipRow label="Marketing signals" values={s.marketingSignals} />
          {s.otherPreferences.length > 0 && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Other preferences
              </Typography>
              <Stack spacing={0.5} sx={{ mt: 0.5 }}>
                {s.otherPreferences.map((pref) => (
                  <Typography variant="body2" key={pref.id}>
                    <strong>{pref.label}:</strong> {pref.description}
                  </Typography>
                ))}
              </Stack>
            </Box>
          )}
          {!s.businessModelPreferences.length &&
            !s.commercialSignals.length &&
            !s.growthSignals.length &&
            !s.marketingSignals.length &&
            !s.otherPreferences.length && (
              <Typography variant="body2" color="text.secondary">
                No soft preferences set.
              </Typography>
            )}
        </Stack>
      </Paper>
    </Stack>
  );
}
