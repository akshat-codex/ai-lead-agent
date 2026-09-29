"use client";

import Link from "next/link";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";

export default function NavBar() {
  return (
    <AppBar
      position="static"
      color="default"
      elevation={0}
      sx={{ bgcolor: "background.paper", borderBottom: "1px solid", borderColor: "divider" }}
    >
      <Toolbar sx={{ gap: 1, py: 0.5 }}>
        <Typography
          variant="h6"
          component={Link}
          href="/"
          sx={{ flexGrow: 1, color: "inherit", textDecoration: "none", fontWeight: 700, letterSpacing: "-0.01em" }}
        >
          Leads Agent
        </Typography>
        <Box sx={{ display: "flex", gap: 1 }}>
          <Button component={Link} href="/icps" color="inherit">
            Saved ICPs
          </Button>
          <Button component={Link} href="/leads/new" variant="contained" disableElevation>
            Find leads
          </Button>
        </Box>
      </Toolbar>
    </AppBar>
  );
}
