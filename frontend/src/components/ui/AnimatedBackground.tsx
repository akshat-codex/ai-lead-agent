"use client";

import dynamic from "next/dynamic";
import AutoAwesomeOutlinedIcon from "@mui/icons-material/AutoAwesomeOutlined";
import BusinessOutlinedIcon from "@mui/icons-material/BusinessOutlined";
import HubOutlinedIcon from "@mui/icons-material/HubOutlined";
import InsightsOutlinedIcon from "@mui/icons-material/InsightsOutlined";
import PersonOutlineOutlinedIcon from "@mui/icons-material/PersonOutlineOutlined";
import SmartToyOutlinedIcon from "@mui/icons-material/SmartToyOutlined";
import Box from "@mui/material/Box";
import { alpha, useTheme } from "@mui/material/styles";
import { motion } from "framer-motion";
import type { SvgIconComponent } from "@mui/icons-material";

// WebGL/<Canvas> cannot render during SSR — loaded client-only, after
// mount, via next/dynamic. This is the ACTUAL 3D layer (a rotating,
// pointer-reactive network-of-nodes scene); everything else in this file
// (noise texture, glow blobs, floating 2D icons) layers on top of it for
// depth and warmth.
const Scene3D = dynamic(() => import("./Scene3D"), { ssr: false });

interface FloatingIcon {
  Icon: SvgIconComponent;
  top: string;
  left: string;
  size: number;
  duration: number;
  delay: number;
  driftX: number;
  driftY: number;
  rotate: number;
}

// Positioned loosely around the viewport edges (never dead-center, so they
// never sit behind primary content on any screen) — a mix of the product's
// own domain icons (person = decision-maker, business = company, hub =
// network/discovery, smart-toy = the "agent" in Lead Agent, insights =
// scoring/ranking, auto-awesome = AI qualification) drifting slowly and
// rotating a few degrees, never fast or distracting.
const FLOATING_ICONS: FloatingIcon[] = [
  { Icon: PersonOutlineOutlinedIcon, top: "12%", left: "6%", size: 46, duration: 22, delay: 0, driftX: 22, driftY: 30, rotate: 8 },
  { Icon: BusinessOutlinedIcon, top: "68%", left: "9%", size: 40, duration: 26, delay: 1.5, driftX: -18, driftY: -26, rotate: -6 },
  { Icon: SmartToyOutlinedIcon, top: "20%", left: "88%", size: 50, duration: 24, delay: 0.5, driftX: -26, driftY: 24, rotate: 10 },
  { Icon: HubOutlinedIcon, top: "78%", left: "85%", size: 38, duration: 28, delay: 2, driftX: 20, driftY: -22, rotate: -8 },
  { Icon: InsightsOutlinedIcon, top: "45%", left: "94%", size: 34, duration: 20, delay: 1, driftX: -16, driftY: 20, rotate: 6 },
  { Icon: AutoAwesomeOutlinedIcon, top: "88%", left: "45%", size: 30, duration: 18, delay: 2.5, driftX: 18, driftY: -16, rotate: -10 },
  { Icon: PersonOutlineOutlinedIcon, top: "8%", left: "42%", size: 30, duration: 25, delay: 3, driftX: -14, driftY: 18, rotate: 7 },
];

// An inline SVG feTurbulence filter, base64-encoded as a data: URI — a
// subtle film-grain/noise texture (the black/red editorial reference's
// gritty backdrop) with zero network requests and no binary asset file.
// tileable via a fixed small viewBox repeated across the viewport.
const NOISE_SVG = `<svg xmlns='http://www.w3.org/2000/svg' width='140' height='140'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>`;
const NOISE_DATA_URI = `url("data:image/svg+xml;utf8,${NOISE_SVG}")`;

/**
 * A fixed, full-viewport backdrop layer: a subtle film-grain noise texture
 * (the black/red editorial reference's gritty backdrop), two slow-drifting
 * glow blobs in the brand accent, and a set of outline icons (person/
 * company/agent/network/insight) that slowly float, drift, and rotate —
 * the "leads/AI" visual motif the rest of the product already uses
 * (landing page workflow icons, FitBadge, etc.), rendered as ambient
 * background motion rather than static decoration.
 *
 * Pointer-events: none and z-index: -1 throughout so none of this ever
 * intercepts clicks or sits above real content. Rendered once, at the root
 * layout, behind every page.
 */
export default function AnimatedBackground() {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const glow = alpha(theme.palette.primary.main, isDark ? 0.3 : 0.16);
  const glowSecondary = alpha(theme.palette.primary.light, isDark ? 0.14 : 0.08);
  const iconColor = alpha(theme.palette.primary.main, isDark ? 0.18 : 0.12);

  return (
    <Box
      aria-hidden
      sx={{
        position: "fixed",
        inset: 0,
        zIndex: 0,
        overflow: "hidden",
        pointerEvents: "none",
        bgcolor: "background.default",
      }}
    >
      <Box
        sx={{
          position: "absolute",
          inset: 0,
          backgroundImage: NOISE_DATA_URI,
          backgroundRepeat: "repeat",
          opacity: isDark ? 0.05 : 0.035,
          mixBlendMode: isDark ? "screen" : "multiply",
        }}
      />

      <Box sx={{ position: "absolute", inset: 0 }}>
        <Scene3D accentColor={theme.palette.primary.main} secondaryColor={theme.palette.primary.light} />
      </Box>

      <Box
        component={motion.div}
        animate={{ x: [0, 60, -30, 0], y: [0, -45, 30, 0] }}
        transition={{ duration: 26, repeat: Infinity, ease: "easeInOut" }}
        sx={{
          position: "absolute",
          top: "-10%",
          left: "-5%",
          width: { xs: 400, md: 700 },
          height: { xs: 400, md: 700 },
          borderRadius: "50%",
          background: `radial-gradient(circle, ${glow} 0%, transparent 70%)`,
          filter: "blur(20px)",
        }}
      />
      <Box
        component={motion.div}
        animate={{ x: [0, -45, 35, 0], y: [0, 35, -22, 0] }}
        transition={{ duration: 32, repeat: Infinity, ease: "easeInOut" }}
        sx={{
          position: "absolute",
          bottom: "-15%",
          right: "-8%",
          width: { xs: 350, md: 620 },
          height: { xs: 350, md: 620 },
          borderRadius: "50%",
          background: `radial-gradient(circle, ${glowSecondary} 0%, transparent 70%)`,
          filter: "blur(20px)",
        }}
      />

      {FLOATING_ICONS.map(({ Icon, top, left, size, duration, delay, driftX, driftY, rotate }, index) => (
        <Box
          key={index}
          component={motion.div}
          animate={{
            x: [0, driftX, -driftX * 0.6, 0],
            y: [0, driftY, -driftY * 0.6, 0],
            rotate: [0, rotate, -rotate * 0.5, 0],
          }}
          transition={{ duration, delay, repeat: Infinity, ease: "easeInOut" }}
          sx={{
            position: "absolute",
            top,
            left,
            color: iconColor,
            display: { xs: index > 3 ? "none" : "block", md: "block" },
          }}
        >
          <Icon sx={{ fontSize: size }} />
        </Box>
      ))}

      {/* A soft vignette so the glow/icons fade toward the edges rather than
          reading as hard-edged shapes near the viewport boundary. */}
      <Box
        sx={{
          position: "absolute",
          inset: 0,
          background: (t) =>
            `radial-gradient(ellipse at 50% 0%, transparent 0%, ${t.palette.background.default} 78%)`,
        }}
      />
    </Box>
  );
}
