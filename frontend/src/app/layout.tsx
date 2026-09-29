import type { Metadata } from "next";
import { Fraunces, Inter } from "next/font/google";
import Box from "@mui/material/Box";
import NavBar from "@/components/NavBar";
import AnimatedBackground from "@/components/ui/AnimatedBackground";
import Providers from "./providers";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
// A display serif for headlines only (landing hero, section titles) —
// Fraunces has a genuine, expressive italic optical style close to the
// black/red editorial reference; body copy, buttons, and forms stay on
// Inter for readability. Loaded via next/font/google — no new dependency,
// self-hosted/optimized by Next.js like Inter already is.
const fraunces = Fraunces({
  subsets: ["latin"],
  variable: "--font-fraunces",
  display: "swap",
  style: ["normal", "italic"],
  axes: ["opsz", "SOFT", "WONK"],
});

export const metadata: Metadata = {
  title: "Leads Agent",
  description: "AI-powered ICP Lead Research & Qualification Platform.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${fraunces.variable}`} suppressHydrationWarning>
      <body>
        <Providers>
          <AnimatedBackground />
          <Box sx={{ position: "relative", zIndex: 1, display: "flex", flexDirection: "column", minHeight: "100%" }}>
            <NavBar />
            {children}
          </Box>
        </Providers>
      </body>
    </html>
  );
}
