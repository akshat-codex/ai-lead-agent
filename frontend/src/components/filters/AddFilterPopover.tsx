"use client";

import { useMemo, useState } from "react";
import AddIcon from "@mui/icons-material/Add";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Divider from "@mui/material/Divider";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Popover from "@mui/material/Popover";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { CUSTOM_FILTER_KEY } from "@/lib/filters/types";
import type { FilterDefinition } from "@/lib/filters/types";

interface AddFilterPopoverProps {
  catalog: FilterDefinition[];
  /** Keys already present in the criteria list — hidden from the picker
   * unless they support multiple criteria of the same key. */
  usedKeys: Set<string>;
  onSelect: (definition: FilterDefinition) => void;
  onAddCustom: (label: string) => void;
}

export default function AddFilterPopover({ catalog, usedKeys, onSelect, onAddCustom }: AddFilterPopoverProps) {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
  const [search, setSearch] = useState("");

  const results = useMemo(() => {
    const q = search.trim().toLowerCase();
    return catalog
      .filter((d) => d.key !== CUSTOM_FILTER_KEY)
      .filter((d) => !usedKeys.has(d.key))
      .filter((d) => !q || d.label.toLowerCase().includes(q) || d.description?.toLowerCase().includes(q) || d.key.toLowerCase().includes(q));
  }, [catalog, search, usedKeys]);

  const handleClose = () => {
    setAnchorEl(null);
    setSearch("");
  };

  return (
    <>
      <Button startIcon={<AddIcon />} variant="outlined" onClick={(e) => setAnchorEl(e.currentTarget)}>
        Add filter
      </Button>
      <Popover
        open={Boolean(anchorEl)}
        anchorEl={anchorEl}
        onClose={handleClose}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        slotProps={{ paper: { elevation: 4 } }}
      >
        <Box sx={{ width: 320, maxWidth: "90vw", p: 1.5 }}>
          <TextField
            autoFocus
            fullWidth
            size="small"
            placeholder="Search filters (industry, funding, revenue, ...)"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <List dense sx={{ maxHeight: 280, overflowY: "auto", mt: 1 }}>
            {results.map((def) => (
              <ListItemButton
                key={def.key}
                onClick={() => {
                  onSelect(def);
                  handleClose();
                }}
              >
                <ListItemText primary={def.label} secondary={def.description} />
              </ListItemButton>
            ))}
            {results.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ px: 2, py: 1 }}>
                No matching filter.
              </Typography>
            )}
          </List>
          {search.trim() && (
            <>
              <Divider sx={{ my: 1 }} />
              <ListItemButton
                onClick={() => {
                  onAddCustom(search.trim());
                  handleClose();
                }}
              >
                <ListItemText
                  primary={`Add "${search.trim()}" as custom requirement`}
                  secondary="No dedicated filter for this yet — saved as free text."
                />
              </ListItemButton>
            </>
          )}
        </Box>
      </Popover>
    </>
  );
}
