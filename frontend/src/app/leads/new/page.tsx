"use client";

import { useRouter } from "next/navigation";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import SearchDefinitionBuilder from "@/components/filters/SearchDefinitionBuilder";

export default function NewLeadSearchPage() {
  const router = useRouter();

  return (
    <Container maxWidth="lg">
      <Stack spacing={3} sx={{ py: 6 }}>
        <Stack spacing={0.5}>
          <Typography variant="h4" component="h1">
            Define your ideal customer
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Describe who you&apos;re looking for in plain language, or build the search field by field.
          </Typography>
        </Stack>
        <SearchDefinitionBuilder onConfirmed={(icpId) => router.push(`/leads/${icpId}/companies`)} />
      </Stack>
    </Container>
  );
}
