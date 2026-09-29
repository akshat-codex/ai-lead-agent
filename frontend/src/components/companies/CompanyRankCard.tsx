"use client";

import { useState } from "react";
import BusinessIcon from "@mui/icons-material/BusinessOutlined";
import LaunchIcon from "@mui/icons-material/Launch";
import GroupsIcon from "@mui/icons-material/GroupsOutlined";
import PlaceIcon from "@mui/icons-material/PlaceOutlined";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Collapse from "@mui/material/Collapse";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import EvidenceChecklist from "./EvidenceChecklist";
import EntityCard from "@/components/ui/EntityCard";
import FitBadge, { rankTierToUiTier } from "@/components/ui/FitBadge";
import ScorePercent from "@/components/ui/ScorePercent";
import type { CompanyAttributes } from "@/lib/companies/attributes";
import type { RankedLead } from "@/lib/ranking/types";

interface CompanyRankCardProps {
  companyName: string;
  companyDomain: string | null;
  ranked: RankedLead;
  attributes?: CompanyAttributes | null;
  qualificationSummary: string | null;
  qualificationExplanation: string | null;
  linkedInId: string | null;
  isEvaluationPending: boolean;
  selected: boolean;
  onToggleSelected: () => void;
}

function AttributeChip({ icon, label }: { icon: React.ReactElement; label: string }) {
  return (
    <Chip
      size="small"
      variant="outlined"
      icon={icon}
      label={label}
      sx={{ borderColor: "divider", color: "text.secondary", bgcolor: "background.default", fontWeight: 500 }}
    />
  );
}

export default function CompanyRankCard({
  companyName,
  companyDomain,
  ranked,
  attributes,
  qualificationSummary,
  qualificationExplanation,
  linkedInId,
  isEvaluationPending,
  selected,
  onToggleSelected,
}: CompanyRankCardProps) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = Boolean(qualificationSummary || qualificationExplanation);
  const hasAttributes = Boolean(attributes?.industry || attributes?.country || attributes?.employeeRange);

  return (
    <EntityCard selected={selected} onToggleSelected={onToggleSelected}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", rowGap: 0.5 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center", minWidth: 0 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }} noWrap>
            {companyName}
          </Typography>
          <FitBadge tier={rankTierToUiTier(ranked.tier)} />
        </Stack>
        <ScorePercent value={ranked.signals.finalScore} />
      </Stack>

      {companyDomain && (
        <Typography variant="body2" color="text.secondary">
          {companyDomain}
        </Typography>
      )}

      {hasAttributes && (
        <Stack direction="row" spacing={1} sx={{ mt: 1, flexWrap: "wrap", gap: 1 }}>
          {attributes?.industry && <AttributeChip icon={<BusinessIcon fontSize="inherit" />} label={attributes.industry} />}
          {attributes?.employeeRange && <AttributeChip icon={<GroupsIcon fontSize="inherit" />} label={`${attributes.employeeRange} employees`} />}
          {attributes?.country && <AttributeChip icon={<PlaceIcon fontSize="inherit" />} label={attributes.country} />}
        </Stack>
      )}

      {isEvaluationPending ? (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5, fontStyle: "italic" }}>
          Evaluation pending
        </Typography>
      ) : (
        <Box sx={{ mt: 1.5 }}>
          <EvidenceChecklist reasonCodes={ranked.reasonCodes} />
        </Box>
      )}

      {hasDetail && (
        <>
          <Link
            component="button"
            variant="body2"
            onClick={() => setExpanded((v) => !v)}
            sx={{ mt: 1, display: "inline-block" }}
          >
            {expanded ? "Hide details" : "Why this company matched"}
          </Link>
          <Collapse in={expanded}>
            <Stack
              spacing={0.5}
              sx={{ mt: 1, p: 1.5, bgcolor: "background.default", border: "1px solid", borderColor: "divider", borderRadius: 1.5 }}
            >
              {qualificationSummary && (
                <Typography variant="body2">{qualificationSummary}</Typography>
              )}
              {qualificationExplanation && (
                <Typography variant="body2" color="text.secondary">
                  {qualificationExplanation}
                </Typography>
              )}
            </Stack>
          </Collapse>
        </>
      )}

      <Stack direction="row" spacing={1} sx={{ mt: 1, alignItems: "center" }}>
        {linkedInId && (
          <Link
            href={`https://linkedin.com/${linkedInId}`}
            target="_blank"
            rel="noopener noreferrer"
            variant="body2"
            sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, fontWeight: 600 }}
          >
            LinkedIn <LaunchIcon fontSize="inherit" />
          </Link>
        )}
      </Stack>
    </EntityCard>
  );
}
