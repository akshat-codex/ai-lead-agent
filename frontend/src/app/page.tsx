"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function fetchHealth() {
  const res = await fetch(`${API_BASE_URL}/health`);
  if (!res.ok) {
    throw new Error(`Backend health check failed: ${res.status}`);
  }
  return res.json() as Promise<{ status: string; service: string; environment: string }>;
}

export default function Home() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    retry: false,
  });

  return (
    <Container maxWidth="sm">
      <Box sx={{ py: 12 }}>
        <Stack spacing={3}>
          <Typography variant="h3" component="h1" sx={{ fontWeight: 700 }}>
            Find your next best customers
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Describe your ideal customer profile, discover matching companies, surface the right
            decision-makers, and build outreach-ready leads — all in one guided flow.
          </Typography>
          <Stack direction="row" spacing={2} sx={{ alignItems: "center" }}>
            <Button component={Link} href="/leads/new" variant="contained" size="large" endIcon={<ArrowForwardIcon />} disableElevation>
              Start a search
            </Button>
          </Stack>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", pt: 2 }}>
            <Typography variant="caption" color="text.secondary">
              Backend status:
            </Typography>
            {isLoading && <Chip label="checking..." size="small" />}
            {error && <Chip label="unreachable" color="error" size="small" />}
            {data && (
              <Chip
                label={`${data.status} (${data.environment})`}
                color="success"
                size="small"
              />
            )}
          </Stack>
        </Stack>
      </Box>
    </Container>
  );
}
