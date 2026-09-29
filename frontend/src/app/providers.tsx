"use client";

import { useMemo, useState } from "react";
import { AppRouterCacheProvider } from "@mui/material-nextjs/v16-appRouter";
import { ThemeProvider } from "@mui/material/styles";
import CssBaseline from "@mui/material/CssBaseline";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { buildTheme } from "@/theme";
import { ThemeModeProvider, useThemeMode } from "@/lib/theme/themeModeContext";

function ThemedApp({ children }: { children: React.ReactNode }) {
  const { mode } = useThemeMode();
  const theme = useMemo(() => buildTheme(mode), [mode]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  );
}

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient());

  return (
    <AppRouterCacheProvider options={{ key: "mui" }}>
      <ThemeModeProvider>
        <ThemedApp>
          <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
        </ThemedApp>
      </ThemeModeProvider>
    </AppRouterCacheProvider>
  );
}
