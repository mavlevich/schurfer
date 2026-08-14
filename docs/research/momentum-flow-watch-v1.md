# Momentum flow WATCH v1

## Status and purpose

`momentum_flow_watch_v1` is a prospective, WATCH-only market-state evaluator for
Bybit linear USDT perpetuals. It records what the system knew and decided at each
closed UTC minute. It does not open a paper or real position and makes no claim of
profitability.

The worker is intentionally separate from the pump scanner. The scanner detects a
large price move that already exists. WATCH looks for a possible accumulation state
before that price threshold is reached.

## Frozen contract

The machine-readable source of truth is
`apps/analytics/schurfer_analytics/momentum_flow_watch_contract.py`. Its canonical
SHA-256 is registered in `app.momentum_flow_watch_runs` at the first service start.
The canonical contract JSON is stored beside it. A binary with a different hash or
contract body must refuse to continue the same version.

The pre-registered contract hash is
`f112c05005e8eb5c81670df09beedb741351bce55c307019aa347552c5dd6f97`.
It is a checked literal, not a value silently refreshed whenever the contract changes.

The first start also fixes `cohort_started_at`. Only fully closed buckets after that
instant enter the cohort. There is no CLI or environment override for the version,
hash, or cohort boundary.

| Input rule                          |                                                              Frozen value |
| ----------------------------------- | ------------------------------------------------------------------------: |
| History required                    | 61 consecutive complete bars spanning the prior 60 minutes through target |
| Current flow window                 |                                                                15 minutes |
| Flow baseline                       |                                                      preceding 45 minutes |
| Minimum quality-ready cross-section |                                                           100 instruments |
| OI rank                             |                       at or above current cross-section p90, and positive |
| Buy-imbalance rank                  |                  at or above current cross-section p90, and at least 0.10 |
| Flow-acceleration rank              |                 at or above current cross-section p75, and at least 1.50x |
| 15-minute gross flow floor          |                                                                10,000 USD |
| 60-minute price containment         |                                                          -5% through +12% |
| Maximum 15-minute price change      |                                                                       +6% |
| Maximum bucket-to-decision delay    |                                                               120 seconds |
| Episode rearm                       |                            5 consecutive quality-ready non-signal minutes |
| WATCH cooldown                      |                                                    360 minutes per symbol |

Percentiles use deterministic nearest-rank calculation. They use only inputs from the
same closed minute and never consult pumps, returns, fills, outcomes, or future bars.
The cross-sectional rule adapts to the current market regime while retaining fixed
rank and absolute boundaries.

## Input-only calibration snapshot

The absolute guardrails were reviewed against one input-only production snapshot
before the first WATCH cohort and without querying pumps, returns, fills, or outcomes.
The snapshot used the closed `2026-08-13T23:43:00Z` bucket, universe
`5d5026a767aa054a57f5f29f0bc33f5a060a48019b5a0ab137bbf6b63c6cc19f`, and capture
version `v1`. Of 516 persisted target-minute symbols, 163 satisfied the proxy for the
same strict 61-bar completeness, gap, fresh-OI, price, and positive-baseline rules.

| Input distribution                   |              Observed value |
| ------------------------------------ | --------------------------: |
| OI growth p50 / p90                  |          -0.0009% / 1.2235% |
| Buy imbalance p90                    |                      0.4320 |
| Flow acceleration p50 / p75 / p90    | 0.8178x / 1.3330x / 2.7251x |
| 15-minute gross flow p25 / p50 / p75 | 5,347 / 14,809 / 52,234 USD |
| 60-minute price return p01 / p99     |          -2.6965% / 3.5632% |
| 15-minute price return p99           |                     4.1723% |

This is calibration provenance, not evidence of predictive power. The percentile
ranks remain the primary adaptive thresholds. The fixed 1.50x acceleration and
10,000 USD flow floors exclude weak absolute states without making the observed
cross-section empty; the price bounds remain broad containment safeguards.

## Quality gates

Evaluation fails closed when any required input is unavailable or unsafe:

- unresolved exact venue symbol or universe version;
- missing, non-consecutive, incomplete, or gap-marked bars;
- missing price or a stale final quote observation;
- missing fresh OI at either the 60-minute anchor or current minute;
- no positive flow in the preceding 45-minute acceleration baseline;
- a cross-section smaller than the frozen minimum.

Carried-forward OI is not fresh. Its own event timestamp must fall inside the bar and
its receive timestamp must be known by evaluator start.
Insufficient market-wide cross-section never rearms an active symbol episode because
it is absence of decision evidence, not evidence that the symbol state cleared.

## Durable states and denominator

The worker writes one row for every symbol it evaluates, not only positive WATCH
decisions. Stable statuses are:

- `watch`;
- `rejected_quality`;
- `rejected_signal`;
- `suppressed_active_episode`;
- `suppressed_cooldown`.

Every row preserves features, cross-sectional thresholds, reason codes, data-quality
state, episode state, input hash, and source/receive/bucket/evaluator/decision times.
This is the denominator for future opportunity-rate, precursor-recall, precision,
false-WATCH, and latency reports.

State changes are persisted in the same transaction as the evaluation batch. The
append-only hypertable remains the decision audit, while a small current-state table
keeps at most one row per exact instrument for bounded restart time and is updated
only when that symbol's state changes. A single run cursor advances atomically after
the whole minute is verified. Repeating an already-written minute is allowed only
when its deterministic input hash matches.
Cooldown is measured in bucket event-time, while evaluator and decision timestamps
separately preserve actual processing latency.

Only one worker may own a WATCH version at a time. A session-scoped Postgres advisory
lock prevents a second instance from creating divergent state for the same cohort.

## Runtime and notifications

The service reads persisted bars and therefore does not share the capture process or
its queues. Start it explicitly with `make prod-momentum-watch-start`; this applies
the migration and starts only the profiled worker, without restarting the active
momentum capture canary.

Health is published at `market:momentumwatch:health` and is available through
`make prod-momentum-watch-health`. Qualifying WATCH rows also emit structured logs.

There is deliberately no direct Telegram sender in v1. The unified notification
outbox currently defines only its contract and audit schema; its consumer is not yet
implemented. A later producer migration may publish WATCH notifications through that
single gateway without changing this decision contract.
