"use client";

import LaunchIcon from "@mui/icons-material/Launch";
import Link from "@mui/material/Link";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import Paper from "@mui/material/Paper";
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
    <TableContainer component={Paper} variant="outlined">
      <Table size="small">
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
            <TableCell>Qualification</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {leads.map((lead) => (
            <TableRow key={lead.leadId} hover>
              <TableCell>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {lead.companyName ?? <Unavailable />}
                </Typography>
                {lead.companyDomain && (
                  <Typography variant="caption" color="text.secondary">
                    {lead.companyDomain}
                  </Typography>
                )}
              </TableCell>
              <TableCell>{lead.personName ?? <Unavailable />}</TableCell>
              <TableCell>{lead.title ?? <Unavailable />}</TableCell>
              <TableCell>
                <LinkedInCell url={lead.companyLinkedinUrl} />
              </TableCell>
              <TableCell>
                <LinkedInCell url={lead.personLinkedinUrl} />
              </TableCell>
              <TableCell>{lead.email ?? <Unavailable />}</TableCell>
              <TableCell>{lead.emailStatus ?? <Unavailable />}</TableCell>
              <TableCell>{lead.phone ?? <Unavailable />}</TableCell>
              <TableCell>
                <StatusChip status={lead.enrichmentStatus === "enriched" ? "done" : "idle"} label={lead.enrichmentStatus === "enriched" ? "Enriched" : "Not enriched"} />
              </TableCell>
              <TableCell>{lead.tier ? <FitBadge tier={rankTierToUiTier(lead.tier as RankTier)} /> : <Unavailable />}</TableCell>
              <TableCell>
                <ScorePercent value={lead.finalScore} />
              </TableCell>
              <TableCell>{lead.qualificationDecision ?? <Unavailable />}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
