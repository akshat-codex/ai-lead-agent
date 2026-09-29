"use client";

import { use, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import BatchCostSummary from "@/components/companies/BatchCostSummary";
import BatchDiscoveryTrigger from "@/components/companies/BatchDiscoveryTrigger";
import CompanyRankingList from "@/components/companies/CompanyRankingList";
import FindMoreLeadsButton from "@/components/companies/FindMoreLeadsButton";
import ErrorState, { errorMessage } from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import { getIcp } from "@/lib/icp/api";
import { useBatchDiscovery } from "@/lib/companies/useBatchDiscovery";
import { useLeadAgentSession } from "@/lib/leads/sessionContext";

export default function CompaniesPage({ params }: { params: Promise<{ icpId: string }> }) {
  const { icpId } = use(params);
  const router = useRouter();
  const { setIcpId, setBatchId } = useLeadAgentSession();

  useEffect(() => {
    setIcpId(icpId);
  }, [icpId, setIcpId]);

  const icpQuery = useQuery({ queryKey: ["icps", icpId], queryFn: () => getIcp(icpId) });
  const discovery = useBatchDiscovery(icpId);

  useEffect(() => {
    if (discovery.batch) setBatchId(discovery.batch.id);
  }, [discovery.batch, setBatchId]);

  return (
    <Container maxWidth="lg">
      <Stack spacing={3.5} sx={{ py: { xs: 3, sm: 5 } }}>
        <Stack spacing={0.75}>
          <Typography variant="h4" component="h1">
            Companies{icpQuery.data ? ` for "${icpQuery.data.name}"` : ""}
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Ranked by fit — review why each company matched, then select the ones worth pursuing.
          </Typography>
        </Stack>

        {icpQuery.isLoading && <LoadingState label="Loading search..." />}
        {icpQuery.error && (
          <ErrorState
            message={`Could not load this search. ${errorMessage(icpQuery.error)}`}
            onRetry={() => icpQuery.refetch()}
          />
        )}

        {icpQuery.data && (
          <>
            {(discovery.phase === "idle" || discovery.phase === "first-page" || (discovery.phase === "error" && !discovery.result)) && (
              <BatchDiscoveryTrigger phase={discovery.phase} error={discovery.error} onRun={discovery.start} />
            )}

            {discovery.result && (
              <>
                {discovery.batch && <BatchCostSummary batch={discovery.batch} />}
                <CompanyRankingList
                  result={discovery.result}
                  attributes={discovery.attributes}
                  onContinue={() => router.push(`/leads/${icpId}/people`)}
                  hermesSearching={discovery.phase === "hermes-searching"}
                />
                <FindMoreLeadsButton
                  phase={discovery.phase}
                  resultCount={discovery.result.rankedLeads.length}
                  targetCount={discovery.targetCount}
                  acceptedCount={discovery.batch?.acceptedCount ?? 0}
                  errorCode={discovery.batch?.discoveryErrorCode ?? null}
                  onFindMore={() => discovery.batch && discovery.findMore(discovery.batch.id)}
                />
              </>
            )}
          </>
        )}
      </Stack>
    </Container>
  );
}
