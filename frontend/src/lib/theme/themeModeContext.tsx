"use client";

import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import type { PaletteMode } from "@mui/material";

const STORAGE_KEY = "lead-agent-theme-mode";

interface ThemeModeContextValue {
  mode: PaletteMode;
  toggleMode: () => void;
}

const ThemeModeContext = createContext<ThemeModeContextValue | null>(null);

function readStoredMode(): PaletteMode {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : "dark";
  } catch {
    return "dark";
  }
}

// useSyncExternalStore (not useState+useEffect) is the correct way to read
// an external, mutable source like localStorage — it reads synchronously
// on the client (no setState-in-effect cascading-render lint violation)
// while still returning a fixed "dark" snapshot during SSR, so the server
// markup and the client's first render agree (the real localStorage value,
// if it differs, is picked up on the client's very first paint via
// getSnapshot, not a later effect).
function subscribe(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  return () => window.removeEventListener("storage", callback);
}

function getServerSnapshot(): PaletteMode {
  return "dark";
}

/**
 * Dark is the default (matches the product's own visual direction) —
 * light remains fully available via the toggle, persisted per-browser in
 * localStorage.
 */
export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const mode = useSyncExternalStore(subscribe, readStoredMode, getServerSnapshot);

  const toggleMode = useCallback(() => {
    const next: PaletteMode = mode === "dark" ? "light" : "dark";
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
      // localStorage's own "storage" event only fires in OTHER tabs, never
      // the tab that made the change — dispatch a synthetic one so this
      // tab's useSyncExternalStore subscription re-reads immediately too.
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY, newValue: next }));
    } catch {
      // Non-fatal — persistence failing (private browsing, etc.) just means the choice won't survive a reload.
    }
  }, [mode]);

  const value = useMemo(() => ({ mode, toggleMode }), [mode, toggleMode]);

  return <ThemeModeContext.Provider value={value}>{children}</ThemeModeContext.Provider>;
}

export function useThemeMode(): ThemeModeContextValue {
  const ctx = useContext(ThemeModeContext);
  if (!ctx) throw new Error("useThemeMode must be used within a ThemeModeProvider");
  return ctx;
}
