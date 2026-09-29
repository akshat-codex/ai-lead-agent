"use client";

import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import HelpOutlineIcon from "@mui/icons-material/HelpOutlineOutlined";
import CancelIcon from "@mui/icons-material/Cancel";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import { reasonCodeLabel, reasonCodeSentiment } from "@/lib/ranking/reasonCodes";

interface EvidenceChecklistProps {
  reasonCodes: string[];
}

const ICONS = {
  met: <CheckCircleIcon color="success" fontSize="small" />,
  not_met: <CancelIcon color="disabled" fontSize="small" />,
  unknown: <HelpOutlineIcon color="disabled" fontSize="small" />,
};

/**
 * Built ONLY from the backend's own RankingReasonCode values — never
 * frontend-invented sentences. See lib/ranking/reasonCodes.ts for the label
 * lookup on the backend enum.
 */
export default function EvidenceChecklist({ reasonCodes }: EvidenceChecklistProps) {
  if (reasonCodes.length === 0) return null;

  return (
    <List dense disablePadding>
      {reasonCodes.map((code) => {
        const sentiment = reasonCodeSentiment(code);
        return (
          <ListItem key={code} disableGutters disablePadding sx={{ py: 0.25 }}>
            <ListItemIcon sx={{ minWidth: 26 }}>{ICONS[sentiment]}</ListItemIcon>
            <ListItemText
              slotProps={{ primary: { variant: "body2", color: sentiment === "not_met" ? "text.secondary" : "text.primary" } }}
              primary={reasonCodeLabel(code)}
            />
          </ListItem>
        );
      })}
    </List>
  );
}
