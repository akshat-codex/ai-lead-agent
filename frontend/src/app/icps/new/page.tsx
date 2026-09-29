"use client";

import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import IcpBuilder from "@/components/icp/IcpBuilder";

export default function NewIcpPage() {
  const router = useRouter();
  const queryClient = useQueryClient();

  return (
    <Container maxWidth="md">
      <Stack spacing={3} sx={{ py: 6 }}>
        <Typography variant="h4" component="h1">
          New ICP
        </Typography>
        <IcpBuilder
          mode="create"
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ["icps"] });
            router.push("/icps");
          }}
        />
      </Stack>
    </Container>
  );
}
