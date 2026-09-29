"use client";

import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Step from "@mui/material/Step";
import StepLabel from "@mui/material/StepLabel";
import Stepper from "@mui/material/Stepper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import HardRulesSection from "./HardRulesSection";
import SoftPreferencesSection from "./SoftPreferencesSection";
import IcpSummary from "./IcpSummary";
import { saveIcp } from "@/lib/icp/api";
import { createEmptyIcpDraft, type IcpDraft, type SavedIcp } from "@/lib/icp/types";
import {
  collectErrorMessages,
  firstInvalidStep,
  isIcpDraftValid,
  validateIcpDraft,
} from "@/lib/icp/validation";

const STEPS = ["Hard rules", "Soft preferences", "Review & save"];

interface IcpBuilderProps {
  mode: "create" | "new-version";
  /** Pre-fills the builder — e.g. the previous version's data when creating a new version. */
  initialDraft?: IcpDraft;
  onSaved: (saved: SavedIcp) => void;
}

export default function IcpBuilder({ mode, initialDraft, onSaved }: IcpBuilderProps) {
  const [draft, setDraft] = useState<IcpDraft>(() => initialDraft ?? createEmptyIcpDraft());
  const [activeStep, setActiveStep] = useState(0);
  const [showErrors, setShowErrors] = useState(false);

  const errors = useMemo(() => validateIcpDraft(draft), [draft]);
  const valid = isIcpDraftValid(errors);

  const mutation = useMutation({
    mutationFn: () => saveIcp(draft),
    onSuccess: onSaved,
  });

  const goNext = () => setActiveStep((s) => Math.min(s + 1, STEPS.length - 1));
  const goBack = () => setActiveStep((s) => Math.max(s - 1, 0));

  const handleSave = () => {
    setShowErrors(true);
    if (valid) {
      mutation.mutate();
      return;
    }
    const invalidStep = firstInvalidStep(errors);
    if (invalidStep !== null) setActiveStep(invalidStep);
  };

  return (
    <Stack spacing={3}>
      <TextField
        label="ICP name"
        placeholder="e.g. D2C Skincare"
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        error={showErrors && Boolean(errors.name)}
        helperText={
          (showErrors && errors.name) ||
          (mode === "new-version"
            ? "Saving will create the next version under this name."
            : "Give this ICP a short, memorable name — versions are tracked under it.")
        }
        disabled={mode === "new-version"}
        fullWidth
      />

      <Stepper activeStep={activeStep}>
        {STEPS.map((label) => (
          <Step key={label}>
            <StepLabel>{label}</StepLabel>
          </Step>
        ))}
      </Stepper>

      {showErrors && errors.general && <Alert severity="error">{errors.general}</Alert>}

      <Box>
        {activeStep === 0 && (
          <HardRulesSection
            value={draft.hardRules}
            errors={showErrors ? errors.hardRules : {}}
            onChange={(hardRules) => setDraft({ ...draft, hardRules })}
          />
        )}
        {activeStep === 1 && (
          <SoftPreferencesSection
            value={draft.softPreferences}
            errors={showErrors ? errors.softPreferences : {}}
            onChange={(softPreferences) => setDraft({ ...draft, softPreferences })}
          />
        )}
        {activeStep === 2 && (
          <Stack spacing={2}>
            <Typography variant="body2" color="text.secondary">
              Review the configuration below before saving.
            </Typography>
            {showErrors && !valid && (
              <Alert severity="error">
                <Stack spacing={0.5}>
                  {collectErrorMessages(errors).map((message) => (
                    <Typography variant="body2" key={message}>
                      {message}
                    </Typography>
                  ))}
                </Stack>
              </Alert>
            )}
            <IcpSummary draft={draft} />
          </Stack>
        )}
      </Box>

      {mutation.isError && (
        <Alert severity="error">
          {mutation.error instanceof Error ? mutation.error.message : "Failed to save ICP."}
        </Alert>
      )}

      <Stack direction="row" spacing={2} sx={{ justifyContent: "space-between" }}>
        <Button onClick={goBack} disabled={activeStep === 0}>
          Back
        </Button>
        {activeStep < STEPS.length - 1 ? (
          <Button variant="contained" onClick={goNext}>
            Next
          </Button>
        ) : (
          <Button variant="contained" onClick={handleSave} disabled={mutation.isPending}>
            {mutation.isPending ? "Saving..." : mode === "new-version" ? "Save new version" : "Save ICP"}
          </Button>
        )}
      </Stack>
    </Stack>
  );
}
