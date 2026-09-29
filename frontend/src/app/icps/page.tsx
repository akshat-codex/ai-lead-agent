"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import DescriptionOutlinedIcon from "@mui/icons-material/DescriptionOutlined";
import EditOutlinedIcon from "@mui/icons-material/EditOutlined";
import VisibilityOutlinedIcon from "@mui/icons-material/VisibilityOutlined";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import IcpSummary from "@/components/icp/IcpSummary";
import EmptyState from "@/components/ui/EmptyState";
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
      <Stack spacing={4} sx={{ py: { xs: 4, sm: 6 } }}>
        <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", rowGap: 2 }}>
          <Stack spacing={0.5}>
            <Typography variant="h4" component="h1">
              Saved ICPs
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Ideal customer profiles you&apos;ve defined, with every saved version.
            </Typography>
          </Stack>
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
          <EmptyState
            icon={<DescriptionOutlinedIcon fontSize="small" />}
            title="No ICPs saved yet"
            description="Create your first ideal customer profile to start finding matching companies."
            action={
              <Button component={Link} href="/icps/new" variant="contained" startIcon={<AddIcon />} sx={{ mt: 1 }}>
                New ICP
              </Button>
            }
          />
        )}

        <Stack spacing={2}>
          {Array.from(groups.entries()).map(([name, versions]) => {
            const latest = versions[0];
            return (
              <Paper key={name} variant="outlined" sx={{ borderRadius: 2.5, p: 2.5 }}>
                <Stack spacing={1.5}>
                  <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center" }}>
                    <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                      {name}
                    </Typography>
                    <Tooltip title="Create new version">
                      <IconButton
                        component={Link}
                        href={`/icps/${latest.id}/edit`}
                        aria-label={`Create a new version of ${name}`}
                        size="small"
                      >
                        <EditOutlinedIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </Stack>

                  <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
                    {versions.map((icp) => (
                      <Chip
                        key={icp.id}
                        label={`v${icp.version}`}
                        color={icp === latest ? "primary" : "default"}
                        variant={expandedId === icp.id ? "filled" : "outlined"}
                        icon={<VisibilityOutlinedIcon />}
                        onClick={() => setExpandedId(expandedId === icp.id ? null : icp.id)}
                        sx={expandedId !== icp.id ? { borderColor: "divider" } : undefined}
                      />
                    ))}
                  </Stack>

                  {versions
                    .filter((icp) => icp.id === expandedId)
                    .map((icp) => (
                      <Box
                        key={icp.id}
                        sx={{ pt: 1.5, mt: 0.5, borderTop: "1px solid", borderColor: "divider" }}
                      >
                        <IcpSummary draft={icp} />
                      </Box>
                    ))}
                </Stack>
              </Paper>
            );
          })}
        </Stack>
      </Stack>
    </Container>
  );
}
