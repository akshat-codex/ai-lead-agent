# Leads Agent

**Leads Agent** is an AI-powered ICP (Ideal Customer Profile) Lead Research & Qualification Platform.

Given an ICP, the eventual system will discover candidate companies, enrich them, find decision-makers,
verify identities, collect evidence, score leads, run LLM qualification with adversarial verification,
deduplicate, route through human review, and export verified leads — all governed by one core principle:

> **QUALITY > QUANTITY.** Never fabricate or guess lead information.

This repository will eventually be integrated into the company's main website/repository. **For now it is
a fully standalone project.**

## Current development stage

**Phase 0 — Project Foundation & Quality Contract.**

This stage establishes the development skeleton (a running frontend, a running backend with a health
endpoint, database/queue configuration, Docker Compose for local development, and baseline tests) plus the
canonical quality contract every later phase must be built against. No ICP, lead-research, scraping,
enrichment, scoring, or LLM-qualification logic exists yet. See
[docs/architecture.md](docs/architecture.md) for the full roadmap and architectural principles, and:

- [docs/quality-contract.md](docs/quality-contract.md) — ICP hard vs. soft rules, anti-fabrication rules,
  manager feedback, quality KPIs.
- [docs/lead-decision-policy.md](docs/lead-decision-policy.md) — lead states, transitions, and
  accept/hold/reject/duplicate rules.
- [docs/evidence-policy.md](docs/evidence-policy.md) — the provenance contract for critical fields.
- [docs/batch-contract.md](docs/batch-contract.md) — rules for batch/bulk generation runs.

## Technology stack

| Layer                 | Choice                                             |
| ---------------------- | --------------------------------------------------- |
| Frontend               | Next.js (App Router), TypeScript, MUI, React Query |
| Backend                | Python, FastAPI, Pydantic, SQLAlchemy               |
| Database               | PostgreSQL                                          |
| Background processing  | Redis, Celery                                       |
| Local development      | Docker / Docker Compose                             |
| Testing                | pytest (backend)                                    |

## Project structure

```text
Leads Agent/
├── frontend/         Next.js + TypeScript + MUI + React Query app
├── backend/          FastAPI application (app/, tests/)
├── docs/             Architecture and design documentation
├── tests/            Cross-cutting / integration test notes (see tests/README.md)
├── docker/           Reserved for shared Docker assets (currently empty)
├── .gitignore
├── docker-compose.yml
└── README.md
```

## Running locally

### Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- Docker Desktop (optional, for the containerized workflow)

### Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Visit [http://localhost:8000/health](http://localhost:8000/health) — it should return
`{"status": "ok", "service": "Leads Agent", "environment": "development"}`.

Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs).

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

Visit [http://localhost:3000](http://localhost:3000). The home page pings the backend `/health` endpoint
and shows connectivity status.

### Infrastructure (PostgreSQL, Redis)

The fastest way to get PostgreSQL and Redis running locally is Docker Compose:

```bash
docker compose up -d postgres redis
```

Or run the full stack (frontend, backend, worker, PostgreSQL, Redis) with:

```bash
docker compose up --build
```

### Background worker (Celery)

No tasks are registered yet, but the worker can be started once Redis is running:

```bash
cd backend
celery -A app.worker.celery_app worker --loglevel=info
```

## Testing

```bash
cd backend
pytest
```

Frontend has no business logic yet; `npm run build` is used as the compile/type-check smoke test.

## Current limitations

- No ICP builder, company/people discovery, enrichment, scoring, or LLM qualification — these are future
  phases (see roadmap below).
- No authentication, no production deployment configuration, no database schema beyond the SQLAlchemy
  plumbing.
- No paid data or LLM providers are connected. Everything runs locally with mocked/absent data sources
  until explicitly instructed otherwise.
- Docker Compose has been written to the Compose spec and validated for syntax, but has not been run
  end-to-end on this machine (Docker was not installed in this environment during setup).

## Roadmap

Leads Agent will be built out phase-by-phase. See [docs/architecture.md](docs/architecture.md) for the
full list and the architectural principles (standalone-first, provider-independent, quality-first, hard
ICP rules, evidence/provenance, LLM-as-reasoner-not-source-of-truth, cost control) that govern every
phase. High level:

0. Project Foundation & Quality Contract *(this stage)*
1. ICP Builder UI
2. ICP Normalization
3. Hard ICP Rule Engine
4. Manager Feedback / Learning
5. Data Provider Strategy
6. Company Discovery
7. Company Entity Resolution
8. Company Enrichment
9. Decision-Maker Discovery
10. Person Identity Resolution
11. Evidence Engine
12. Hard ICP Validation
13. Business Model Classification
14. Commercial Signal Extraction
15. Adaptive Lead Scoring
16. LLM Qualification
17. Adversarial Review
18. Multi-Source Verification
19. Deduplication
20. Lead State Machine
21. Batch Orchestrator
22. Bulk Generation
23. Human Review
24. Versioned Database
25. Feedback Learning
26. Search Strategy Optimization
27. Provider Reliability Router
28. Anti-Hallucination Guardrails
29. Confidence Model
30. Observability
31. Export / CRM
32. API
33. Security / Governance
34. Production Hardening
35. Evaluation Harness
36. Shadow Mode
37. Production MVP
38. Advanced Autonomous Researcher
39. Continuous Optimization
