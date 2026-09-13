# State and plan — 2026-09-13

A consolidation of a single research session. It records what was established, honestly
separates the session's own exploratory probes (mostly rediscovery, superseded) from
what the existing corpus already confirmed, flags a production disk risk, and sets a
short prioritized plan. It changes no production config and promotes nothing.

## TL;DR

- **The executable pump-short is confirmed NEGATIVE** (not new; read from
  `app.research_report_runs`, the record of real confirmatory runs). `liquid_taker_
candidate_v1` (HYP-008): 802 eligible episodes, only 151 tradeable, net expectancy
  point estimate **-0.224%/episode, 95% CI [-0.455%, -0.0096%] entirely below zero**.
  `liquid_taker_wider_stop_shadow_v1` (HYP-010): paired delta +0.084%, CI crosses zero
  (no rescue). Both `do_not_promote`. The other register families were never run to a
  verdict; the only DB-registered cohort is the falsified early_momentum_v4.
- **The single positive edge candidate in the whole corpus is HYP-012 cross-venue
  source-lead LONG** (`docs/research/discovery-ledger.md`): enter long on Binance/Bybit
  after MEXC/Gate show the pump first. All 4 routes positive and Holm-significant
  (+1.8% to +3.4%, p=0.0004-0.0016), robust under leave-one-out and busiest-week
  exclusion. The symmetric confirmation-time SHORT is loss-making. It is a `candidate`,
  not confirmed.
- **HYP-015 hold12h paper worker was STARTED today** (owner-authorized), paper-only.
  The untouched forward cohort accumulates from 2026-09-13; a net verdict WITH funding
  is weeks away.
- **Data is already rich.** The cold-bar export is `COPY (SELECT * ...)` of
  `bybit_momentum_bars_1m` (OHLC, bid/ask, microstructure, created_at), daily; OI,
  funding, and liquidations are captured in their own tables. The local 11-column
  `bars-window.parquet` is a hand-cut accumulation subset, not the real export.

## This session's own probes — treat as superseded rediscovery

Run on the impoverished, truncated local window (ends 2026-09-11, before the real
pumps). Saved on branch `research/pump-domain-exploration-2026-09-13` as evidence, not
as claims.

- **accumulation-LONG** is a positive-SKEW "lottery", not a steady edge: median return
  negative at every hold, win 28-48%, but a fat right tail that grows with hold length
  (240m -> 3d, max +153%) and pulls the 3-day mean net slightly positive. The true tail
  is truncated (LSK went ~20x on Sep 12-13, outside our window). "240m long is dead"
  was a hold-horizon artifact.
- **pump-short probe** (+1.5%, monotonic dose-response) was NAIVE: it skipped the
  market-quality/executability gate (which culls ~81% of episodes), skipped funding,
  and its window contained no steamroller. The rigorous apparatus already had it
  negative (above).
- **exit-policy probe** (patient hold beat stops) was close-only (no high/low) and
  steamroller-free, so it is optimistic and now moot given the confirmed-negative base.

The genuine value of the session was mapping the frontier (reading the ledger and the
confirmatory-run record), not these probes.

## Corpus status (from the discovery ledger + report runs)

| line                                                            | status                                                                    |
| --------------------------------------------------------------- | ------------------------------------------------------------------------- |
| HYP-012 source-lead cross-venue LONG                            | candidate (only positive) — blocked on identity registry + forward cohort |
| HYP-015 momentum-flow hold12h                                   | candidate — forward paper worker STARTED 2026-09-13                       |
| liquid_taker HYP-008 / HYP-010 (executable pump-short)          | do_not_promote (net negative)                                             |
| HYP-011 candle-anomaly, HYP-013 token-behavior, HYP-014/016/017 | parked / insufficient / inconclusive                                      |
| early_momentum_v4                                               | falsified                                                                 |

## Production disk / backup risk (found today)

- `/` was 69% full (49G/75G). `prod-backup` refuses when free space is under ~2x the DB
  size (~30GB needed, ~24GB free), so scheduled local pg_dump backups were likely
  FAILING (offsite borg may still stream). Freed **10.26GB of docker build cache**
  (`docker builder prune -af`) -> 55% used, backups unblocked. That is symptom relief.
- **Root cause:** timeseries hypertables (`bybit_momentum_bars_1m`, etc.) already have
  Timescale retention + compression, so they are bounded. The unbounded growth is in
  plain `app` tables with no retention: **`pump_derivatives_context_samples` = 3.0GB**
  (top), then `momentum_flow_paper_probes` 612MB, `funding_rate_snapshots` 246MB,
  `oi_snapshots` 146MB. As the DB grows, the 2x-DB backup guard re-squeezes disk.

## Plan (prioritized; do not scatter)

1. **Disk / data lifecycle (root fix):** decide a retention/archival policy for the
   unbounded app tables (especially the 3GB derivatives-context), and/or attach a
   Hetzner block volume for longer hot history (fat-tail work needs long history).
   Confirm scheduled backups now pass. No prod data is deleted without owner sign-off.
2. **HYP-012 (the only positive edge):** passively grow the canonical-identity registry
   (the bottleneck: ~5 assets approved, ~10 qualified leads/week) and accumulate the
   qualified forward cohort; revisit the forward verdict at >=100 pairs / >=30 clusters
   / >=4 weeks on `identity_verified=true` routes. Months, mostly passive.
3. **HYP-015 hold12h:** let the worker run; read the paired net verdict (with funding)
   against contemporaneous `momentum_flow_paper_v1` in a few weeks.
4. **ENG-024** (existing primary): unchanged — verify in prod.
5. **Do NOT open new cold-probe lines** — the ledger's own multiple-comparison warning.

## Theory: the +20/+30% threshold is a late, reactive radar

By the time a token is +30% the move is largely done (LSK ran ~24h of accumulation and
~+10% drift before its blow-off). Catching it EARLIER is more valuable, but earlier
detection trades precision for earliness: most quiet accumulations never become pumps,
so an early signal has a low base rate (many false positives). This is exactly the
accumulation-LONG "lottery" profile above (catch early, most lose small, rare huge win).

The one EARLY signal with a real positive edge is **HYP-012 cross-venue lead** — it is
literally "catch it earlier": act when MEXC/Gate move first, before Binance/Bybit
confirm. So "catch earlier" does not mean lowering the +30% execution floor; it means
watching leading precursors (source-venue lead, net-buy accumulation, OI growth), each
needing its own base-rate/precision validation. The team already lowered the
MEASUREMENT floor to +20% (`PUMP_MEASUREMENT_MIN_PCT=20`, HYP-003) while keeping
execution at +30%, so earlier data is already being collected. Going earlier than that
(pure pre-breakout accumulation) is the frontier — high value, low precision, must be
validated on base rates, not tuned on winners.

## Backlog idea: pump-anatomy diagnostic

Event-triggered (on a `pump_event` radar hit), NOT per-token continuous, so load is
light per event. Given a symbol+time it would assemble: pre-pump net-buy accumulation,
OI growth, liquidations/squeeze, cross-venue lead timing, and the forward outcome — the
LSK walk-through, made repeatable. Diagnostic-only (understanding + universe-coverage
gaps like LONGXIA, which is not captured at all), never a promotion signal. A full
14k-event backfill is the only heavy variant and would be one-off.

## To consult the colleague

- HYP-012 identity-registry expansion: is prioritizing canonical chain+contract mapping
  worth it, given it gates the only positive edge?
- Disk retention policy for the unbounded app tables (esp. derivatives-context 3GB), and
  whether a Hetzner volume for longer hot history is justified.
- Whether the pump-anatomy diagnostic is worth building now or parked.
