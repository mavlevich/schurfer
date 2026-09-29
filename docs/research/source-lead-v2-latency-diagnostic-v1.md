# HYP-012 v2 latency diagnostic protocol v1

Status: REGISTERED 2026-09-27, before any shadow latency, quote or outcome count was viewed.
Shadow execution (#454) was enabled on prod at 2026-09-27T17:50:23Z (see below). Only the service's
liveness (heartbeat timestamp) was checked at enablement.

Amendment A1, 2026-09-29, before the first diagnostic run: the weekly read is scheduled
for Monday 06:00 UTC and refuses until the database clock reaches week end plus six hours.
The six-hour grace is fixed in code, exceeds the shadow worker's ten-minute claim recovery
age, and is recorded in each artifact. The canonical CLI always writes under
`/runtime/research/source_lead_shadow_latency`; its path cannot be overridden.
Historical heartbeat-gap attribution is unavailable because Redis retains only current
health. This is an explicit deviation from the original coverage requirement below;
`no_attempt` remains in the full denominator, with no invented outage explanation.

## What it is for

The HYP-012b exploration found that Bybit's catch-up after a source pump is fast and small. It
did not show _where_ the delay between the source move and an executable quote goes. This protocol
decomposes that delay for the v2 cohort, so the next engineering decision (for example, a websocket
detector) is made on measurements.

It is operational only.

- It never changes the v2 cohort, its qualification, thresholds, estimand or its single formal
  read (`source-lead-forward-cohort-v2.md`).
- It never reads a return.
- A change it motivates to capture, detection or entry timing is a new version with its own start
  date. It never applies to the running cohort.

## Population and recording start

- **Population.** Every v2-qualified episode (`source_lead_qualified_capture_v4`, Bybit target)
  with `source_first_observed_at >= 2026-09-29T00:00Z`, and its row in
  `app.source_lead_shadow_attempts` (`source_lead_shadow_v1`). This boundary is the shadow worker's
  own `COHORT_START`, so every episode in the population is one the worker is meant to attempt.
- **Recording start.** `SOURCE_LEAD_MODE=shadow` first ran on prod at 2026-09-27T17:50:23Z.
  - Until 2026-09-29T00:00Z the worker selects no episode. That period is a warm-up of service
    liveness (heartbeats) only, with no attempts to report.

## The timing chain

All instants are UTC. Each segment is reported on its own.

| Mark | Field                      | Meaning                                              |
| ---- | -------------------------- | ---------------------------------------------------- |
| M    | none                       | first move of the source market (see "Not measured") |
| S    | `source_first_observed_at` | the scanner's first observation of the source pump   |
| O    | `observed_at`              | the capture's Bybit target observation               |
| Q    | `qualified_at`             | the episode qualified                                |
| F    | `first_seen_at`            | the shadow worker first saw the episode              |
| R    | `quote_requested_at`       | fresh Bybit book requested                           |
| V    | `quote_received_at`        | book received                                        |
| B    | `book_ts_ms`               | Bybit's own book timestamp (exchange clock)          |

| Segment                    | Definition | Stored as                               |
| -------------------------- | ---------- | --------------------------------------- |
| capture                    | O - S      | derived                                 |
| qualification              | Q - O      | derived                                 |
| pickup                     | F - Q      | `from_qualified_ms`                     |
| detection to shadow        | F - O      | `detect_latency_ms` (`late` above 30 s) |
| processing                 | R - F      | `process_latency_ms`                    |
| quote round trip           | V - R      | `quote_latency_ms`                      |
| book age                   | V - B      | `book_age_ms`                           |
| end to end after detection | V - S      | derived                                 |

`quote_change_bps` is described alongside, as the change of the executable $50 ask VWAP between O
and V. It is not slippage, because no order was sent.

## Not measured, and why

- **M, the first move on the source.** It needs the source venue's trades or book with exchange
  timestamps. We do not capture that for MEXC, Gate, BingX, BloFin or LBank.
- **The detection gap S - M.** It is therefore unknown, and not bounded by anything this protocol
  sees. The scanner cycle (60 s) only limits how late a crossed threshold is noticed. The time
  from the first move until the move reaches the signal threshold can be arbitrarily long. This
  protocol cannot size it.
- **Future capture.** A capture of source trades with exchange timestamps, a separate PR with its
  own start date, would add M.

## Coverage and missingness

Reported per UTC week, never imputed:

- **Coverage.** Qualified episodes from 2026-09-29T00:00Z, with and without an attempt row. A
  qualified episode without a row is `no_attempt` (for example, the worker was down).
  Historical heartbeat gaps cannot be listed under Amendment A1; the report marks
  attribution unavailable. The pre-cohort service warm-up had no eligible worker attempts.
- **Outcomes.** The attempt outcome counts, by the existing codes (`shadow_recorded`,
  `stale_book`, `no_book_timestamp`, `delivery_unknown`, `instrument_rules_unknown`,
  `evaluation_error` and the rest). A segment is computed only over the rows whose marks exist;
  its n is always shown.
- **Clock-skew flags.** Negative segments are counted and shown, not dropped.

## What is reported

For each segment: n, p50, p90, p99 and max, plus the `late` share. For `quote_change_bps`: n, p50
and p90, split by `late`. Weekly, next to the v2 operational health check.

## Pre-registered engineering rule

The rule is applied once, at the first scheduled weekly report after the window holds at least
30 `shadow_recorded` attempts. It is never applied to an ad hoc look.
The scheduled report runs Monday at 06:00 UTC, no earlier than six hours after that
week's end by the database clock. A missed timer firing is recorded with its actual read
time and cannot skip an earlier weekly artifact.

- **Scope of V - S.** V - S is computed over every attempt in the population that has a V, not
  only over `shadow_recorded` ones.
- **Detection next.** This branch needs all three:
  1. coverage: at least 90% of all qualified episodes in the window have an attempt with a V;
  2. median V - S at most 10 s;
  3. p90 V - S at most 30 s.

  Then the pipeline after detection is not the bottleneck. The next lever is detection, S - M,
  which needs the source-trade capture above before any websocket detector is built.

- **Pipeline first.** Otherwise: low coverage, a slow median or a long tail means the in-house
  pipeline (capture, qualification, pickup, quote, and the causes of missing attempts) is fixed
  first. The report names which condition failed.

Neither branch changes v2. Both only choose the next engineering PR.

## Reader implementation note (2026-09-29)

`source-lead-shadow-diagnostic --week-end YYYY-MM-DD` reads a closed UTC-week
prefix through Monday 00:00 UTC. It prints the current week's counts and timing
distributions, plus cumulative coverage. The first weekly report with at least
30 `shadow_recorded` attempts fixes the engineering branch. Each week's JSON is
written once with SHA-256 in the persistent artifact directory. Repeating a
week returns that saved report without querying the database. A later week
requires the immediately preceding saved report and carries its decision
forward, even if old attempt statuses have since changed. Missed weeks must be
recorded in order; the report records when each read actually occurred.
The timer invokes `--latest-closed-week` on Mondays at 06:00 UTC. Both modes use
the same fixed canonical directory. The production Make target accepts a week
argument but cannot redirect the artifact path through `ARGS`.

The report reads only qualifications, capture/target timestamps and shadow
attempt status, timestamps and `quote_change_bps`. It never reads prices,
returns, exit observations or `trade_decisions`. The `quote_change_bps` field
is descriptive and is not an estimate of realized slippage or profit.

The existing Redis heartbeat contains current health, not a durable history of
outage intervals. The reader therefore reports `no_attempt` from the database
and labels historical heartbeat-gap attribution unavailable; it never infers a
specific outage from a missing attempt. This limits diagnosis of missingness
but cannot make the 90% quote-coverage gate pass. The report's JSON contains a
SHA-256 of the rows read. Attempt status can later change through recovery,
so that digest identifies the read snapshot pinned in that week's operational
artifact, not an immutable formal v2 result. On production, the artifacts live
under `/runtime/research/source_lead_shadow_latency`.

Run the first closed cohort week no earlier than 2026-10-05T06:00Z. The CLI does
not send orders or trigger a formal cohort read.
