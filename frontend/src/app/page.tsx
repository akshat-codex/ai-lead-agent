"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import AutoAwesomeOutlinedIcon from "@mui/icons-material/AutoAwesomeOutlined";
import BusinessOutlinedIcon from "@mui/icons-material/BusinessOutlined";
import FactCheckOutlinedIcon from "@mui/icons-material/FactCheckOutlined";
import GroupsOutlinedIcon from "@mui/icons-material/GroupsOutlined";
import MarkEmailReadOutlinedIcon from "@mui/icons-material/MarkEmailReadOutlined";
import TableChartOutlinedIcon from "@mui/icons-material/TableChartOutlined";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { alpha, useTheme } from "@mui/material/styles";
import { motion } from "framer-motion";
import type { SvgIconComponent } from "@mui/icons-material";
import { staggerContainer, staggerItem } from "@/components/ui/FadeIn";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function fetchHealth() {
  const res = await fetch(`${API_BASE_URL}/health`);
  if (!res.ok) {
    throw new Error(`Backend health check failed: ${res.status}`);
  }
  return res.json() as Promise<{ status: string; service: string; environment: string }>;
}

interface WorkflowStep {
  icon: SvgIconComponent;
  label: string;
  description: string;
}

const WORKFLOW_STEPS: WorkflowStep[] = [
  { icon: AutoAwesomeOutlinedIcon, label: "Define ICP", description: "Describe your ideal customer, in words or with structured filters." },
  { icon: BusinessOutlinedIcon, label: "Discover companies", description: "Search real company sources and rank matches against your ICP." },
  { icon: FactCheckOutlinedIcon, label: "Validate evidence", description: "Every match is checked against hard rules before it counts." },
  { icon: GroupsOutlinedIcon, label: "Find decision-makers", description: "Surface the right people at each qualified company." },
  { icon: MarkEmailReadOutlinedIcon, label: "Enrich contacts", description: "Resolve verified email, phone, and LinkedIn details." },
  { icon: TableChartOutlinedIcon, label: "Review & export", description: "Filter, review, and export outreach-ready leads." },
];

const MotionPaper = motion.create(Paper);
const MotionBox = motion.create(Box);

export default function Home() {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const { data, error, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    retry: false,
  });

  return (
    <Container maxWidth="lg">
      <Stack spacing={{ xs: 8, md: 11 }} sx={{ pt: { xs: 7, md: 11 }, pb: { xs: 6, md: 9 } }}>
        <Stack
          component={motion.div}
          initial="hidden"
          animate="show"
          variants={staggerContainer}
          spacing={3}
          sx={{ alignItems: "flex-start", maxWidth: 720 }}
        >
          <MotionBox variants={staggerItem}>
            <Chip
              icon={<AutoAwesomeOutlinedIcon sx={{ fontSize: 15 }} />}
              label="ICP-driven lead discovery"
              size="small"
              variant="outlined"
              sx={{
                borderColor: "divider",
                color: "primary.main",
                bgcolor: (t) => alpha(t.palette.primary.main, isDark ? 0.12 : 0.06),
                fontWeight: 600,
              }}
            />
          </MotionBox>

          <MotionBox variants={staggerItem}>
            <Typography
              variant="h1"
              component="h1"
              sx={{
                fontSize: { xs: "2.5rem", sm: "3.25rem", md: "3.75rem" },
                lineHeight: 1.08,
                backgroundImage: (t) =>
                  `linear-gradient(135deg, ${t.palette.text.primary} 40%, ${t.palette.primary.main} 100%)`,
                backgroundClip: "text",
                WebkitBackgroundClip: "text",
                color: "transparent",
              }}
            >
              Find your next{" "}
              <Box component="em" sx={{ fontStyle: "italic" }}>
                best
              </Box>{" "}
              customers
            </Typography>
          </MotionBox>

          <MotionBox variants={staggerItem}>
            <Typography variant="body1" color="text.secondary" sx={{ maxWidth: 560, fontSize: "1.0625rem" }}>
              Describe your ideal customer profile, discover matching companies, validate the fit with
              evidence, surface the right decision-makers, and build outreach-ready leads — all in one
              guided flow.
            </Typography>
          </MotionBox>

          <MotionBox variants={staggerItem}>
            <Stack direction="row" spacing={2} sx={{ alignItems: "center", flexWrap: "wrap", rowGap: 1.5 }}>
              <Button component={Link} href="/leads/new" variant="contained" size="large" endIcon={<ArrowForwardIcon />}>
                Start a search
              </Button>
              <Button component={Link} href="/icps" variant="outlined" size="large">
                View saved ICPs
              </Button>
            </Stack>
          </MotionBox>
        </Stack>

        <Box>
          <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 2.5 }}>
            How it works
          </Typography>
          <Box
            component={motion.div}
            initial="hidden"
            whileInView="show"
            viewport={{ once: true, margin: "-60px" }}
            variants={staggerContainer}
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", md: "repeat(3, 1fr)" },
              gap: 1.5,
            }}
          >
            {WORKFLOW_STEPS.map((step, index) => (
              <MotionPaper
                key={step.label}
                variants={staggerItem}
                whileHover={{ y: -4 }}
                variant="outlined"
                sx={{
                  p: 2.25,
                  borderRadius: 2.5,
                  bgcolor: "background.paper",
                  position: "relative",
                  overflow: "hidden",
                  transition: "border-color 200ms ease, box-shadow 200ms ease",
                  "&:hover": {
                    borderColor: "primary.main",
                    boxShadow: (t) => `0 12px 28px ${alpha(t.palette.primary.main, isDark ? 0.22 : 0.14)}`,
                  },
                }}
              >
                <Stack direction="row" spacing={1.5} sx={{ alignItems: "flex-start" }}>
                  <Box
                    sx={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      width: 36,
                      height: 36,
                      borderRadius: 2,
                      bgcolor: (t) => alpha(t.palette.primary.main, isDark ? 0.16 : 0.08),
                      color: "primary.main",
                      flexShrink: 0,
                    }}
                  >
                    <step.icon fontSize="small" />
                  </Box>
                  <Box sx={{ minWidth: 0 }}>
                    <Stack direction="row" spacing={0.75} sx={{ alignItems: "center" }}>
                      <Typography variant="caption" color="primary.main" sx={{ fontWeight: 700 }}>
                        {index + 1}
                      </Typography>
                      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                        {step.label}
                      </Typography>
                    </Stack>
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>
                      {step.description}
                    </Typography>
                  </Box>
                </Stack>
              </MotionPaper>
            ))}
          </Box>
        </Box>

        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <Typography variant="caption" color="text.secondary">
            Backend status:
          </Typography>
          {isLoading && <Chip label="checking..." size="small" />}
          {error && <Chip label="unreachable" color="error" size="small" />}
          {data && (
            <Chip
              label={`${data.status} (${data.environment})`}
              color="success"
              size="small"
            />
          )}
        </Stack>
      </Stack>
    </Container>
  );
}
