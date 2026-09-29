"use client";

import { usePathname } from "next/navigation";
import StepperShell from "@/components/ui/StepperShell";
import { LeadAgentSessionProvider } from "@/lib/leads/sessionContext";

const STEPS = [
  { label: "Describe ICP", matchPath: "/leads/new" },
  { label: "Companies", matchPath: "/companies" },
  { label: "People", matchPath: "/people" },
  { label: "Enrich", matchPath: "/enrich" },
  { label: "Review", matchPath: "/review" },
];

function activeStepIndex(pathname: string): number {
  const index = STEPS.findIndex((step) => pathname.includes(step.matchPath));
  return index === -1 ? 0 : index;
}

export default function LeadsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <LeadAgentSessionProvider>
      <StepperShell steps={STEPS} activeIndex={activeStepIndex(pathname)} />
      {children}
    </LeadAgentSessionProvider>
  );
}
