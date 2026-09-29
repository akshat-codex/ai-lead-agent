"use client";

import { use, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQueries, useQuery } from "@tanstack/react-query";
import GroupsOutlinedIcon from "@mui/icons-material/GroupsOutlined";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import PeopleDiscoveryTrigger from "@/components/people/PeopleDiscoveryTrigger";
import PeopleResultsList from "@/components/people/PeopleResultsList";
import EmptyState from "@/components/ui/EmptyState";
import ErrorState, { errorMessage } from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import { getCompany } from "@/lib/companies/api";
import type { CanonicalCompany } from "@/lib/companies/types";
import { getIcp } from "@/lib/icp/api";
import { useLeadAgentSession } from "@/lib/leads/sessionContext";
import { usePeopleDiscoveryPipeline } from "@/lib/peopleDiscovery/usePeopleDiscoveryPipeline";

export default function PeoplePage({ params }: { params: Promise<{ icpId: string }> }) {
  const { icpId } = use(params);
  const router = useRouter();
  const { setIcpId, selectedCompanyIds } = useLeadAgentSession();

  useEffect(() => {
    setIcpId(icpId);
  }, [icpId, setIcpId]);

  const icpQuery = useQuery({ queryKey: ["icps", icpId], queryFn: () => getIcp(icpId) });
  const pipeline = usePeopleDiscoveryPipeline(icpId);

  const companyQueries = useQueries({
    queries: selectedCompanyIds.map((companyId) => ({
      queryKey: ["companies", companyId],
      queryFn: () => getCompany(companyId),
    })),
  });
  const companies: Record<string, CanonicalCompany> = {};
  selectedCompanyIds.forEach((companyId, index) => {
    const data = companyQueries[index]?.data;
    if (data) companies[companyId] = data;
  });

  const runPipeline = () => pipeline.run(selectedCompanyIds);
  const retryCompany = () => pipeline.run(selectedCompanyIds);

  return (
    <Container maxWidth="lg">
      <Stack spacing={3.5} sx={{ py: { xs: 3, sm: 5 } }}>
        <Stack spacing={0.5}>
          <Typography variant="h4" component="h1">
            People{icpQuery.data ? ` for "${icpQuery.data.name}"` : ""}
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Decision-makers at your selected companies — grouped by company for easy review.
          </Typography>
        </Stack>

        {icpQuery.data && icpQuery.data.hardRules.allowedTitles.length > 0 && (
          <Typography variant="body2" color="text.secondary">
            Searching for: {icpQuery.data.hardRules.allowedTitles.join(", ")}
          </Typography>
        )}

        {icpQuery.isLoading && <LoadingState label="Loading search..." />}
        {icpQuery.error && (
          <ErrorState
            message={`Could not load this search. ${errorMessage(icpQuery.error)}`}
            onRetry={() => icpQuery.refetch()}
          />
        )}

        {icpQuery.data && selectedCompanyIds.length === 0 && (
          <EmptyState
            icon={<GroupsOutlinedIcon fontSize="small" />}
            title="No companies selected"
            description="Go back to step 2 and select at least one company before finding decision-makers."
            action={
              <Link href={`/leads/${icpId}/companies`}>
                <Typography variant="body2" color="primary">
                  Back to companies
                </Typography>
              </Link>
            }
          />
        )}

        {icpQuery.data && selectedCompanyIds.length > 0 && (
          <>
            {pipeline.phase !== "done" && (
              <PeopleDiscoveryTrigger
                phase={pipeline.phase}
                statusLabel={pipeline.statusLabel}
                error={pipeline.error}
                companyCount={selectedCompanyIds.length}
                onRun={runPipeline}
              />
            )}

            {pipeline.phase === "done" && pipeline.result && (
              <PeopleResultsList
                companies={companies}
                result={pipeline.result}
                onRetryCompany={retryCompany}
                onContinue={() => router.push(`/leads/${icpId}/enrich`)}
              />
            )}
          </>
        )}
      </Stack>
    </Container>
  );
}
