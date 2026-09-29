"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import DarkModeOutlinedIcon from "@mui/icons-material/DarkModeOutlined";
import LightModeOutlinedIcon from "@mui/icons-material/LightModeOutlined";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Toolbar from "@mui/material/Toolbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import { motion } from "framer-motion";
import Logomark from "@/components/ui/Logomark";
import { useThemeMode } from "@/lib/theme/themeModeContext";

const NAV_LINKS = [{ href: "/icps", label: "Saved ICPs", matchPrefix: "/icps" }];

const MotionIconButton = motion.create(IconButton);

export default function NavBar() {
  const pathname = usePathname();
  const { mode, toggleMode } = useThemeMode();

  return (
    <AppBar
      position="sticky"
      color="default"
      elevation={0}
      sx={{
        top: 0,
        bgcolor: (theme) => alpha(theme.palette.background.paper, 0.72),
        backdropFilter: "blur(12px)",
        borderBottom: "1px solid",
        borderColor: "divider",
      }}
    >
      <Container maxWidth="lg" disableGutters>
        <Toolbar sx={{ gap: 1, py: 0.5, px: { xs: 2, sm: 3 } }}>
          <Stack
            component={Link}
            href="/"
            direction="row"
            spacing={1.25}
            sx={{
              alignItems: "center",
              flexGrow: 1,
              color: "inherit",
              textDecoration: "none",
              transition: "opacity 160ms ease",
              "&:hover": { opacity: 0.85 },
            }}
          >
            <Logomark />
            <Typography variant="h6" sx={{ fontWeight: 700, letterSpacing: "-0.01em" }}>
              Lead Agent
            </Typography>
          </Stack>
          <Box sx={{ display: "flex", gap: 0.5, alignItems: "center" }}>
            {NAV_LINKS.map((link) => {
              const active = pathname?.startsWith(link.matchPrefix);
              return (
                <Button
                  key={link.href}
                  component={Link}
                  href={link.href}
                  sx={{
                    color: active ? "primary.main" : "text.secondary",
                    bgcolor: active ? "action.selected" : "transparent",
                    fontWeight: 600,
                    "&:hover": { bgcolor: "action.hover", color: "text.primary" },
                  }}
                >
                  {link.label}
                </Button>
              );
            })}
            <Tooltip title={mode === "dark" ? "Switch to light mode" : "Switch to dark mode"}>
              <MotionIconButton
                onClick={toggleMode}
                aria-label="Toggle color mode"
                whileHover={{ rotate: 15, scale: 1.08 }}
                whileTap={{ scale: 0.92 }}
                sx={{ color: "text.secondary", ml: 0.5 }}
              >
                {mode === "dark" ? <LightModeOutlinedIcon fontSize="small" /> : <DarkModeOutlinedIcon fontSize="small" />}
              </MotionIconButton>
            </Tooltip>
            <Button component={Link} href="/leads/new" variant="contained" sx={{ ml: 0.5 }}>
              Find leads
            </Button>
          </Box>
        </Toolbar>
      </Container>
    </AppBar>
  );
}
