"use client";

import { useState } from "react";
import LinkedInIcon from "@mui/icons-material/LinkedIn";
import Box from "@mui/material/Box";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import Collapse from "@mui/material/Collapse";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EvidenceChecklist from "@/components/companies/EvidenceChecklist";
import FitBadge, { rankTierToUiTier } from "@/components/ui/FitBadge";
import ScorePercent from "@/components/ui/ScorePercent";
import type { RankedLead } from "@/lib/ranking/types";

interface PersonCardProps {
  name: string;
  /** From the discovery-time candidate — canonical people don't persist a
   * title, so this is honestly labeled "as discovered", not verified/current. */
  title: string | null;
  ranked: RankedLead | null;
  qualificationSummary: string | null;
  qualificationExplanation: string | null;
  linkedinId: string | null;
  isEvaluationPending: boolean;
  selected: boolean;
  onToggleSelected: () => void;
}

export default function PersonCard({
  name,
  title,
  ranked,
  qualificationSummary,
  qualificationExplanation,
  linkedinId,
  isEvaluationPending,
  selected,
  onToggleSelected,
}: PersonCardProps) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = Boolean(qualificationSummary || qualificationExplanation);

  return (
    <Paper
      variant="outlined"
      sx={{
        p: 2.5,
        borderColor: selected ? "primary.main" : "divider",
        transition: "border-color 120ms ease, box-shadow 120ms ease",
        "&:hover": { boxShadow: 1 },
      }}
    >
      <Stack direction="row" spacing={1.5} sx={{ alignItems: "flex-start" }}>
        <Checkbox checked={selected} onChange={onToggleSelected} sx={{ mt: -0.5 }} />
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", rowGap: 0.5 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", minWidth: 0 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600 }} noWrap>
                {name}
              </Typography>
              {ranked && <FitBadge tier={rankTierToUiTier(ranked.tier)} />}
            </Stack>
            {ranked && <ScorePercent value={ranked.signals.finalScore} />}
          </Stack>

          {title && (
            <Tooltip title="Title as last seen by the discovery provider; not independently verified.">
              <Typography variant="body2" color="text.secondary">
                {title} <Typography component="span" variant="caption">(as discovered)</Typography>
              </Typography>
            </Tooltip>
          )}

          <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
            {linkedinId ? (
              <Chip
                size="small"
                variant="filled"
                color="primary"
                clickable
                component="a"
                href={`https://linkedin.com/in/${linkedinId}`}
                target="_blank"
                rel="noopener noreferrer"
                icon={<LinkedInIcon fontSize="inherit" />}
                label="LinkedIn available"
              />
            ) : (
              <Chip size="small" variant="outlined" icon={<LinkedInIcon fontSize="inherit" />} label="No LinkedIn found" />
            )}
          </Stack>

          {ranked && !isEvaluationPending && (
            <Box sx={{ mt: 1.5 }}>
              <EvidenceChecklist reasonCodes={ranked.reasonCodes} />
            </Box>
          )}
          {isEvaluationPending && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5, fontStyle: "italic" }}>
              Evaluation pending
            </Typography>
          )}

          {hasDetail && (
            <>
              <Link
                component="button"
                variant="body2"
                onClick={() => setExpanded((v) => !v)}
                sx={{ mt: 1, display: "inline-block" }}
              >
                {expanded ? "Hide details" : "Why this person"}
              </Link>
              <Collapse in={expanded}>
                <Stack spacing={0.5} sx={{ mt: 1, p: 1.5, bgcolor: "action.hover", borderRadius: 1 }}>
                  {qualificationSummary && <Typography variant="body2">{qualificationSummary}</Typography>}
                  {qualificationExplanation && (
                    <Typography variant="body2" color="text.secondary">
                      {qualificationExplanation}
                    </Typography>
                  )}
                </Stack>
              </Collapse>
            </>
          )}
        </Box>
      </Stack>
    </Paper>
  );
}
