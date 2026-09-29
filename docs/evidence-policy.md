# Evidence Policy

Defines the **contract** for evidence, not its implementation. The full Evidence Engine is Phase 11; this
document only fixes the shape and rules every later phase must design against.

## Evidence record contract

Conceptually, every evidence record supports one claimed fact and carries:

| Field         | Meaning                                                                      |
| ------------- | ------------------------------------------------------------------------------ |
| `field`       | Which critical field this evidences (e.g. `employee_count`, `current_title`). |
| `value`       | The claimed value for that field.                                            |
| `source`      | Where it came from (a specific URL, provider name + record id, document).    |
| `source_type` | Category of source (see below).                                              |
| `retrieved_at`| When the evidence was collected.                                             |
| `evidence`    | The supporting artifact/pointer (a quote, snippet, screenshot reference, or provider payload excerpt) — not just a bare citation. |
| `confidence`  | How trustworthy this single record is (see scale below).                     |

An evidence record is a claim about a fact, not the fact itself. A field is only treated as verified once
its evidence records clear the minimum bar defined below.

## Source types

- `COMPANY_WEBSITE` — the company's own site (about/team/careers pages, etc.)
- `PUBLIC_FILING` — regulatory or registry filings
- `LINKEDIN` — LinkedIn profile or company page
- `PROVIDER_API` — a third-party data/enrichment provider
- `NEWS` — news articles or press releases
- `SEARCH_RESULT` — a general web search result not covered above
- `MANUAL_HUMAN` — a human reviewer directly confirmed it
- `INFERRED` — derived by the system from other evidence, not observed directly (e.g. guessing seniority
  from a title string)

`INFERRED` is a source type, not a shortcut around evidence — it must still point to the evidence it was
inferred from, and it never counts toward the minimum bar on its own (see below).

## Confidence scale

- **HIGH** — directly observed on an authoritative, hard-to-fake source (company's own site, a filing, a
  human-confirmed fact).
- **MEDIUM** — from a generally reliable third-party source (a data provider, LinkedIn) that can be stale
  or occasionally wrong.
- **LOW** — inferred, indirect, or from a low-reliability source.

Confidence is per evidence record. A field's overall confidence is derived from its best supporting
record(s), not an average — one HIGH-confidence record should outweigh several LOW ones, which is the
concern of scoring/multi-source verification (Phases 15/18), not this contract.

## Minimum evidence bar

For the critical fields listed in the [Quality Contract §2](quality-contract.md#2-anti-fabrication-rules):

- At least **one** evidence record with `source_type != INFERRED` and `confidence` of `MEDIUM` or `HIGH`
  is required before the field can be treated as verified for hard-rule evaluation, shown as an accepted
  fact, or exported.
- A field backed only by `INFERRED` and/or `LOW`-confidence evidence is **not verified**. It may be
  displayed as a low-confidence hint during review, but it cannot satisfy a hard rule check and cannot be
  used to justify `ACCEPTED`.
- If two evidence records for the same field conflict, the field is **not verified** until the conflict is
  resolved (a higher-confidence source corroborates one side, or a human resolves it) — see [Lead Decision
  Policy: HOLD rules](lead-decision-policy.md#hold-rules).

## Outcomes when the bar isn't met

Per the Quality Contract's anti-fabrication rules, a critical field that cannot clear this bar results in
`HOLD` (if more research is plausible), `REJECT` (if a hard rule cannot be evaluated without it and no
further research is planned), or an explicit "unknown" — never a fabricated value.

## What this document does not define

Storage schema, evidence deduplication, multi-source corroboration scoring, and the retrieval/collection
mechanics all belong to Phase 11 (Evidence Engine) and Phase 18 (Multi-Source Verification). This document
only fixes the contract those phases must implement against.
