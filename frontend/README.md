# Leads Agent — Frontend

Next.js (App Router) + TypeScript + MUI + React Query. See the [root README](../README.md) for full project context.

## Run locally

```bash
npm install
cp .env.example .env.local
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The home page pings the backend `/health` endpoint (configured via `NEXT_PUBLIC_API_BASE_URL`) to show connectivity status.

## Build

```bash
npm run build
```
