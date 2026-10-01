import type { Metadata } from "next";
import { Space_Grotesk } from "next/font/google";
import Box from "@mui/material/Box";
import NavBar from "@/components/NavBar";
import AnimatedBackground from "@/components/ui/AnimatedBackground";
import Providers from "./providers";

// A single, cohesive typeface across the whole product — Space Grotesk's
// geometric, slightly technical character is the "futuristic B2B SaaS"
// identity for Lead Agent as a standalone product: headlines AND body/UI
// text both use it (previously a serif display + Inter body split), so
// the entire app reads as one distinct brand rather than a generic
// system-sans product. Loaded via next/font/google — no new dependency,
// self-hosted/optimized by Next.js.
const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-space-grotesk",
  display: "swap",
  weight: ["400", "500", "600", "700"],
});

export const metadata: Metadata = {
  title: "Lead Agent",
  description: "AI-powered ICP Lead Research & Qualification Platform.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={spaceGrotesk.variable} suppressHydrationWarning>
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
