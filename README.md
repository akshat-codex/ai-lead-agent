# Lead Agent

**Lead Agent** is an open-source, AI-assisted ICP (Ideal Customer Profile) lead discovery and
qualification tool. Give it an ICP — in plain language or as structured filters — and it discovers
candidate companies, validates them against hard rules, discovers and enriches decision-makers, runs
LLM-based qualification, ranks the results, and exports outreach-ready leads.

> **QUALITY > QUANTITY.** Nothing here ever fabricates or guesses a lead's information. A field that
> can't be evidenced stays unknown (`HOLD`), never silently filled in. See
> [docs/quality-contract.md](docs/quality-contract.md) and [docs/evidence-policy.md](docs/evidence-policy.md)
> for the full contract every part of the pipeline is built against.

This is a standalone project — anyone can clone it, plug in their own provider API keys, and run it
locally or self-host it. It is **bring-your-own-keys**: no provider is required to start the app, and
each one is entirely optional — the app degrades honestly (mock data, or a narrower discovery pool) when
a key is missing, rather than failing or faking results.

## What it actually does today

- **ICP intake** — describe your ideal customer in plain language or build it field-by-field; saved and
  versioned.
- **Company discovery** — three discovery modes let you choose which sources run:
  - `fast` — a structured company database only (Explorium).
  - `safe` — open web search only (Tavily + Serper), for industries a fixed taxonomy can't resolve.
  - `hard` — every configured source combined, for the widest possible pool.
- **Evidence-gated hard-rule validation** — every candidate is checked against your ICP's non-negotiable
  rules (industry, geography, employee count, company type, exclusions, allowed decision-maker titles).
  A rule only ever passes on real evidence; missing evidence holds, it never guesses.
- **Decision-maker discovery & enrichment** — finds people at qualifying companies and resolves contact
  details (email/phone/LinkedIn) through configured providers.
- **LLM qualification + adversarial review** — a real LLM judges commercial fit on candidates that pass
  the hard-rule gate, citing only real evidence (fabricated citations are rejected outright), followed by
  a second adversarial pass that can downgrade an unconvincing qualification.
- **Ranking, deduplication, and export** — a final reviewable, filterable table of leads, exportable as
  plain CSV/JSON or as a CRM-shaped bulk-import CSV (`format=HUBSPOT_CSV` / `format=SALESFORCE_CSV`) ready
  to upload directly into that CRM's own import wizard — no OAuth or live CRM connection involved.

## Providers — bring your own keys

Every real data source is optional and independently gated by its own API key. With **zero keys
configured**, the app still runs end-to-end on deterministic mock data (useful for development/testing
the pipeline itself — mock-sourced leads are visibly flagged in the UI, never silently mixed in as if
real).

| Provider | Used for | Env var(s) | Notes |
|---|---|---|---|
| **Explorium** | Company discovery (structured database) | `EXPLORIUM_API_KEY` | The only source with real structured employee-count/geography/industry data — configuring this is the single highest-leverage thing you can do for hard-rule accuracy. |
| **Tavily** | Company discovery (web search) | `TAVILY_API_KEY` | Covers industries a fixed taxonomy can't name. Free-text description evidence is bridged into industry/company_type matching (see below) since Tavily has no structured taxonomy of its own. |
| **Serper** | Company discovery (Google search) | `SERPER_API_KEY` | Same role as Tavily, an independent second web-search source — combine both for broader/cross-corroborated coverage. |
| **Hermes** | Company discovery (async research agent) | `HERMES_API_TOKEN` | A secondary, slower (multi-minute) discovery source, used only when the faster sources don't produce enough candidates. |
| **Unipile** | Decision-maker discovery + company enrichment | `UNIPILE_API_KEY`, `UNIPILE_DSN`, `UNIPILE_ACCOUNT_ID` | Requires a pre-connected LinkedIn account in your Unipile dashboard; all three values are required together. |
| **Apollo** | Person enrichment (email/phone) | `APOLLO_API_KEY` | Triggered explicitly per selected person, not automatically for everyone discovered. |
| **OpenAI** | LLM qualification + adversarial review | `OPENAI_API_KEY` | Without this, qualification runs on a deterministic mock that always returns a rubber-stamp "good fit" — real discriminating judgment requires a real key. |
| **Gemini** | Discovery-strategy term expansion / deep-mode pre-screen (optional) | `GEMINI_API_KEY` + `DISCOVERY_STRATEGY_LLM_PROVIDER=gemini` | A separate, opt-in slot — never affects qualification even if configured. |

Copy `backend/.env.example` to `backend/.env` and fill in whichever keys you have — see that file for the
full, documented list (it matches `app/core/config.py`'s settings 1:1).

### Getting the strongest results

Company discovery breaks down into two fundamentally different kinds of sources, and knowing which one
you have configured explains what results to expect:

- **Structured sources** (Explorium) return a real, queryable database record — employee count,
  resolved country, a real taxonomy category. Candidates from these sources can cleanly pass every
  hard rule.
- **Web-search sources** (Tavily, Serper, Hermes) return search results and free text, not a database
  row. A free-text evidence bridge lets a candidate's industry/company_type still pass the hard-rule gate
  when its own description literally states the required terms (e.g. "a D2C skincare brand..." against
  an ICP requiring `company_type=D2C`) — but employee count and geography have no such bridge yet, since
  free text rarely states them precisely enough to evidence confidently.

If you're only running `safe` mode (web search only) and seeing few "strong fit" results, the two
highest-leverage fixes, in order, are: **(1)** add an `EXPLORIUM_API_KEY` if you can — it's the only
source that supplies the structured evidence the hard-rule engine needs across every field, not just
industry/company_type; **(2)** add an `OPENAI_API_KEY` — without it, qualification never does real
discriminating work, it just rubber-stamps every candidate that already passed the hard-rule gate.

## Technology stack

| Layer | Choice |
|---|---|
| Frontend | Next.js (App Router), TypeScript, MUI, React Query, Framer Motion |
| Backend | Python, FastAPI, Pydantic, SQLAlchemy |
| Database | PostgreSQL |
| Background processing | Redis, Celery (scaffolded; no tasks registered yet) |
| Local development | Docker / Docker Compose |
| Testing | pytest (backend), Vitest (frontend) |

## Project structure

```text
LEADS AGENT/
├── frontend/         Next.js + TypeScript + MUI + React Query app
├── backend/          FastAPI application (app/, tests/)
├── docs/             Architecture and design documentation
├── docker/           Docker-related assets
├── .gitignore
├── docker-compose.yml
└── README.md
```

## Running locally

### Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- Docker Desktop (optional, for the containerized workflow)
- PostgreSQL 15+ (if not using Docker for it)

### Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1 — macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then fill in whichever provider keys you have — see "Providers" above
uvicorn app.main:app --reload
```

Visit [http://localhost:8000/health](http://localhost:8000/health) — it should return
`{"status": "ok", "service": "Lead Agent", "environment": "development"}`.

Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs).

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

Visit [http://localhost:3000](http://localhost:3000).

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

Scaffolded for future use — no tasks are registered yet. The entire pipeline currently runs synchronously
within the request/response cycle. Can be started once Redis is running:

```bash
cd backend
celery -A app.worker.celery_app worker --loglevel=info
```

## Testing

```bash
cd backend
pytest
```

```bash
cd frontend
npm test        # Vitest — logic-level unit tests
npx tsc --noEmit # typecheck
npm run lint
npm run build    # production build smoke test
```

## Safety defaults for a first run

- **`LIVE_TEST_MODE=true`** (the default) clamps every batch's discovery limit and target count down to
  25, regardless of what you request, so a first run with real provider keys can never accidentally spend
  more than intended. Set it to `false` in `backend/.env` once you understand your configured providers'
  real cost/yield.
- **`max_discovery_rounds_per_batch`** (default 5) is a hard, backend-enforced ceiling on how many
  discovery rounds any single batch can ever run, across its entire lifetime — independent of
  `LIVE_TEST_MODE` and never bypassable by resuming a batch repeatedly.
- Every discovery-mode/provider choice is made per-batch at creation time and never changes retroactively.

## Documentation

- [docs/architecture.md](docs/architecture.md) — end-to-end pipeline and current architecture.
- [docs/quality-contract.md](docs/quality-contract.md) — ICP hard vs. soft rules, anti-fabrication rules,
  quality KPIs.
- [docs/lead-decision-policy.md](docs/lead-decision-policy.md) — lead states, transitions, and
  accept/hold/reject/duplicate rules.
- [docs/evidence-policy.md](docs/evidence-policy.md) — the provenance contract for every field a decision
  is based on.
- [docs/batch-contract.md](docs/batch-contract.md) — rules for batch/bulk generation runs.

## Contributing

This project is open source. Issues and pull requests are welcome. A few things worth knowing before
contributing:

- The hard-rule engine (`backend/app/services/hard_rule_engine.py`) and evidence pipeline are the parts
  of this codebase held to the strictest bar: no LLM calls, no randomness, no guessed values anywhere in
  them — changes there should preserve that discipline exactly.
- Every provider integration lives behind the `ProviderAdapter`/`ProviderCapability` abstraction in
  `backend/app/providers/` — adding a new data source means writing one new adapter file and registering
  it in `backend/app/providers/default_registry.py`, gated on its own API key, without touching any other
  provider or the pipeline that consumes it.
- Run the relevant test suite (`pytest` for backend changes, `npm test`/`tsc`/`lint`/`build` for frontend
  changes) before opening a PR.
