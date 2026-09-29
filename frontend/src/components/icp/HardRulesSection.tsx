"use client";

import GavelIcon from "@mui/icons-material/Gavel";
import Alert from "@mui/material/Alert";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import CustomRuleListEditor from "./CustomRuleListEditor";
import TagsField from "./TagsField";
import type { HardRules } from "@/lib/icp/types";
import type { IcpValidationErrors } from "@/lib/icp/validation";

interface HardRulesSectionProps {
  value: HardRules;
  errors: IcpValidationErrors["hardRules"];
  onChange: (value: HardRules) => void;
}

export default function HardRulesSection({ value, errors, onChange }: HardRulesSectionProps) {
  const set = <K extends keyof HardRules>(key: K, val: HardRules[K]) =>
    onChange({ ...value, [key]: val });

  return (
    <Paper
      variant="outlined"
      sx={{ p: 3, borderRadius: 2.5, borderLeft: "4px solid", borderLeftColor: "error.main" }}
    >
      <Stack spacing={2.5}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <GavelIcon color="error" fontSize="small" />
          <Typography variant="h6">Hard rules</Typography>
          <Chip label="Mandatory" color="error" size="small" variant="outlined" />
        </Stack>
        <Typography variant="body2" color="text.secondary">
          A lead that fails any of these is disqualified — no score or AI judgment can override them.
        </Typography>

        <TagsField
          label="Industry"
          placeholder="e.g. Skincare"
          value={value.industry}
          onChange={(v) => set("industry", v)}
        />

        <TagsField
          label="Geography"
          placeholder="e.g. United States"
          error={errors.geography}
          value={value.geography}
          onChange={(v) => set("geography", v)}
        />

        <Stack direction="row" spacing={2}>
          <TextField
            label="Minimum employees"
            type="number"
            fullWidth
            value={value.minEmployees ?? ""}
            error={Boolean(errors.employeeRange)}
            onChange={(e) =>
              set("minEmployees", e.target.value === "" ? null : Number(e.target.value))
            }
          />
          <TextField
            label="Maximum employees"
            type="number"
            fullWidth
            value={value.maxEmployees ?? ""}
            error={Boolean(errors.employeeRange)}
            helperText={errors.employeeRange}
            onChange={(e) =>
              set("maxEmployees", e.target.value === "" ? null : Number(e.target.value))
            }
          />
        </Stack>

        <TagsField
          label="Allowed decision-maker titles"
          placeholder="e.g. Head of Growth"
          value={value.allowedTitles}
          onChange={(v) => set("allowedTitles", v)}
        />

        <TagsField
          label="Company type"
          placeholder="e.g. D2C, PE-backed, Startup"
          value={value.companyType}
          onChange={(v) => set("companyType", v)}
        />

        <TagsField
          label="Explicit exclusions"
          placeholder="e.g. a company name, domain, or excluded industry"
          error={errors.exclusions}
          value={value.exclusions}
          onChange={(v) => set("exclusions", v)}
        />
        {errors.exclusions && <Alert severity="error">{errors.exclusions}</Alert>}

        <CustomRuleListEditor
          title="Other mandatory / custom rules"
          addLabel="Add custom rule"
          emptyHint="No custom rules yet."
          items={value.customRules}
          errors={errors.customRules}
          onChange={(v) => set("customRules", v)}
        />
      </Stack>
    </Paper>
  );
}
