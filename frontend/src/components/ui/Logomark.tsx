"use client";

import Box from "@mui/material/Box";
import { useTheme } from "@mui/material/styles";

interface LogomarkProps {
  size?: number;
}

/**
 * A simple, original geometric mark (three ascending bars converging into
 * a target/crosshair-style node) — evokes "finding/targeting leads"
 * without copying any third-party logo. Pure inline SVG, no new asset
 * files or dependencies, colored entirely from the theme's primary
 * palette so it stays in sync with any future palette change.
 */
export default function Logomark({ size = 28 }: LogomarkProps) {
  const theme = useTheme();
  const gradientId = "lead-agent-logomark-gradient";
  return (
    <Box
      component="svg"
      viewBox="0 0 28 28"
      width={size}
      height={size}
      sx={{ display: "block", flexShrink: 0 }}
      aria-hidden
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor={theme.palette.primary.light} />
          <stop offset="100%" stopColor={theme.palette.primary.dark} />
        </linearGradient>
      </defs>
      <rect width="28" height="28" rx="7" fill={`url(#${gradientId})`} />
      <path
        d="M8 18.5V13.5"
        stroke="white"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <path
        d="M14 18.5V9.5"
        stroke="white"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <path
        d="M20 18.5V6.5"
        stroke="white"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <circle cx="20" cy="6.5" r="2" fill="white" />
    </Box>
  );
}
