# Binance WATCH input-readiness v1

## Incident summary

From 2026-08-15 (momentum_flow_watch_binance's own first startup) through
2026-08-17, `momentum_flow_watch_binance` produced **zero** `watch`
decisions, and `momentum_flow_paper_binance` opened **zero** positions,
despite both workers reporting Redis health `status: "ok"` the entire
time. No Binance trade alerts ever reached Telegram in that window --
correctly, as it turns out: the notifier never received a single Binance
WATCH decision to alert on. This is a producer/consumer contract
incompatibility, not a quiet market, a notifier bug, or bad luck.

```
Binance capture (aggTrade + OI poll)
        |
bars with OpenPrice/HighPrice/LowPrice/ClosePrice permanently nil
  (documented in cmd/momentumcapturebinance/main.go's own package doc
  comment since feat/binance-momentum-capture-v1 -- see docs/research/
  binance-momentum-capture-v1.md's own "What this still cannot capture")
        |
momentum_flow_watch_evaluator.prepare_symbol_evaluation requires
close_price on every bar in the lookback window
        |
100% rejected_quality (missing_price on every single evaluation)
        |
0 watch decisions -> 0 paper entries -> 0 Telegram alerts
```

The notifier itself is not implicated anywhere in this chain: it alerts
on WATCH/paper rows that exist, and none were ever created.

## Why "ok" health did not catch this

A worker's own health status answers "did my own tick run without
throwing," not "is what I am built to detect even reachable given my own
upstream data." A quiet market and a structurally blocked producer both
produce the exact same observable output from inside the worker's own
loop: zero watches this tick, no exception. Nothing before this PR
distinguished them.

## Two independent, confirmed root causes

Measured directly against real prod data (2026-08-17), not assumed:

1. **`missing_price`** (100% of evaluations): Binance capture has no
   ticker/price feed at all (`binance.Adapter` deliberately does not
   implement `momentumsource.TickerSource`; OI is REST-polled with no
   price attached). `momentum.Engine.AddTrade` also never sets
   `ClosePrice` -- OHLC price is exclusively an `AddTickerObservation`
   concern in the shared engine today. Fix: PR2
   (`feat/momentum-trade-price-source-v1`).

2. **`missing_fresh_oi`** (~94% of evaluations, including many that would
   otherwise pass `missing_price` once PR2 ships): `binance.PollOpenInterest`
   runs a single `time.Ticker` at a per-symbol delay of
   `60s / len(symbols)` (~114ms for ~525 symbols), calling a **blocking**
   HTTP request synchronously inside each tick. Go's `time.Ticker` drops
   missed ticks rather than queueing them, so any request slower than
   ~114ms stalls the whole round-robin. Measured real per-symbol OI
   refresh gaps over a 30-minute prod window: p50 127s, p95 255s, p99
   505s, max 1010s -- 2 to 17x slower than the 60s
   `momentum_flow_watch_evaluator._fresh_oi` requires (OI event must land
   within the exact same 1-minute bucket being evaluated). Binance's own
   rate budget (2400 request-weight/min) has ample headroom for 525
   sequential requests/min; the bottleneck is the blocking-call-inside-
   ticker structure, not the rate limit. Fix: PR3
   (`fix/binance-oi-poll-scheduler-v1`).

## What this PR (PR1) actually does

Does **not** fix either root cause -- it makes the workers unable to lie
about them. `schurfer_analytics.momentum_flow_producer_readiness`
introduces two additional worker health statuses, alongside the existing
`starting`/`ok`/`degraded`:

- `blocked_upstream_incompatible`: the readiness check ran and genuinely
  found the upstream not ready.
- `degraded_dependency_unavailable`: the readiness check itself could not
  run (a transient DB/Redis error) -- kept distinct because it is
  infrastructure flakiness, not a producer/consumer contract problem, and
  an operator needs to tell the two apart.

- `run_watch_worker` checks `MomentumFlowWatchRepository.
has_any_recent_valid_price` (a _complete_ bar with `close_price > 0`,
  not just non-NULL) **every tick**, before `due_buckets` -- not a
  startup-only gate. When not ready, it writes the appropriate status and
  skips straight to the next tick without calling `due_buckets` at all.
- `run_paper_worker` checks its own upstream WATCH worker's health hash
  (by `contract.watch_version`) **every tick, before `process_tick`** --
  not after, and not only at startup. `process_tick` gained an
  `allow_new_entries` parameter: when the upstream is not ready, new
  WATCH candidates are never claimed (`due_watches` is not even called),
  but `expire_deadlines`/`monitored_probes`/`_process_probe` -- an
  already-open position's own stop, max-hold close, and horizon-outcome
  bookkeeping -- run exactly as if nothing were blocked. A stale `status:
"ok"` (a hard-crashed WATCH process leaves its last write sitting in
  Redis forever otherwise) does not count as ready either --
  `generated_at` must be within `UPSTREAM_HEALTH_MAX_AGE_SECONDS` (60s)
  of now.

Neither worker raises or exits when blocked. Both stay in their own
normal loop, reporting the appropriate status each tick, and resume `"ok"`
on their own the moment the upstream recovers -- no restart needed.

This satisfies the "no-output alarm after warm-up" requirement by
construction rather than a separate timer: a worker that cannot produce
real output now cannot hold status `"ok"` at all -- there is no window
where a genuinely broken producer looks identical to a quiet market in
health output.

Known, accepted limitations:

- A brand-new venue's very first WATCH tick, moments after its own
  capture process begins, has no bars at all yet in the lookback window
  either, and this check cannot tell that apart from a structurally
  incompatible producer. Not a practical problem for any venue this repo
  captures today (every one has run for hours before its own WATCH worker
  is ever started).
- `has_any_recent_valid_price` is a `LIMIT 1` existence check against ONE
  symbol somewhere in the whole captured universe, not a coverage ratio.
  It answers "is price capability present at all" (the binary failure
  mode the actual incident was: Binance had zero valid prices anywhere),
  not "is the full cross-section/OI ready for a real decision." Once PR2
  ships trade-derived price, a single symbol with a valid trade would
  already flip this to `True` even if hundreds of other symbols and OI
  freshness are still not ready -- that stronger, coverage-aware question
  is PR4's job, not this check's. Do not read a passing check here as
  "the producer is fully healthy."

## Colleague review: what changed after the first draft

The first version of this PR was not mergeable. A colleague review found
three P1-severity defects, all now fixed (see the corrected description
above) and covered by new tests:

1. **A blocked upstream stopped managing already-open positions.** The
   first draft's `run_paper_worker` raised at startup if the upstream was
   not ready -- but this worker also owns stop-loss/max-hold/horizon-
   outcome bookkeeping for positions that are already open. A restart
   while blocked would have left those positions completely unmonitored,
   a materially worse failure than the one being fixed. Fixed via
   `process_tick`'s own `allow_new_entries` parameter (see above).
2. **The readiness check ran after `process_tick`, not before.** The
   first draft opened new entries and only checked upstream readiness
   afterward for that same tick's own health report -- meaning a tick
   could act before confirming it was allowed to. Fixed by moving the
   check to the top of the loop.
3. **A stale `status: "ok"` counted as alive forever.** The first draft's
   `_upstream_watch_blocked` only read the `status` field. A hard-crashed
   WATCH process (OOM-killed, host reboot -- anything that skips the
   graceful `blocked_upstream_incompatible` write path) leaves its last
   `"ok"` sitting in Redis with no further updates; paper would have kept
   believing it forever. Fixed via `upstream_health_is_ready`'s own
   `generated_at` freshness bound.

Two P2-severity findings, also fixed:

4. `has_recent_price_data` (renamed `has_any_recent_valid_price`) did not
   require `close_price > 0` or `complete = true`, and the module's own
   docs overclaimed what a single-row existence check could prove. See
   "Known, accepted limitations" above for the corrected claim.
5. Both workers originally raised/exited when blocked, relying on
   Docker's `restart: unless-stopped` policy to retry -- pure churn for a
   condition restarting cannot fix. Fixed by folding the readiness check
   into the normal per-tick loop instead of a startup-only gate that
   crashes.

## Retroactive label

The 2026-08-15T00:00Z to 2026-08-17T08:00Z period for
`momentum_flow_watch_v1_binance` / `momentum_flow_paper_v1_binance` is
`input_contract_incompatible`, not "the market gave no signals." Every
row those two workers wrote in that window is a real, correctly-computed
`rejected_quality` decision against real captured data -- the data itself
(flow and OI, once PR3 lands) remains usable for offline discovery. Price
cannot be reconstructed from what was captured; a separate historical
1m-kline backfill would be needed for that, with its own provenance, and
would not qualify as prospective evidence for any forward cohort.

## Process critique (why this shipped undetected)

1. Producer (`cmd/momentumcapturebinance`) and consumer
   (`momentum_flow_watch_evaluator`) were tested independently, never
   end to end.
2. `momentum_flow_watch_evaluator`'s own tests supply synthetic
   `close_price` values on every `WatchBar` -- a shape the real Binance
   producer has never once produced. PR2 adds the missing end-to-end test
   (synthetic Binance aggTrade + OI -> Engine -> Writer -> WATCH
   repository -> Evaluator, asserting no `missing_price`).
3. Every existing activation-gate check (RAM, disk, container health) is
   a resource check, not a semantic-compatibility check.
4. `status: "ok", total: 0` was read as "a real, if quiet, market" instead
   of triggering investigation -- exactly the ambiguity this PR removes.
5. The known limitation was documented in prose (`cmd/momentumcapturebinance`'s
   own package doc comment) but never turned into a machine-checked gate
   until now.

## What's next

- PR2 `feat/momentum-trade-price-source-v1`: trade-derived OHLC for
  Binance specifically (`momentum.Engine` gets a configurable
  `PriceSource`; Bybit stays byte-for-byte unchanged), with explicit
  price provenance fields, not a silent repurposing of ticker semantics.
- PR3 `fix/binance-oi-poll-scheduler-v1`: bounded concurrent OI polling
  (token bucket, 4-8 workers, per-symbol cadence, latency/429 telemetry).
- PR4 `analysis/binance-watch-input-coverage-v1`: 24-48h coverage
  measurement once PR2+PR3 are live -- descriptive only, no threshold
  tuning, no outcomes.
- PR5 `feat/binance-momentum-watch-v2` (conditional on PR4's own
  findings): a new, explicitly versioned contract if exact-minute
  freshness genuinely cannot be met, with point-in-time age-bound
  semantics instead -- never a silent edit to the frozen v1 contract.
  `momentum-watch-binance`/`momentum-paper-binance` stay stopped on prod
  until this ships.
