# Batch Contract

Rules for batch/bulk lead generation runs (Phases 21/22). This document defines the contract only —
the batch orchestrator itself is a future phase.

## Core rule: never force-fill a batch

A requested batch of **N** candidates may legitimately produce **fewer than N** `ACCEPTED` leads if the
market, the ICP, or available evidence doesn't support more. No stage may:

- Lower the evidence bar to manufacture more `ACCEPTED` leads.
- Reclassify `HOLD`, `REJECTED`, or `DUPLICATE` leads as `ACCEPTED` to hit the requested count.
- Relax a hard ICP rule for the remainder of a batch because too few candidates qualified.

Under-delivery against `requested` is an expected, reportable outcome — not a failure to be silently
patched over. This follows directly from the Quality Contract's [Good-fit precision and false-positive
KPIs](quality-contract.md#4-quality-kpis): a batch optimized to hit its requested count would trade away
exactly the metrics that matter.

## Batch metrics

Every batch run reports, at minimum:

| Metric       | Meaning                                                              |
| ------------ | ----------------------------------------------------------------------- |
| `requested`  | The target number of leads asked for.                                |
| `researched` | Candidates that entered the pipeline and were actually processed (enriched/verified). |
| `accepted`   | Leads that reached `ACCEPTED`.                                       |
| `held`       | Leads that ended the batch in `HOLD`.                                |
| `rejected`   | Leads that ended the batch in `REJECTED`.                             |
| `duplicates` | Leads that ended the batch in `DUPLICATE`.                            |

**Invariants:**

- `accepted + held + rejected + duplicates ≤ researched` (a candidate can still be mid-pipeline when the
  batch ends, or the batch may stop early — see below).
- `researched` may be less than, equal to, or greater than `requested`: the orchestrator may need to
  research more candidates than requested to find enough qualified ones, and is equally allowed to stop
  short if candidates run out.
- `accepted` is reported honestly even when `accepted < requested`. This is not an error state.

## Completion condition

A batch is considered complete when either:

1. `accepted` reaches `requested`, or
2. The orchestrator has exhausted a reasonable set of discoverable candidates for the given ICP and
   provider budget, whichever comes first.

In case 2, the batch still reports full metrics (including the shortfall) rather than silently returning
fewer results without explanation.
