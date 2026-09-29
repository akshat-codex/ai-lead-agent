# Architecture

## Purpose

Leads Agent turns an Ideal Customer Profile (ICP) into a list of **verified, evidence-backed** leads,
never fabricated or guessed ones. This document describes the high-level architecture and the principles
that must hold across every phase of the roadmap, even though only the foundation (Phase 0) exists today.

## End-to-end pipeline (target state)

```text
ICP
 → normalize ICP
 → separate hard rules from soft preferences
 → discover candidate companies
 → enrich companies
 → discover decision-makers
 → verify identities
 → collect evidence
 → apply hard ICP filters
 → understand business model
 → detect commercial/growth signals
 → score leads
 → LLM qualification
 → adversarial verification
 → multi-source verification
 → deduplication
 → human review
 → accept / hold / reject / duplicate
 → batch/bulk generation
 → manager feedback
 → learning/optimization
 → export verified leads
```

Each stage is a distinct, independently testable unit. None of these stages are implemented yet — this
document exists so future phases build on a shared understanding of where they fit.

## Current architecture (Phase 0)

```text
┌─────────────┐        HTTP        ┌──────────────┐
│  frontend    │ ──────────────────▶│   backend     │
│  Next.js     │◀────────────────── │   FastAPI     │
│  MUI + RQ    │      /health        │              │
└─────────────┘                     └──────┬───────┘
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                                   ▼
                  ┌───────────────┐                  ┌────────────────┐
                  │  PostgreSQL    │                  │     Redis       │
                  │  (SQLAlchemy)  │                  │ (Celery broker) │
                  └───────────────┘                  └────────┬────────┘
                                                                │
                                                        ┌───────▼────────┐
                                                        │  Celery worker  │
                                                        │  (no tasks yet) │
                                                        └────────────────┘
```

The backend exposes `GET /health` only. The database has connection plumbing (`app/db/session.py`) but no
domain schema — ICP, Company, Person, and Lead models belong to later phases. The Celery app
(`app/worker/celery_app.py`) is configured but has no registered tasks.

## Quality contract

The canonical rules for ICP hard/soft rules, lead decision states, evidence provenance, manager feedback,
quality KPIs, and batch behavior are defined in Phase 0's quality contract documents, not in this file:

- [Quality Contract](quality-contract.md) — the umbrella rulebook (ICP hard vs. soft rules,
  anti-fabrication rules, manager feedback, quality KPIs).
- [Lead Decision Policy](lead-decision-policy.md) — lead states, transitions, and accept/hold/reject/duplicate rules.
- [Evidence Policy](evidence-policy.md) — the provenance contract for critical fields.
- [Batch Contract](batch-contract.md) — rules for batch/bulk generation runs.

Every phase from Phase 1 onward must be implemented against these documents, not around them.

## Architectural principles

These principles apply to every future phase, not just the foundation.

### 1. Standalone first

The application must work independently of the company's existing website/repository. No phase should
assume access to that repository's code, database, or auth system. Integration happens later, deliberately.

### 2. Provider-independent

The system will eventually use multiple company data providers, people data providers, search providers,
enrichment providers, and LLM providers. No phase should hard-code business logic around a single vendor.
Provider integrations must sit behind interfaces/adapters (introduced when Phase 5 — Data Provider
Strategy — is built), so providers can be swapped, combined, or mocked without touching downstream logic.

### 3. Quality first

Leads are prioritized in this order, strictly:

```text
REAL + VERIFIED  >  REAL + PARTIALLY VERIFIED  >  HOLD  >  NEVER FABRICATED
```

A `HOLD` state is always preferable to a guess. No stage may synthesize a fact (an email, a title, a
company detail) that was not derived from real, traceable evidence. The full rules are in the [Quality
Contract](quality-contract.md) and [Lead Decision Policy](lead-decision-policy.md).

### 4. Hard ICP rules are deterministic

Hard ICP requirements (e.g. "must be in industry X", "must have 50–500 employees") are enforced by
deterministic code (the Hard ICP Rule Engine, Phase 3), not by LLM judgment. LLMs classify, reason, and
summarize; they never get to silently override a hard rule. See [Quality Contract §1](quality-contract.md#1-icp-rules-hard-vs-soft).

### 5. Evidence and provenance

Every critical fact attached to a lead (company detail, decision-maker identity, signal) must eventually
carry a source and a provenance trail. A fact without evidence is not a fact the system can act on.

### 6. LLMs reason, they are not the source of truth

LLMs are used for qualification, classification, and adversarial review — never as the origin of a fact.
Any claim an LLM produces must be checkable against evidence collected earlier in the pipeline.

### 7. Cost control

No paid providers (lead databases, search APIs, enrichment APIs, LinkedIn providers, LLM APIs) are wired
in during this stage. The architecture is built and validated locally with mocks first; paid integrations
are added deliberately, phase by phase, only when explicitly instructed.

## Why this stack

- **Next.js + TypeScript + MUI + React Query** — a conventional, well-supported combination for a
  data-heavy review/qualification UI (tables, forms, async data) without committing to a large design
  system build-out this early.
- **FastAPI + Pydantic + SQLAlchemy** — Pydantic schemas map naturally onto the ICP/evidence/lead data
  contracts this system will need; FastAPI's typing and automatic OpenAPI docs keep the provider-adapter
  interfaces introduced later self-documenting.
- **PostgreSQL** — relational integrity matters here (leads reference companies, people, evidence, and
  review decisions); JSONB support covers semi-structured provider payloads without a second database.
- **Redis + Celery** — company/people discovery, enrichment, and multi-source verification are
  I/O-bound and will need retries, rate limiting, and backoff — a conventional task queue fits better than
  ad hoc async code sprinkled through request handlers.

## What is intentionally not built yet

Per the Phase 0 scope, none of the following exist in this codebase: ICP builder, ICP normalization,
scraping, company/people discovery, enrichment, scoring, LLM qualification, adversarial review,
multi-source verification, deduplication, batch/bulk generation, human review workflows, provider
integrations (LinkedIn, company websites, paid data/search/LLM APIs), authentication, or production
deployment. These are tracked in the roadmap in the root [README](../README.md) and will be implemented
phase by phase.
