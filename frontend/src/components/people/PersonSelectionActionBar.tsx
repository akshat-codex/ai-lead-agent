"use client";

import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface PersonSelectionActionBarProps {
  count: number;
  onContinue: () => void;
}

export default function PersonSelectionActionBar({ count, onContinue }: PersonSelectionActionBarProps) {
  if (count === 0) return null;

  return (
    <Box sx={{ position: "sticky", bottom: 0, py: 2, zIndex: 1 }}>
      <Paper elevation={3} sx={{ p: 2, display: "flex", justifyContent: "center" }}>
        <Stack direction="row" spacing={2} sx={{ alignItems: "center" }}>
          <Typography variant="body2">
            {count} {count === 1 ? "person" : "people"} selected
          </Typography>
          <Button variant="contained" endIcon={<ArrowForwardIcon />} onClick={onContinue}>
            Enrich contacts
          </Button>
        </Stack>
      </Paper>
    </Box>
  );
}
