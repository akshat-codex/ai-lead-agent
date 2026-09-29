# Lead Decision Policy

Defines the canonical lead states, the conditions for moving between them, and the rules for the four
terminal outcomes. Governed by, and must be read alongside, the [Quality Contract](quality-contract.md).

## States

| State        | Meaning                                                                          |
| ------------ | --------------------------------------------------------------------------------- |
| `DISCOVERED` | Candidate company/person found by discovery; nothing verified yet.                |
| `ENRICHING`  | Enrichment providers are being queried for company/person data.                   |
| `VERIFYING`  | Identity verification and evidence collection are in progress.                    |
| `QUALIFIED`  | Hard ICP rules passed and evidence bar met; commercial/business-model analysis and scoring have run. |
| `REVIEW`     | Ready for human review (post LLM qualification / adversarial verification).       |
| `ACCEPTED`   | Terminal. Confirmed lead, ready for export.                                       |
| `HOLD`       | Suspended. Missing or conflicting information that further research could resolve. |
| `REJECTED`   | Terminal. Fails a hard ICP rule or is clearly commercially unsuitable.            |
| `DUPLICATE`  | Terminal. Matches an already-existing lead/entity.                                |

`ACCEPTED`, `REJECTED`, and `DUPLICATE` are terminal for the automated pipeline — they only change again
through explicit human review. `HOLD` is never terminal: it must always have a defined path back into the
pipeline (more research, a later batch run, or a human decision).

## Transitions

```text
DISCOVERED → ENRICHING → VERIFYING → QUALIFIED → REVIEW → ACCEPTED
                 │            │           │          │
                 └────────────┴───────────┴──────────┴──→ HOLD (missing/conflicting evidence)
                 │            │           │          │
                 └────────────┴───────────┴──────────┴──→ REJECTED (hard rule fails / clearly unsuitable)
                 │            │
                 └────────────┴──────────────────────────→ DUPLICATE (matches existing entity)

HOLD → (re-enters research) → ENRICHING / VERIFYING
HOLD → REJECTED   (further research confirms a hard-rule failure)
HOLD → REVIEW     (further research resolves the gap; ready for human/LLM review)
REVIEW → ACCEPTED / HOLD / REJECTED   (human decision, or LLM+adversarial verification result)
```

Dedup checks (`DUPLICATE`) can, in principle, trigger at any stage once Phase 19 exists — as soon as an
entity match is confirmed, the lead moves to `DUPLICATE` regardless of what stage it was in.

## Acceptance rules

A lead may move to `ACCEPTED` only when **all** of the following hold:

1. Every hard ICP rule (§1 of the Quality Contract) evaluates to `PASS`, deterministically, with no
   unknowns treated as passes.
2. Every critical field required to evaluate those hard rules has evidence meeting the minimum bar in the
   [Evidence Policy](evidence-policy.md).
3. There is no unresolved conflicting evidence on a critical field.
4. The lead is not flagged as `DUPLICATE`.
5. Where LLM qualification and adversarial review (Phases 16/17) are in the pipeline, they have run and
   raised no unresolved objection.

**Invariant:** a high commercial score, a favorable LLM opinion, or positive manager feedback can never
substitute for #1–#3. Scoring and LLM output may only affect ranking and routing to `REVIEW` — they have
no authority to flip a hard-rule failure into `ACCEPTED`.

## HOLD rules

A lead moves to (or stays in) `HOLD` when:

- A critical field required by a hard rule is missing, and further research (a retry, another provider,
  another search strategy) could plausibly resolve it.
- Two or more evidence records for the same critical field conflict, and the conflict isn't yet resolved
  by a higher-confidence source (see [Evidence Policy](evidence-policy.md)).
- A human reviewer or manager feedback explicitly requests `HOLD` pending more information.

`HOLD` is the default outcome whenever the system is uncertain in a way more research could fix. It is
always preferred over guessing (Quality Contract §2) and over forcing a premature `ACCEPTED`/`REJECTED`.

## REJECT rules

A lead moves to `REJECTED` when either:

1. **Hard rule failure** — any hard ICP rule evaluates to `FAIL` with sufficient evidence (deterministic,
   automatic). This is the primary and preferred rejection path.
2. **Clear commercial unsuitability** — the lead is unambiguously unsuitable on grounds outside the hard
   rules (e.g. an evidenced explicit exclusion, or a confirmed disqualifying fact such as "already a
   customer" surfaced by enrichment). This path requires:
   - A specific, logged reason (structured, not a raw score threshold).
   - Evidence support at the same bar as any other critical-field claim — a low-confidence or purely
     inferred signal is not sufficient grounds for automatic rejection under this path; it should route to
     `REVIEW` or `HOLD` instead.

A rejection is always reversible by human review (moving the lead to `REVIEW` or `HOLD` for a second
look) — `REJECTED` is terminal for the automated pipeline, not for a human overseeing it.

## DUPLICATE rules

A lead moves to `DUPLICATE` when the (future) deduplication engine (Phase 19) matches it to an
already-existing lead or resolved entity (company or person) with sufficient confidence. Until that engine
exists, no automated `DUPLICATE` transition can occur — duplicates can only be marked manually during
human review, with the reason recorded the same way a future automated match would be.

## Relationship to manager feedback

Manager feedback (Quality Contract §3) is recorded against a lead's outcome and reason, and can change
future ranking/discovery behavior. It does not directly move a lead between states except through the
normal `REVIEW` human-decision path — feedback is an input to learning, not a side-channel state
transition.
