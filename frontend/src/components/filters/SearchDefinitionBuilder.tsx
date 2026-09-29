"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import TuneIcon from "@mui/icons-material/Tune";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AddFilterPopover from "./AddFilterPopover";
import ExtractedFiltersReview from "./ExtractedFiltersReview";
import FilterCriterionChip from "./FilterCriterionChip";
import NlDescriptionInput from "./NlDescriptionInput";
import SearchCriteriaSummary from "./SearchCriteriaSummary";
import SearchPresetPicker from "./SearchPresetPicker";
import ErrorState, { errorMessage } from "@/components/ui/ErrorState";
import LoadingState from "@/components/ui/LoadingState";
import { createIcpFromCriteria, getFilterCatalog } from "@/lib/filters/api";
import { extractFiltersFromDescription } from "@/lib/filters/nlExtraction";
import type { ExtractionResult } from "@/lib/filters/nlExtraction";
import { presetToCriteria } from "@/lib/filters/presets";
import type { SearchPreset } from "@/lib/filters/presets";
import { CUSTOM_FILTER_KEY, newClientId } from "@/lib/filters/types";
import type { ExtractedFilter, FilterCriterion, FilterDefinition, FilterValue } from "@/lib/filters/types";

interface SearchDefinitionBuilderProps {
  onConfirmed: (icpId: string) => void;
}

type BuilderMode = "ai" | "manual";

export default function SearchDefinitionBuilder({ onConfirmed }: SearchDefinitionBuilderProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [criteria, setCriteria] = useState<FilterCriterion[]>([]);
  const [pending, setPending] = useState<ExtractionResult | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const [mode, setMode] = useState<BuilderMode>("ai");

  const catalogQuery = useQuery({ queryKey: ["filter-catalog"], queryFn: getFilterCatalog });
  const catalog = useMemo(() => catalogQuery.data?.definitions ?? [], [catalogQuery.data]);
  const catalogByKey = useMemo(() => new Map<string, FilterDefinition>(catalog.map((d) => [d.key, d])), [catalog]);

  const usedKeys = useMemo(() => new Set(criteria.map((c) => c.key)), [criteria]);

  const mutation = useMutation({
    mutationFn: () => createIcpFromCriteria(criteria, name),
    onSuccess: (saved) => onConfirmed(saved.id),
  });

  const handleExtract = () => {
    setPending(extractFiltersFromDescription(description, catalog));
  };

  const acceptExtracted = (filters: ExtractedFilter[]) => {
    setCriteria((prev) => [...prev, ...filters]);
  };

  const handleAcceptAll = () => {
    if (pending) acceptExtracted(pending.filters);
    setPending(null);
  };

  const handleDismissOne = (id: string) => {
    setPending((prev) => (prev ? { ...prev, filters: prev.filters.filter((f) => f.id !== id) } : prev));
  };

  const handleDismissAll = () => setPending(null);

  const handleSelectFromCatalog = (definition: FilterDefinition) => {
    const defaultValue: FilterValue = definition.valueType === "multi-select"
      ? []
      : definition.valueType === "number-range"
        ? { min: null, max: null }
        : definition.valueType === "boolean"
          ? false
          : "";
    setCriteria((prev) => [
      ...prev,
      {
        id: newClientId(),
        key: definition.key,
        operator: definition.operators?.[0] ?? "equals",
        value: defaultValue,
        source: "manual",
      },
    ]);
  };

  const handleAddCustom = (label: string) => {
    setCriteria((prev) => [
      ...prev,
      {
        id: newClientId(),
        key: CUSTOM_FILTER_KEY,
        operator: "contains",
        value: "",
        label,
        source: "manual",
      },
    ]);
  };

  const handleCriterionChange = (id: string, value: FilterValue) => {
    setCriteria((prev) => prev.map((c) => (c.id === id ? { ...c, value } : c)));
  };

  const handleRemoveCriterion = (id: string) => {
    setCriteria((prev) => prev.filter((c) => c.id !== id));
  };

  const handleApplyPreset = (preset: SearchPreset) => {
    const existingKeys = new Set(criteria.map((c) => c.key));
    const additions = presetToCriteria(preset).filter((c) => !existingKeys.has(c.key));
    setCriteria((prev) => [...prev, ...additions]);
  };

  const nameError = showErrors && !name.trim() ? "Give this search a name" : undefined;
  const criteriaError = showErrors && criteria.length === 0 ? "Add at least one filter" : undefined;

  const handleContinue = () => {
    setShowErrors(true);
    if (!name.trim() || criteria.length === 0) return;
    mutation.mutate();
  };

  return (
    <Grid container spacing={3}>
      <Grid size={{ xs: 12, md: 7 }}>
        <Stack spacing={3}>
          <TextField
            label="Search name"
            placeholder="e.g. Q3 D2C outreach"
            value={name}
            onChange={(e) => setName(e.target.value)}
            error={Boolean(nameError)}
            helperText={nameError}
          />

          <Paper variant="outlined" sx={{ p: 0, borderRadius: 2.5, overflow: "hidden" }}>
            <Tabs
              value={mode}
              onChange={(_e, value: BuilderMode) => setMode(value)}
              variant="fullWidth"
              sx={{ borderBottom: "1px solid", borderColor: "divider", bgcolor: "background.default" }}
            >
              {/* Phase 4 (AI/UX audit) fix: this tab runs
                  extractFiltersFromDescription — a deterministic,
                  client-side regex extractor (see nlExtraction.ts's own
                  header comment), never an LLM call. "AI Prompt" overclaimed
                  what the mechanism actually is; the extraction itself was
                  always honest (mandatory review, "guessed from your
                  description" disclaimer, unmatched fragments shown) — only
                  the label was misleading. */}
              <Tab value="ai" icon={<AutoAwesomeIcon fontSize="small" />} iconPosition="start" label="Describe in words" />
              <Tab value="manual" icon={<TuneIcon fontSize="small" />} iconPosition="start" label="Manual Builder" />
            </Tabs>

            <Box sx={{ p: 2.5 }}>
              {mode === "ai" && (
                <Stack spacing={2.5}>
                  <NlDescriptionInput
                    value={description}
                    onChange={setDescription}
                    onExtract={handleExtract}
                    disabled={catalogQuery.isLoading}
                  />

                  {pending && (
                    <ExtractedFiltersReview
                      filters={pending.filters}
                      unmatched={pending.unmatched}
                      catalogByKey={catalogByKey}
                      onDismiss={handleDismissOne}
                      onAcceptAll={handleAcceptAll}
                      onDismissAll={handleDismissAll}
                    />
                  )}
                </Stack>
              )}

              {mode === "manual" && (
                <Stack spacing={2.5}>
                  <SearchPresetPicker onApply={handleApplyPreset} />
                  <Stack spacing={1}>
                    <Typography variant="caption" color="text.secondary">
                      Add a field
                    </Typography>
                    <Box>
                      <AddFilterPopover
                        catalog={catalog}
                        usedKeys={usedKeys}
                        onSelect={handleSelectFromCatalog}
                        onAddCustom={handleAddCustom}
                      />
                    </Box>
                  </Stack>
                </Stack>
              )}
            </Box>
          </Paper>

          {catalogQuery.data?.isFallback && (
            <Alert severity="info">
              Showing default filters — connect the filter catalog for the full set.
            </Alert>
          )}
          {catalogQuery.isLoading && <LoadingState label="Loading available filters..." />}
          {catalogQuery.error && (
            <ErrorState
              message={`Could not load the filter catalog. ${errorMessage(catalogQuery.error)}`}
              onRetry={() => catalogQuery.refetch()}
            />
          )}

          <Divider />

          <Stack spacing={1.5}>
            <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
              Search criteria
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Everything here is editable, whether it came from the AI prompt or was added manually.
            </Typography>
            {criteriaError && (
              <Typography variant="caption" color="error">
                {criteriaError}
              </Typography>
            )}
            {criteria.length > 0 ? (
              <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1, alignItems: "center" }}>
                {criteria.map((c) => (
                  <FilterCriterionChip
                    key={c.id}
                    criterion={c}
                    definition={catalogByKey.get(c.key)}
                    onChange={(value) => handleCriterionChange(c.id, value)}
                    onRemove={() => handleRemoveCriterion(c.id)}
                  />
                ))}
              </Stack>
            ) : (
              <Typography variant="body2" color="text.secondary" sx={{ fontStyle: "italic" }}>
                No criteria yet — use the AI prompt or Manual Builder above to add some.
              </Typography>
            )}
          </Stack>

          {mutation.isError && (
            <ErrorState message={`Could not save this search. ${errorMessage(mutation.error)}`} onRetry={() => mutation.mutate()} />
          )}

          <Stack direction="row" sx={{ justifyContent: "flex-end" }}>
            <Button variant="contained" size="large" onClick={handleContinue} disabled={mutation.isPending}>
              {mutation.isPending ? "Saving..." : "Find companies →"}
            </Button>
          </Stack>
        </Stack>
      </Grid>

      <Grid size={{ xs: 12, md: 5 }}>
        <SearchCriteriaSummary name={name} criteria={criteria} catalogByKey={catalogByKey} />
      </Grid>
    </Grid>
  );
}
