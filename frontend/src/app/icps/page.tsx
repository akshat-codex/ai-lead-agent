"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import EditIcon from "@mui/icons-material/Edit";
import VisibilityIcon from "@mui/icons-material/Visibility";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import IcpSummary from "@/components/icp/IcpSummary";
import ErrorState, { errorMessage } from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import { listIcps } from "@/lib/icp/api";
import type { SavedIcp } from "@/lib/icp/types";

function groupByName(icps: SavedIcp[]): Map<string, SavedIcp[]> {
  const groups = new Map<string, SavedIcp[]>();
  for (const icp of icps) {
    const existing = groups.get(icp.name) ?? [];
    existing.push(icp);
    groups.set(icp.name, existing);
  }
  for (const versions of groups.values()) {
    versions.sort((a, b) => b.version - a.version);
  }
  return groups;
}

export default function IcpListPage() {
  const { data, error, isLoading, refetch } = useQuery({ queryKey: ["icps"], queryFn: listIcps });
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const groups = useMemo(() => groupByName(data ?? []), [data]);

  return (
    <Container maxWidth="md">
      <Stack spacing={3} sx={{ py: 6 }}>
        <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center" }}>
          <Typography variant="h4" component="h1">
            Saved ICPs
          </Typography>
          <Button component={Link} href="/icps/new" variant="contained" startIcon={<AddIcon />}>
            New ICP
          </Button>
        </Stack>

        {isLoading && <LoadingState label="Loading saved ICPs..." />}

        {error && (
          <ErrorState
            message={`Could not load ICPs from the backend. ${errorMessage(error)}`}
            onRetry={() => refetch()}
          />
        )}

        {data && data.length === 0 && (
          <Alert severity="info">
            No ICPs saved yet. Click &quot;New ICP&quot; to create your first one.
          </Alert>
        )}

        {Array.from(groups.entries()).map(([name, versions]) => {
          const latest = versions[0];
          return (
            <Stack
              key={name}
              spacing={1}
              sx={{ border: "1px solid", borderColor: "divider", borderRadius: 1, p: 2 }}
            >
              <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center" }}>
                <Typography variant="h6">{name}</Typography>
                <IconButton
                  component={Link}
                  href={`/icps/${latest.id}/edit`}
                  aria-label={`Create a new version of ${name}`}
                  title="Create new version"
                >
                  <EditIcon fontSize="small" />
                </IconButton>
              </Stack>

              <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
                {versions.map((icp) => (
                  <Chip
                    key={icp.id}
                    label={`v${icp.version}`}
                    color={icp === latest ? "primary" : "default"}
                    variant={expandedId === icp.id ? "filled" : "outlined"}
                    icon={<VisibilityIcon />}
                    onClick={() => setExpandedId(expandedId === icp.id ? null : icp.id)}
                  />
                ))}
              </Stack>

              {versions
                .filter((icp) => icp.id === expandedId)
                .map((icp) => (
                  <Box key={icp.id} sx={{ pt: 1 }}>
                    <IcpSummary draft={icp} />
                  </Box>
                ))}
            </Stack>
          );
        })}
      </Stack>
    </Container>
  );
}
