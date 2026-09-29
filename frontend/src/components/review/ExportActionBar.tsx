"use client";

import DownloadIcon from "@mui/icons-material/Download";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface ExportActionBarProps {
  leadCount: number;
  onExportCsv: () => void;
  onStartOver: () => void;
}

export default function ExportActionBar({ leadCount, onExportCsv, onStartOver }: ExportActionBarProps) {
  return (
    <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ alignItems: { sm: "center" }, justifyContent: "space-between" }}>
      <Typography variant="body2" color="text.secondary">
        {leadCount} {leadCount === 1 ? "lead" : "leads"} ready for export
      </Typography>
      <Stack direction="row" spacing={1.5}>
        <Button variant="outlined" startIcon={<RestartAltIcon />} onClick={onStartOver}>
          Start over
        </Button>
        <Button variant="contained" startIcon={<DownloadIcon />} onClick={onExportCsv} disabled={leadCount === 0}>
          Export CSV
        </Button>
      </Stack>
    </Stack>
  );
}
