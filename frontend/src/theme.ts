import { createTheme } from "@mui/material/styles";

const theme = createTheme({
  palette: {
    mode: "light",
    primary: {
      main: "#2f6feb",
      dark: "#1f52c2",
    },
    // "Strong fit" / "Good fit" badges reuse MUI's existing semantic slots so
    // every component (Chip, Alert, ...) already knows how to consume them —
    // no custom strongFit/goodFit palette keys needed for a 3-tier badge.
    success: {
      main: "#1e8e5a",
    },
    warning: {
      main: "#b98900",
    },
    // A soft off-white page background (rather than pure white) so outlined
    // Paper cards throughout the wizard read as distinct surfaces.
    background: {
      default: "#f6f7fb",
      paper: "#ffffff",
    },
    text: {
      primary: "#161b26",
      secondary: "#5d6472",
    },
    divider: "rgba(15, 23, 42, 0.08)",
  },
  shape: {
    borderRadius: 10,
  },
  typography: {
    fontFamily: 'var(--font-inter), "Roboto", "Helvetica", "Arial", sans-serif',
    h4: { fontWeight: 700, letterSpacing: "-0.01em" },
    h6: { fontWeight: 700 },
    subtitle1: { fontWeight: 600 },
    button: { fontWeight: 600, textTransform: "none" },
  },
  shadows: [
    "none",
    "0 1px 2px rgba(15, 23, 42, 0.06)",
    "0 2px 8px rgba(15, 23, 42, 0.08)",
    "0 4px 14px rgba(15, 23, 42, 0.10)",
    "0 6px 20px rgba(15, 23, 42, 0.12)",
    ...Array(20).fill("0 6px 20px rgba(15, 23, 42, 0.12)"),
  ] as unknown as import("@mui/material/styles").Theme["shadows"],
  components: {
    MuiPaper: {
      styleOverrides: {
        outlined: {
          borderColor: "rgba(15, 23, 42, 0.08)",
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          borderRadius: 8,
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
          borderRadius: 12,
        },
      },
    },
  },
});

export default theme;
