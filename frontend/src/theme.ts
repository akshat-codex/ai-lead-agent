import { alpha, createTheme } from "@mui/material/styles";
import type { PaletteMode } from "@mui/material";

// Centralized design tokens — every color/radius/shadow decision that used
// to be repeated ad hoc across component `sx` props should be expressed
// here instead, so restyling one thing restyles it everywhere. Nothing in
// this file changes component BEHAVIOR — it only changes how existing
// components look.
//
// Two modes: "light" (the original palette) and "dark" — a GitHub-Dimmed /
// Linear-dark-inspired near-black surface palette with the same blue accent,
// built via buildTheme(mode) so every design decision (shadows, component
// overrides, typography) is shared between both and only the palette itself
// diverges.

const RADIUS = { sm: 8, md: 10, lg: 16 };

interface ModePalette {
  primaryMain: string;
  primaryDark: string;
  primaryLight: string;
  successMain: string;
  successDark: string;
  warningMain: string;
  warningDark: string;
  errorMain: string;
  errorDark: string;
  bgDefault: string;
  bgPaper: string;
  bgElevated: string;
  textPrimary: string;
  textSecondary: string;
  divider: string;
  dividerStrong: string;
  glow: string;
}

// Brand accent is a bold crimson red (agency/editorial reference:
// black-and-red, serif-display palette) — deliberately a different hue
// lean from the semantic `error` red below (crimson/pink-leaning primary
// vs. orange-leaning error) so a destructive/failure state never reads as
// "just another brand-colored button."
const LIGHT: ModePalette = {
  primaryMain: "#d61f3c",
  primaryDark: "#a8112a",
  primaryLight: "#ef4a63",
  successMain: "#1e8e5a",
  successDark: "#166a44",
  warningMain: "#b98900",
  warningDark: "#8a6600",
  errorMain: "#c2410c",
  errorDark: "#9a3412",
  bgDefault: "#f7f5f4",
  bgPaper: "#ffffff",
  bgElevated: "#ffffff",
  textPrimary: "#161316",
  textSecondary: "#615a5c",
  divider: "rgba(22, 19, 22, 0.09)",
  dividerStrong: "rgba(22, 19, 22, 0.18)",
  glow: "rgba(214, 31, 60, 0.16)",
};

// Near-black (not pure #000 — a touch of warmth so it doesn't read as flat
// void) surfaces with a hot, saturated crimson accent that pops hard
// against black, matching the black/red agency-editorial reference.
const DARK: ModePalette = {
  primaryMain: "#ff3355",
  primaryDark: "#d61f3c",
  primaryLight: "#ff6b82",
  successMain: "#3fb97a",
  successDark: "#2f9c65",
  warningMain: "#e0a72e",
  warningDark: "#c98f1a",
  errorMain: "#ff7a45",
  errorDark: "#e0602c",
  bgDefault: "#0c0a0b",
  bgPaper: "#121012",
  bgElevated: "#1a1618",
  textPrimary: "#f5f1f2",
  textSecondary: "#a39a9d",
  divider: "rgba(245, 241, 242, 0.09)",
  dividerStrong: "rgba(245, 241, 242, 0.18)",
  glow: "rgba(255, 51, 85, 0.28)",
};

export function buildTheme(mode: PaletteMode) {
  const p = mode === "dark" ? DARK : LIGHT;

  const shadowColor = mode === "dark" ? "0, 0, 0" : "15, 23, 42";
  const shadowStrength = mode === "dark" ? [0.4, 0.45, 0.5, 0.55] : [0.06, 0.08, 0.1, 0.12];

  return createTheme({
    palette: {
      mode,
      primary: { main: p.primaryMain, dark: p.primaryDark, light: p.primaryLight },
      success: { main: p.successMain, dark: p.successDark },
      warning: { main: p.warningMain, dark: p.warningDark },
      error: { main: p.errorMain, dark: p.errorDark },
      background: { default: p.bgDefault, paper: p.bgPaper },
      text: { primary: p.textPrimary, secondary: p.textSecondary },
      divider: p.divider,
    },
    shape: {
      borderRadius: RADIUS.md,
    },
    typography: {
      // One typeface for the whole product — Space Grotesk's geometric,
      // technical character IS the brand identity here, not just the
      // headline treatment. Weight/spacing still escalate with size
      // (tighter tracking + heavier weight on the biggest headlines) so
      // there's still a clear hierarchy, just expressed within one family
      // instead of a serif/sans split.
      fontFamily: 'var(--font-space-grotesk), "Roboto", "Helvetica", "Arial", sans-serif',
      h1: { fontWeight: 700, letterSpacing: "-0.03em" },
      h2: { fontWeight: 700, letterSpacing: "-0.03em" },
      h3: { fontWeight: 700, letterSpacing: "-0.025em" },
      h4: { fontWeight: 700, letterSpacing: "-0.02em" },
      h5: { fontWeight: 700, letterSpacing: "-0.015em" },
      h6: { fontWeight: 700, letterSpacing: "-0.01em" },
      subtitle1: { fontWeight: 600 },
      overline: { fontWeight: 700, letterSpacing: "0.08em", fontSize: "0.6875rem" },
      button: { fontWeight: 600, textTransform: "none", letterSpacing: "0.01em" },
    },
    shadows: [
      "none",
      `0 1px 2px rgba(${shadowColor}, ${shadowStrength[0]})`,
      `0 2px 8px rgba(${shadowColor}, ${shadowStrength[1]})`,
      `0 4px 14px rgba(${shadowColor}, ${shadowStrength[2]})`,
      `0 6px 20px rgba(${shadowColor}, ${shadowStrength[3]})`,
      ...Array(20).fill(`0 6px 20px rgba(${shadowColor}, ${shadowStrength[3]})`),
    ] as unknown as import("@mui/material/styles").Theme["shadows"],
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          // A visible, on-brand focus ring for keyboard navigation — MUI's
          // default browser outline is inconsistent across elements; this
          // makes focus state a deliberate, theme-driven decision instead.
          "*:focus-visible": {
            outline: `2px solid ${alpha(p.primaryMain, 0.5)}`,
            outlineOffset: "2px",
          },
          body: {
            colorScheme: mode,
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          root: {
            backgroundImage: "none",
          },
          outlined: {
            borderColor: p.divider,
          },
        },
      },
      MuiButton: {
        defaultProps: {
          disableElevation: true,
        },
        styleOverrides: {
          root: {
            borderRadius: RADIUS.sm,
            transition: "transform 160ms ease, box-shadow 160ms ease, background-color 160ms ease, border-color 160ms ease",
          },
          contained: {
            "&:hover": {
              transform: "translateY(-1px)",
              boxShadow: `0 8px 20px ${alpha(p.primaryMain, mode === "dark" ? 0.35 : 0.25)}`,
            },
            "&:active": { transform: "translateY(0)" },
          },
          outlined: {
            "&:hover": {
              transform: "translateY(-1px)",
              borderColor: p.primaryMain,
              backgroundColor: alpha(p.primaryMain, mode === "dark" ? 0.12 : 0.06),
            },
          },
        },
      },
      MuiChip: {
        styleOverrides: {
          root: {
            fontWeight: 600,
          },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: RADIUS.lg - 4,
            border: `1px solid ${p.divider}`,
          },
        },
      },
      MuiAppBar: {
        styleOverrides: {
          root: {
            backgroundImage: "none",
          },
        },
      },
      MuiTextField: {
        defaultProps: {
          size: "small",
        },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            borderRadius: RADIUS.sm,
            transition: "border-color 160ms ease, box-shadow 160ms ease",
            "&:hover .MuiOutlinedInput-notchedOutline": {
              borderColor: alpha(p.primaryMain, 0.5),
            },
          },
        },
      },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            borderRadius: 6,
            fontSize: "0.75rem",
            fontWeight: 500,
            backgroundColor: p.bgElevated,
            color: p.textPrimary,
            border: `1px solid ${p.divider}`,
          },
        },
      },
      MuiAlert: {
        styleOverrides: {
          root: {
            borderRadius: RADIUS.md,
          },
        },
      },
      MuiDialog: {
        styleOverrides: {
          paper: {
            borderRadius: RADIUS.lg,
            border: `1px solid ${p.divider}`,
            backgroundImage: "none",
          },
        },
      },
      MuiTableCell: {
        styleOverrides: {
          root: {
            borderColor: p.divider,
          },
          head: {
            fontWeight: 700,
            color: p.textSecondary,
            fontSize: "0.75rem",
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            backgroundColor: p.bgPaper,
          },
        },
      },
      MuiDivider: {
        styleOverrides: {
          root: {
            borderColor: p.divider,
          },
        },
      },
      MuiCheckbox: {
        styleOverrides: {
          root: {
            transition: "transform 120ms ease",
            "&:hover": { transform: "scale(1.08)" },
          },
        },
      },
    },
  });
}

const theme = buildTheme("dark");

// Exported so components can reference the same "strong divider" tint
// instead of re-hardcoding an rgba value independently — mode-aware via
// the theme's own text color rather than a fixed light-mode literal.
export function borderColorStrong(mode: PaletteMode): string {
  return mode === "dark" ? DARK.dividerStrong : LIGHT.dividerStrong;
}

export function glowColor(mode: PaletteMode): string {
  return mode === "dark" ? DARK.glow : LIGHT.glow;
}

export default theme;
