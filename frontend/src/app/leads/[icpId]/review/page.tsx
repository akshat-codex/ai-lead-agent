"use client";

import { use, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQueries, useQuery } from "@tanstack/react-query";
import TableChartOutlinedIcon from "@mui/icons-material/TableChartOutlined";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import ExportActionBar from "@/components/review/ExportActionBar";
import ReviewFilterBar from "@/components/review/ReviewFilterBar";
import type { ReviewFilters } from "@/components/review/ReviewFilterBar";
import ReviewLeadTable from "@/components/review/ReviewLeadTable";
import EmptyState from "@/components/ui/EmptyState";
import ErrorState, { errorMessage } from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import { getCompany, getCompanyFacts } from "@/lib/companies/api";
import { getPersonEvidence } from "@/lib/evidence/api";
import { downloadCsv } from "@/lib/export/download";
import { getExport } from "@/lib/export/api";
import { buildReviewCsv } from "@/lib/export/csv";
import { filterReviewLeads } from "@/lib/export/filterLeads";
import { buildReviewLead } from "@/lib/export/reviewLead";
import { getIcp } from "@/lib/icp/api";
import { useLeadAgentSession } from "@/lib/leads/sessionContext";
import { getPerson } from "@/lib/people/api";

export default function ReviewPage({ params }: { params: Promise<{ icpId: string }> }) {
  const { icpId } = use(params);
  const router = useRouter();
  const { setIcpId, selectedPersonIds, reset } = useLeadAgentSession();
  const [filters, setFilters] = useState<ReviewFilters>({ search: "", tier: "all", enrichment: "all" });

  useEffect(() => {
    setIcpId(icpId);
  }, [icpId, setIcpId]);

  const icpQuery = useQuery({ queryKey: ["icps", icpId], queryFn: () => getIcp(icpId) });
  const exportQuery = useQuery({ queryKey: ["exports", icpId], queryFn: () => getExport(icpId) });

  const selectedLeads = useMemo(() => {
    if (!exportQuery.data) return [];
    const selectedSet = new Set(selectedPersonIds);
    return exportQuery.data.leads.filter((lead) => lead.identity.personId && selectedSet.has(lead.identity.personId));
  }, [exportQuery.data, selectedPersonIds]);

  const personIds = useMemo(
    () => selectedLeads.map((l) => l.identity.personId).filter((id): id is string => Boolean(id)),
    [selectedLeads],
  );
  const companyIds = useMemo(() => [...new Set(selectedLeads.map((l) => l.identity.companyId))], [selectedLeads]);

  const personQueries = useQueries({
    queries: personIds.map((personId) => ({ queryKey: ["people", personId], queryFn: () => getPerson(personId) })),
  });
  const evidenceQueries = useQueries({
    queries: personIds.map((personId) => ({ queryKey: ["evidence", "PERSON", personId], queryFn: () => getPersonEvidence(personId) })),
  });
  const companyQueries = useQueries({
    queries: companyIds.map((companyId) => ({ queryKey: ["companies", companyId], queryFn: () => getCompany(companyId) })),
  });
  const companyFactsQueries = useQueries({
    queries: companyIds.map((companyId) => ({ queryKey: ["companies", companyId, "facts"], queryFn: () => getCompanyFacts(companyId) })),
  });

  const detailLoading =
    personQueries.some((q) => q.isLoading) ||
    evidenceQueries.some((q) => q.isLoading) ||
    companyQueries.some((q) => q.isLoading) ||
    companyFactsQueries.some((q) => q.isLoading);

  const reviewLeads = useMemo(() => {
    const personById = new Map(personIds.map((id, i) => [id, personQueries[i]?.data]));
    const evidenceById = new Map(personIds.map((id, i) => [id, evidenceQueries[i]?.data ?? []]));
    const companyById = new Map(companyIds.map((id, i) => [id, companyQueries[i]?.data]));
    const companyFactsById = new Map(companyIds.map((id, i) => [id, companyFactsQueries[i]?.data]));

    return selectedLeads.map((lead) =>
      buildReviewLead(
        lead,
        lead.identity.personId ? personById.get(lead.identity.personId) : undefined,
        lead.identity.personId ? (evidenceById.get(lead.identity.personId) ?? []) : [],
        companyById.get(lead.identity.companyId),
        companyFactsById.get(lead.identity.companyId),
      ),
    );
  }, [selectedLeads, personIds, companyIds, personQueries, evidenceQueries, companyQueries, companyFactsQueries]);

  const filteredLeads = useMemo(() => filterReviewLeads(reviewLeads, filters), [reviewLeads, filters]);

  const handleExportCsv = () => {
    const csv = buildReviewCsv(filteredLeads);
    downloadCsv(csv, `leads-${icpId}.csv`);
  };

  const handleStartOver = () => {
    reset();
    router.push("/leads/new");
  };

  return (
    <Container maxWidth="lg">
      <Stack spacing={3} sx={{ py: { xs: 3, sm: 5 } }}>
        <Stack spacing={0.5}>
          <Typography variant="h4" component="h1">
            Review{icpQuery.data ? ` — "${icpQuery.data.name}"` : ""}
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Final check before export — filter, verify, and download your leads.
          </Typography>
        </Stack>

        {icpQuery.isLoading && <LoadingState label="Loading search..." />}
        {icpQuery.error && (
          <ErrorState message={`Could not load this search. ${errorMessage(icpQuery.error)}`} onRetry={() => icpQuery.refetch()} />
        )}

        {icpQuery.data && selectedPersonIds.length === 0 && (
          <EmptyState
            icon={<TableChartOutlinedIcon fontSize="small" />}
            title="No people selected"
            description="Go back to step 3 and select the people you want to review before exporting."
          />
        )}

        {icpQuery.data && selectedPersonIds.length > 0 && exportQuery.isLoading && (
          <LoadingState label="Assembling final leads..." />
        )}

        {icpQuery.data && selectedPersonIds.length > 0 && exportQuery.error && (
          <ErrorState
            message={`Could not load the assembled leads. ${errorMessage(exportQuery.error)}`}
            onRetry={() => exportQuery.refetch()}
          />
        )}

        {icpQuery.data && selectedPersonIds.length > 0 && exportQuery.data && selectedLeads.length === 0 && (
          <EmptyState
            icon={<TableChartOutlinedIcon fontSize="small" />}
            title="Selected people are not ranked yet"
            description="The selected people don't have a ranked lead for this search yet. Go back to step 3 and try again."
          />
        )}

        {icpQuery.data && selectedPersonIds.length > 0 && exportQuery.data && selectedLeads.length > 0 && (
          <>
            {detailLoading && <LoadingState label="Loading contact details..." />}

            {!detailLoading && (
              <Stack spacing={2.5}>
                <ReviewFilterBar filters={filters} onChange={setFilters} />
                <ReviewLeadTable leads={filteredLeads} />
                <ExportActionBar leadCount={filteredLeads.length} onExportCsv={handleExportCsv} onStartOver={handleStartOver} />
              </Stack>
            )}
          </>
        )}
      </Stack>
    </Container>
  );
}
