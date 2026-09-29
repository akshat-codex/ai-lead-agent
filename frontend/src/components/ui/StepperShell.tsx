"use client";

import Box from "@mui/material/Box";
import Container from "@mui/material/Container";
import Step from "@mui/material/Step";
import StepLabel from "@mui/material/StepLabel";
import Stepper from "@mui/material/Stepper";

export interface WizardStep {
  label: string;
  /** Pathname (or pathname prefix) that marks this step active/complete. */
  matchPath: string;
}

interface StepperShellProps {
  steps: WizardStep[];
  activeIndex: number;
}

export default function StepperShell({ steps, activeIndex }: StepperShellProps) {
  return (
    <Box sx={{ borderBottom: "1px solid", borderColor: "divider", bgcolor: "background.paper" }}>
      <Container maxWidth="lg" sx={{ py: 2.5 }}>
        <Stepper activeStep={activeIndex} alternativeLabel>
          {steps.map((step) => (
            <Step key={step.label}>
              <StepLabel>{step.label}</StepLabel>
            </Step>
          ))}
        </Stepper>
      </Container>
    </Box>
  );
}
