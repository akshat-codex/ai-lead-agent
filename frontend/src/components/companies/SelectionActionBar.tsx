"use client";

import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface SelectionActionBarProps {
  count: number;
  onContinue: () => void;
}

export default function SelectionActionBar({ count, onContinue }: SelectionActionBarProps) {
  if (count === 0) return null;

  return (
    <Box sx={{ position: "sticky", bottom: 0, py: 2, zIndex: 1 }}>
      <Paper
        elevation={4}
        sx={{
          p: 2,
          display: "flex",
          justifyContent: "center",
          borderRadius: 3,
          border: "1px solid",
          borderColor: "divider",
        }}
      >
        <Stack direction="row" spacing={2.5} sx={{ alignItems: "center" }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {count} {count === 1 ? "company" : "companies"} selected
          </Typography>
          <Button variant="contained" endIcon={<ArrowForwardIcon />} onClick={onContinue} disableElevation>
            Find decision-makers
          </Button>
        </Stack>
      </Paper>
    </Box>
  );
}
