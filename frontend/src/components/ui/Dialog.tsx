"use client";

import type { ReactNode } from "react";
import CloseIcon from "@mui/icons-material/Close";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import MuiDialog from "@mui/material/Dialog";
import Stack from "@mui/material/Stack";
import type { DialogProps as MuiDialogProps } from "@mui/material/Dialog";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  maxWidth?: MuiDialogProps["maxWidth"];
}

/**
 * Shared modal primitive — no component in this codebase used MUI's Dialog
 * before this (the only prior overlay pattern was Popover); this exists so
 * a future confirmation/detail-view modal has one consistent, already-
 * themed starting point rather than each screen inventing its own.
 * Presentation only, no business logic.
 */
export default function Dialog({ open, onClose, title, children, maxWidth = "sm" }: DialogProps) {
  return (
    <MuiDialog open={open} onClose={onClose} maxWidth={maxWidth} fullWidth>
      <DialogTitle sx={{ pr: 6 }}>
        <Stack direction="row" sx={{ alignItems: "center", justifyContent: "space-between" }}>
          {title}
        </Stack>
        <IconButton
          onClick={onClose}
          aria-label="Close"
          size="small"
          sx={{ position: "absolute", right: 12, top: 12, color: "text.secondary" }}
        >
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>{children}</DialogContent>
    </MuiDialog>
  );
}
