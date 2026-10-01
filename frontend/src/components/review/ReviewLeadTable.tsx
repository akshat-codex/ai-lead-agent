"use client";

import { useState } from "react";
import LaunchIcon from "@mui/icons-material/Launch";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import Box from "@mui/material/Box";
import Collapse from "@mui/material/Collapse";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import Paper from "@mui/material/Paper";
import EvidenceChecklist from "@/components/companies/EvidenceChecklist";
import FitBadge, { rankTierToUiTier } from "@/components/ui/FitBadge";
import type { RankTier } from "@/components/ui/FitBadge";
import ScorePercent from "@/components/ui/ScorePercent";
import StatusChip from "@/components/ui/StatusChip";
import type { ReviewLead } from "@/lib/export/reviewLead";

function Unavailable() {
  return (
    <Typography variant="body2" color="text.secondary" sx={{ fontStyle: "italic" }}>
      Unavailable
    </Typography>
  );
}

/** Fit tier plus a warning icon when independent sources disagree on a
 * critical field (e.g. industry) — an honest data-quality signal that was
 * previously invisible unless a reviewer dug into the company detail page. */
function FitWithConflictWarning({ tier, conflictingFields }: { tier: RankTier | null; conflictingFields: string[] }) {
  if (!tier) return <Unavailable />;
  return (
    <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
      <FitBadge tier={rankTierToUiTier(tier)} />
      {conflictingFields.length > 0 && (
        <Tooltip title={`Sources disagree on: ${conflictingFields.join(", ")}`}>
          <WarningAmberIcon color="warning" fontSize="small" />
        </Tooltip>
      )}
    </Stack>
  );
}

/** The real "why" behind a lead's rank — the backend's own ranking reason
 * codes (same component EvidenceChecklist already uses on the Companies
 * page) plus the LLM's own written qualification summary, expandable
 * rather than always-on to keep the table scannable. */
function WhyCell({ reasonCodes, qualificationSummary }: { reasonCodes: string[]; qualificationSummary: string | null }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = reasonCodes.length > 0 || Boolean(qualificationSummary);
  if (!hasDetail) return <Unavailable />;

  return (
    <Box sx={{ maxWidth: 280 }}>
      <Link component="button" variant="body2" onClick={() => setExpanded((v) => !v)}>
        {expanded ? "Hide reason" : "Why this lead"}
      </Link>
      <Collapse in={expanded}>
        <Stack spacing={0.75} sx={{ mt: 0.75 }}>
          {qualificationSummary && (
            <Typography variant="body2" color="text.secondary">
              {qualificationSummary}
            </Typography>
          )}
          <EvidenceChecklist reasonCodes={reasonCodes} />
        </Stack>
      </Collapse>
    </Box>
  );
}

/** Identity confidence — distinct from the overall Score column: how sure
 * the system is this (company, person) pair is genuinely the same
 * real-world entity across providers, not how well it fits the ICP. */
function ConfidenceCell({
  identityConfidence,
  icpScore,
  commercialScore,
  evidenceScore,
}: {
  identityConfidence: number | null;
  icpScore: number | null;
  commercialScore: number | null;
  evidenceScore: number | null;
}) {
  const tooltip = (
    <Stack spacing={0.25}>
      <Typography variant="caption">ICP fit: {icpScore === null ? "—" : `${Math.round(icpScore)}%`}</Typography>
      <Typography variant="caption">Commercial fit: {commercialScore === null ? "—" : `${Math.round(commercialScore)}%`}</Typography>
      <Typography variant="caption">Evidence quality: {evidenceScore === null ? "—" : `${Math.round(evidenceScore)}%`}</Typography>
    </Stack>
  );
  return (
    <Tooltip title={tooltip}>
      <Box sx={{ display: "inline-block" }}>
        <ScorePercent value={identityConfidence} />
      </Box>
    </Tooltip>
  );
}

function LinkedInCell({ url }: { url: string | null }) {
  if (!url) return <Unavailable />;
  return (
    <Link href={url} target="_blank" rel="noopener noreferrer" variant="body2" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      View <LaunchIcon fontSize="inherit" />
    </Link>
  );
}

interface ReviewLeadTableProps {
  leads: ReviewLead[];
}

export default function ReviewLeadTable({ leads }: ReviewLeadTableProps) {
  return (
    <TableContainer component={Paper} variant="outlined" sx={{ borderRadius: 2.5, maxHeight: "70vh" }}>
      <Table size="small" stickyHeader>
        <TableHead>
          <TableRow>
            <TableCell>Company</TableCell>
            <TableCell>Person</TableCell>
            <TableCell>Title</TableCell>
            <TableCell>Company LinkedIn</TableCell>
            <TableCell>Person LinkedIn</TableCell>
            <TableCell>Email</TableCell>
            <TableCell>Email Status</TableCell>
            <TableCell>Phone</TableCell>
            <TableCell>Enrichment</TableCell>
            <TableCell>Fit</TableCell>
            <TableCell>Score</TableCell>
            <TableCell>Confidence</TableCell>
            <TableCell>Qualification</TableCell>
            <TableCell>Why</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {leads.map((lead) => (
            <TableRow
              key={lead.leadId}
              hover
              sx={{ "&:last-child td": { borderBottom: 0 } }}
            >
              <TableCell sx={{ whiteSpace: "nowrap" }}>
                <Typography variant="body2" sx={{ fontWeight: 700 }}>
                  {lead.companyName ?? <Unavailable />}
                </Typography>
                {lead.companyDomain && (
                  <Typography variant="caption" color="text.secondary">
                    {lead.companyDomain}
                  </Typography>
                )}
              </TableCell>
              <TableCell sx={{ whiteSpace: "nowrap" }}>{lead.personName ?? <Unavailable />}</TableCell>
              <TableCell sx={{ whiteSpace: "nowrap" }}>{lead.title ?? <Unavailable />}</TableCell>
              <TableCell>
                <LinkedInCell url={lead.companyLinkedinUrl} />
              </TableCell>
              <TableCell>
                <LinkedInCell url={lead.personLinkedinUrl} />
              </TableCell>
              <TableCell sx={{ whiteSpace: "nowrap" }}>{lead.email ?? <Unavailable />}</TableCell>
              <TableCell sx={{ whiteSpace: "nowrap" }}>{lead.emailStatus ?? <Unavailable />}</TableCell>
              <TableCell sx={{ whiteSpace: "nowrap" }}>{lead.phone ?? <Unavailable />}</TableCell>
              <TableCell>
                <StatusChip status={lead.enrichmentStatus === "enriched" ? "done" : "idle"} label={lead.enrichmentStatus === "enriched" ? "Enriched" : "Not enriched"} />
              </TableCell>
              <TableCell>
                <FitWithConflictWarning tier={lead.tier as RankTier | null} conflictingFields={lead.conflictingFields} />
              </TableCell>
              <TableCell>
                <ScorePercent value={lead.finalScore} />
              </TableCell>
              <TableCell>
                <ConfidenceCell
                  identityConfidence={lead.identityConfidence}
                  icpScore={lead.icpScore}
                  commercialScore={lead.commercialScore}
                  evidenceScore={lead.evidenceScore}
                />
              </TableCell>
              <TableCell sx={{ whiteSpace: "nowrap" }}>{lead.qualificationDecision ?? <Unavailable />}</TableCell>
              <TableCell>
                <WhyCell reasonCodes={lead.rankingReasonCodes} qualificationSummary={lead.qualificationSummary} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
