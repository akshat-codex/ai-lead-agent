"use client";

import { use, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useQueries } from "@tanstack/react-query";
import AutorenewIcon from "@mui/icons-material/Autorenew";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import PersonEnrichmentRow from "@/components/enrichment/PersonEnrichmentRow";
import EmptyState from "@/components/ui/EmptyState";
import LoadingState from "@/components/ui/LoadingState";
import { getCompany } from "@/lib/companies/api";
import { useLeadAgentSession } from "@/lib/leads/sessionContext";
import { getPerson } from "@/lib/people/api";
import { useEnrichmentPipeline } from "@/lib/personEnrichment/useEnrichmentPipeline";

export default function EnrichPage({ params }: { params: Promise<{ icpId: string }> }) {
  const { icpId } = use(params);
  const router = useRouter();
  const { setIcpId, selectedPersonIds } = useLeadAgentSession();

  useEffect(() => {
    setIcpId(icpId);
  }, [icpId, setIcpId]);

  const pipeline = useEnrichmentPipeline();

  const personQueries = useQueries({
    queries: selectedPersonIds.map((personId) => ({
      queryKey: ["people", personId],
      queryFn: () => getPerson(personId),
    })),
  });

  const companyIds = [
    ...new Set(personQueries.map((q) => q.data?.canonicalCompanyId).filter((id): id is string => Boolean(id))),
  ];
  const companyQueries = useQueries({
    queries: companyIds.map((companyId) => ({
      queryKey: ["companies", companyId],
      queryFn: () => getCompany(companyId),
    })),
  });
  const companyNameById: Record<string, string> = {};
  companyIds.forEach((companyId, index) => {
    const data = companyQueries[index]?.data;
    if (data) companyNameById[companyId] = data.canonicalName;
  });

  const peopleLoading = personQueries.some((q) => q.isLoading);

  return (
    <Container maxWidth="lg">
      <Stack spacing={3} sx={{ py: 4 }}>
        <Stack spacing={0.5}>
          <Typography variant="h4" component="h1">
            Enrich contacts
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Fill in verified emails, phone numbers, and profile details for your selected people.
          </Typography>
        </Stack>

        {selectedPersonIds.length === 0 && (
          <EmptyState
            title="No people selected"
            description="Go back to step 3 and select at least one person before enriching contacts."
          />
        )}

        {selectedPersonIds.length > 0 && (
          <>
            {peopleLoading && <LoadingState label="Loading selected people..." />}

            {!peopleLoading && pipeline.phase === "idle" && (
              <Stack spacing={1.5}>
                <Typography variant="body2" color="text.secondary">
                  Ready to enrich {selectedPersonIds.length} selected {selectedPersonIds.length === 1 ? "contact" : "contacts"}.
                </Typography>
                <Stack direction="row">
                  <Button
                    variant="contained"
                    startIcon={<AutorenewIcon />}
                    onClick={() => pipeline.run(selectedPersonIds)}
                  >
                    Enrich {selectedPersonIds.length} {selectedPersonIds.length === 1 ? "contact" : "contacts"}
                  </Button>
                </Stack>
              </Stack>
            )}

            {(pipeline.phase === "running" || pipeline.phase === "done") && (
              <Stack spacing={2}>
                {pipeline.phase === "running" && <LoadingState label="Enriching contacts..." />}
                {selectedPersonIds.map((personId) => {
                  const person = personQueries.find((_, i) => selectedPersonIds[i] === personId)?.data;
                  const rowState = pipeline.byPersonId[personId];
                  if (!person || !rowState) return null;
                  return (
                    <PersonEnrichmentRow
                      key={personId}
                      name={person.canonicalName}
                      companyName={person.canonicalCompanyId ? (companyNameById[person.canonicalCompanyId] ?? null) : null}
                      state={rowState}
                    />
                  );
                })}
              </Stack>
            )}

            {pipeline.phase === "done" && (
              <Stack direction="row" sx={{ justifyContent: "flex-end", pt: 2 }}>
                <Button variant="contained" onClick={() => router.push(`/leads/${icpId}/review`)}>
                  Continue to Review
                </Button>
              </Stack>
            )}
          </>
        )}
      </Stack>
    </Container>
  );
}
