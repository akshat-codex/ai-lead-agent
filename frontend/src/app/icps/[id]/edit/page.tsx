"use client";

import { use } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import IcpBuilder from "@/components/icp/IcpBuilder";
import ErrorState, { errorMessage } from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import { getIcp } from "@/lib/icp/api";

export default function EditIcpPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const queryClient = useQueryClient();

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["icps", id],
    queryFn: () => getIcp(id),
  });

  return (
    <Container maxWidth="md">
      <Stack spacing={3} sx={{ py: 6 }}>
        <Typography variant="h4" component="h1">
          New version{data ? ` — ${data.name}` : ""}
        </Typography>

        {isLoading && <LoadingState label="Loading ICP..." />}

        {error && (
          <ErrorState
            message={`Could not load this ICP. ${errorMessage(error)}`}
            onRetry={() => refetch()}
          />
        )}

        {data && (
          <IcpBuilder
            mode="new-version"
            initialDraft={{
              name: data.name,
              hardRules: data.hardRules,
              softPreferences: data.softPreferences,
            }}
            onSaved={() => {
              queryClient.invalidateQueries({ queryKey: ["icps"] });
              router.push("/icps");
            }}
          />
        )}
      </Stack>
    </Container>
  );
}
