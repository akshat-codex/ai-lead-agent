"use client";

import DeleteOutlineIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import type { CustomRuleErrors } from "@/lib/icp/validation";
import type { CustomRuleItem } from "@/lib/icp/types";

interface CustomRuleListEditorProps {
  title: string;
  addLabel: string;
  emptyHint: string;
  items: CustomRuleItem[];
  errors?: CustomRuleErrors;
  onChange: (items: CustomRuleItem[]) => void;
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `rule-${Date.now()}-${Math.random()}`;
}

export default function CustomRuleListEditor({
  title,
  addLabel,
  emptyHint,
  items,
  errors,
  onChange,
}: CustomRuleListEditorProps) {
  const updateItem = (id: string, patch: Partial<CustomRuleItem>) => {
    onChange(items.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  };

  const removeItem = (id: string) => {
    onChange(items.filter((item) => item.id !== id));
  };

  const addItem = () => {
    onChange([...items, { id: newId(), label: "", description: "" }]);
  };

  return (
    <Stack spacing={1.5}>
      <Typography variant="subtitle2">{title}</Typography>

      {items.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {emptyHint}
        </Typography>
      )}

      {items.map((item) => {
        const itemErrors = errors?.[item.id];
        return (
          <Stack key={item.id} direction="row" spacing={1} sx={{ alignItems: "flex-start" }}>
            <TextField
              label="Label"
              size="small"
              value={item.label}
              onChange={(e) => updateItem(item.id, { label: e.target.value })}
              error={Boolean(itemErrors?.label)}
              helperText={itemErrors?.label}
              sx={{ flex: 1 }}
            />
            <TextField
              label="Description"
              size="small"
              value={item.description}
              onChange={(e) => updateItem(item.id, { description: e.target.value })}
              error={Boolean(itemErrors?.description)}
              helperText={itemErrors?.description}
              sx={{ flex: 2 }}
            />
            <IconButton
              aria-label="Remove rule"
              onClick={() => removeItem(item.id)}
              sx={{ mt: 0.5 }}
            >
              <DeleteOutlineIcon fontSize="small" />
            </IconButton>
          </Stack>
        );
      })}

      <Button startIcon={<AddIcon />} onClick={addItem} size="small" sx={{ alignSelf: "flex-start" }}>
        {addLabel}
      </Button>
    </Stack>
  );
}
