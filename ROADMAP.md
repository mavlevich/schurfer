# Roadmap

> Living document. Updated as we progress. Last refreshed 2026-08-03.

## Guiding principle

The biggest unknown is whether the strategy has edge after fees, funding, and
slippage. The most expensive mistake is not architecture. It is under-collected,
non-recoverable data. Order-book depth and spread at signal time cannot be
reconstructed later. So we start collecting evidence now and build everything else
in parallel or after.

"Ship new functionality over refactoring working code" still holds. The point is
that right now the highest-value new capability is the measurement layer, not a
strategy feature. Code can be written any time. Today's order book will not exist
tomorrow.

The parked idea catalog lives in [IDEAS.md](IDEAS.md). It is frozen until edge is
proven. Post-MVP strategy and exit improvements live in the exit-strategy notes.

## Current state (2026-07-29)

- Live in production on Hetzner. Private access over Tailscale only. Caddy serves
  the Tailscale hostname with a static cert. Public ports 80 and 443 are closed
  with ufw.
- Trading mode is `AUTO_TRADE=false`, `DRY_RUN=true`. No real orders. Paper
  simulation only, accumulating data. `SCORE_THRESHOLD=6`.
- The durable decision/outcome dataset and market-quality gate are live. The scanner
  now has 17 configured linear-USDT perp venues. The immediate task is measuring
  which venues add unique discoveries or useful lead time before adding more feeds.

## Research portfolio and capital discipline

Schurfer has two explicit goals. First, test whether a strategy has executable net
edge. Second, build a reusable market-research platform only where shared
infrastructure directly reduces the cost or latency of those tests. Platform work is
not a substitute for strategy evidence.

### Four research levels

| Level        | What happens                                            | Parallelism                           | What the result means                  |
| ------------ | ------------------------------------------------------- | ------------------------------------- | -------------------------------------- |
| Observation  | Bounded collectors record non-recoverable data          | Several collectors at once            | Creates a dataset, no claim            |
| Discovery    | Cheap screens against already-collected/historical data | Wide, batched — many variants at once | Generates a hypothesis, proves nothing |
| Confirmation | A frozen contract plus a new untouched forward cohort   | At most 2 concurrent lines            | Tests whether an edge reproduces       |
| Promotion    | Paper fills with real costs, then micro-live            | One strategy at a time                | Tests real executability               |

Discovery is meant to be wide and cheap — running many combinations against one
IDEAS.md candidate in a single pass is encouraged, not discouraged. It is not free or
unlimited, though: reusing one historical window across many variants turns that
window into a training set for everything screened against it, so a good result from
the same window afterward is not new evidence — it needs its own untouched forward
cutoff before Confirmation, exactly like a cross-family result already requires. A
batch of many cheap screens will also produce false positives at a predictable rate
even when nothing in it has real edge. [docs/research/discovery-ledger.md](docs/research/discovery-ledger.md)
logs every registered screen, including rejected and parked ones, so a batch can
never quietly present its one positive row as the whole story.

The portfolio is bounded as follows:

- From `2026-07-29`, spend at most 10 new experiment families before a portfolio
  review on `2026-11-30`. An experiment family is a new collector, signal, replay
  family, or execution model entered in the discovery ledger or moved to
  Confirmation — not a count of pull requests. One family can take several PRs
  (infrastructure, then a screen, then a report); one PR can also carry more than one
  family. Track pull-request count separately as an engineering-velocity signal, not
  as this budget. Maintenance, security fixes, and re-running an already-registered
  report never consume it. Families merged since `2026-07-29` have not yet been
  counted against this cap under the new unit — do that count before registering
  another new family.
- Keep no more than two active Confirmation-level lines (a frozen contract plus its
  own forward cohort) at once. Pump reversion is the primary line. One cheap
  market-intelligence probe may run in parallel. Other ideas stay in Discovery until
  a line passes its gate or is stopped.
- A sample is not promotion-ready from event count alone. It must cover at least four
  distinct UTC calendar weeks, report concentration by week and asset, and show
  sensitivity to removing the busiest week. A future regime classifier may refine
  this rule, but cannot weaken it retrospectively.
- Within-family Holm or Bonferroni correction does not control false discovery across
  all research directions. Results from separate listing, order-flow, on-chain, and
  pump-reversion families remain discovery evidence until they survive an untouched
  forward cohort. A single nominal `p < 0.05` result across many directions is not a
  production authorization.

The current `$50` paper notional is a comparable research unit, not an earnings
claim. Every candidate must publish capacity and capital economics before micro-live:

1. estimate executable opportunities per day, fill rate, net basis points per trade,
   concurrent positions, capital occupancy, and monthly P&L at the measured notional;
2. estimate the notional ceiling at the candidate's impact limit by venue and show
   whether expected profit still exceeds server, data, funding, and operational costs;
3. size from fixed dollar risk, not leverage. Cap notional by risk budget divided by
   stop distance, measured executable depth, and portfolio heat;
4. if micro-live is later authorized, begin at the lower of `$50` notional, the
   venue's practical minimum, `0.25% of allocated equity / stop_fraction`, and the
   measured low-impact capacity. Do not increase more than `1.5x` at one checkpoint;
5. require at least 50 new closed live observations with realized fills and costs,
   stable drawdown, and no risk-control breach before considering another size step.

No live capital is authorized by this roadmap. If conservative capacity implies
economically immaterial profit even when the edge survives, stop strategy-specific
engineering and keep only the reusable research output.

## Committed next pull requests (2026-08-03)

Keep the current measurement services running while this queue is executed. Safety
and data-integrity fixes do not consume the evidence-producing PR budget. Do not mix
these independent changes into one branch.

1. **[Completed] Finish point-in-time source-lead identity review.** Merge the current bounded
   review-queue PR after independent review. The authenticated page exposes only raw
   source/target identity observations; the Python report remains the sole conflict
   classifier. No equal-ticker link is approved by the UI or report skeleton.
2. **[Completed] Repair Bybit WebSocket read liveness.** Add a renewable read
   deadline to ticker and public-trade streams, reset it after every received frame,
   and route silence through the existing reconnect loop. Publish timeout/reconnect
   diagnostics and test a half-open connection. This protects the order-flow evidence
   being collected now and is the immediate next PR after identity review.
3. **[Completed] Make execution order locks renewable.** Replace the fixed 30-second
   assumption in both open and close paths with an owner-checked lease heartbeat and
   retain the atomic owner-only release. Test a deliberately slow exchange path and
   lease loss. This is required before button-approved or automatic live orders, but
   does not block current `DRY_RUN` measurement.
4. **[Completed] Escalate unresolved exchange fills durably.** Resolve price from average, price,
   then valid cost/filled and trade evidence. If it remains unknown, persist a
   de-duplicated incident, revoke PnL readiness, alert Telegram once, retry, expose it
   in status, and send recovery after reconciliation. Never fabricate a fill price.
5. **[Stopped 2026-08-06: no lane passed] Close the Bybit order-flow
   discovery gate.** Read early-long, squeeze-avoidance, and
   delayed-short as separate books. If no lane has pre-trigger lead time,
   multi-asset/day robustness, and plausible after-cost value, stop the
   order-flow line and do not add Binance or L2. If one lane passes, register exactly
   one untouched forward shadow contract:
   - early-long wins: Bybit-only aggressive-buy acceleration while price remains
     below a frozen move cap, with a source-time $50 quote, rejected fills as cash,
     fixed-dollar risk, a hard stop, and bounded 30/60/120-second exits;
   - squeeze-avoidance wins: add one shadow-only veto to the existing short book;
   - delayed-short wins: add one shadow entry-timing challenger after buy pressure
     fades.

   **2026-08-06, step 1: `gate_inconclusive_endpoint_completeness`, not a lane
   verdict.** Ran and archived the unmodified `v1` report (never edited the
   registered contract for this decision) — see
   `backups/reports/orderflow-pilot-v1-2026-08-06.{json,md}`. Result: 8 complete
   matched episodes, 8 asset clusters, 5 UTC market days, against a registered
   threshold of 100/30/7. Root cause found: `ORDERFLOW_MAX_ENDPOINT_STALENESS_MS`
   (5000ms) is applied independently at the anchor plus four post-trigger
   horizons across the event and all 3 controls — roughly 20 conditions that must
   _all_ pass — and on Bybit's actual per-symbol trade frequency for pump
   candidates, the anchor alone is fresh enough only ~35% of the time. This said
   the registered `v1` completeness contract was a poor fit for real trade
   frequency; it did not by itself say whether any lane has a pre-trigger effect.

   **2026-08-06, step 2: `bybit_orderflow_endpoint_sensitivity_v1` closes the
   gate — stopped, no lane passed.** Built a read-only, versioned sensitivity
   report (`orderflow_endpoint_sensitivity_report.py`) that re-parses the same
   raw captures without touching `v1`, evaluating 5/10/15/20/30s side by side
   (60s shown only as an explicitly unusable diagnostic bound for the 1-minute
   lane) — see `backups/reports/orderflow-endpoint-sensitivity-2026-08-06.md`.
   At 15-20s the sample is already adequate (92-146 complete episodes, 33-45
   clusters, 8 UTC days), so this is a real read, not another data-volume
   shortfall. Result: every lane's rank correlation between its feature and its
   return lift collapses toward zero as the sample grows from N=8 (5s) to
   N=146-232 (20-30s) — early-long 0.69→-0.04, squeeze-avoidance 0.88→0.07,
   delayed-short's return lift even flips sign (-0.66%→+0.03%). This is the
   textbook signature of small-sample noise dissolving with more data, not a
   real effect strengthening or holding stable. early-long's median return lift
   stays positive across all bounds, but with no accompanying stable
   correlation this is better explained by a tautology (a token just flagged as
   pumping tends to keep rising briefly relative to an arbitrary matched
   control) than by the order-flow feature itself. No lane showed pre-trigger
   lead time, robustness, or plausible value — stop the order-flow line. Do not
   add Binance, L2, ticker/mid capture, 5-6 controls, or a 24h accumulation
   layer. The freed market-intelligence slot goes to item 6 (Gate source-lead),
   not both at once.

6. **Advance Gate source-lead only after identity evidence exists.** Review exact
   Gate/Binance/Bybit links, archive authoritative evidence and hashes, bump registry
   plus qualification versions, deploy, and choose the next clean UTC cutoff for one
   `gate_source_lead_4h_v1` cohort. Historical confirmed survivors cannot enter it.
7. **[Completed] Fix duplicate-alert spam from premature episode closure on
   thin/flaky venues.** `app.pump_events`
   closes an episode once `miss_count` reaches its threshold and opens a new
   `pump_event_id` on the next detection (`persistence.py`'s `_CLOSE_DUE` /
   `_INSERT_EPISODE`). The notifier de-dupes per `pump_event_id`
   (`notifier.go`'s `seenKey`), so one real, still-elevated pump that briefly drops
   out of the scan window on a thin-liquidity/flaky venue re-alerts every time it
   reopens. Observed on `2026-08-03`: CATE on LBank sent 4 Telegram alerts across
   ~70 minutes for what was one continuous +4900-5500% move, confirmed via 4 distinct
   `pump_event_id`s (2569, 2578, 2580, 2584) in the notifier's own logs — not a
   Redis/notifier restart, not a duplicate-notification bug, just repeated episode
   reopening. Not a safety issue (no capital at risk), just channel noise. Fix
   direction chosen: fix the notifier only, leave `app.pump_events`/`miss_count`
   untouched (episode-lifecycle semantics may matter to other consumers, not
   evaluated). Added a dedicated `notifier:reopen_cooldown:{base}` key (45 minutes,
   set above the largest observed reopen gap in the CATE incident with margin) that
   is refreshed on every suppressed reopen, so it keeps sliding for as long as a
   base keeps reopening and only lets a new alert through once the base has been
   fully quiet for the whole window. This is a bug fix, not an experiment family,
   and does not consume the evidence budget above.

The liquid-taker and wider-stop cohorts continue unchanged toward their August 27
and August 29 checkpoints. The open-ended margin study remains a background boundary
test. Korean listings, more order-flow venues, DEX/on-chain smart money, paid data,
and ML stay parked until one active lane passes or is stopped.

## Near-term delivery sequence: execution and exit decision

The measurement foundation and shared paper/replay performance accounting are live.

- [x] Automate registered checkpoint closure on the host without mounting the Docker
      socket into an HTTP service. An hourly systemd timer runs at most one due report,
      enforces a host lock plus RAM/disk preflight, archives validated JSON and SHA-256,
      preserves candidate registry writes, sends edge-triggered Telegram state changes,
      and exposes sanitized next-run/report/verdict state on the authenticated Research
      page. Terminal outcomes do not rerun automatically and the scheduler cannot alter
      production strategy settings.

The immediate question is narrower: does the tradeable pump-reversion signal survive
the production exit mechanics, and can its execution be improved without taking
unbounded tail risk? Existing entry, score, and exit cohorts continue collecting, but
no new confirmatory family is added until this question is resolved. Production
remains `DRY_RUN=true`, `AUTO_TRADE=false`.

1. **[Completed] Matched cohort economics.** Keep the read-only
   [survival SQL](docs/analysis/pump_short_survival.sql) as an auditable screen and
   extend `decision-quality-report` on one completely resolved
   episode set for `score_any`, `score_4`, and `score_6`. Separate gross return,
   entry impact, modeled exit impact, fees, conservative funding, and net return.
   Segment completed trades by venue, spread, and round-trip impact, but do not
   subtract spread twice: bid/ask VWAP impact is already measured against mid. Reuse
   one decision, entry, and exact candle path for paired full-v1, clock-only,
   initial-SL-plus-clock, and fixed-240-minute exit ablations. Report how many
   initial-stop exits would later be positive at 240 minutes and the MAE required to
   reach that result. These are discovery diagnostics; interacting deltas are not
   additive and cannot change production.
2. **[Completed, collecting] Exit-time liquidity observation.** At every paper close, fetch a
   bounded fresh order book and persist the executable buy-to-close quote: timestamp,
   best bid/ask, mid, spread, size-specific ask VWAP impact, latency, status, and
   error. Preserve the existing decision-time modeled exit impact instead of
   overwriting it. This is an observed exit quote, not an actual fill. Failure to
   fetch it must never block or erase the paper close. Ship the schema and collector
   early so observations accrue.
3. **[In progress] Prospective liquid taker candidate.** Register
   `liquid_taker_candidate_v1` from `2026-07-30T00:00:00Z`: keep the existing entry,
   score, taker execution, and full-v1 exit rules, but require the recorded
   market-quality gate and decision-time round-trip impact at the configured notional
   to be at most 20 bps. Treat Binance as a pre-declared sensitivity slice, not an
   eligibility rule. Report trade flow, capacity, net expectancy, drawdown, venue and
   weekly concentration. Promotion needs at least 100 eligible episodes, 30 asset
   clusters, four calendar weeks, complete pairing, and a positive conservative
   cluster interval. This remains shadow-only and does not change production.
4. **[Completed, collecting] Long-horizon and signed-funding research.** The resolver already stores 24-hour,
   72-hour, and 7-day outcomes. Add them as separate research rows with mature N,
   exact-venue coverage, MFE, MAE, baseline-stop survival, funding settlement count,
   signed funding cash flow, and capital occupancy. Never turn missing funding into
   zero or call every funding rate a cost or credit. Show expected concurrent
   positions before proposing a longer hold. The report pins
   `positive_rate_long_pays_short_v1`: positive rates credit a modeled short and
   negative rates debit it. Public Binance and OKX payloads were checked for raw to
   unified sign preservation on 2026-07-29. Before any live use, validate every
   enabled venue against its official contract and at least one authenticated account
   funding-ledger settlement.
   A separate prospective open-ended margin study starts on `2026-08-03`. It adds
   exact-venue 14-, 21-, and 28-day checkpoints plus a versioned 28-day funding lane.
   Its report compares observed MAE with collateral/notional buffers from 25% through
   200%. This is a no-`max_hold` research path, not an unlimited-loss production
   strategy or an exact liquidation model. It remains background measurement while
   `liquid_taker_candidate_v1` keeps the primary Phase 3 slot. At 30 exact 14-day
   paths/10 clusters/two weeks, fail early if 80% survival already needs more than
   100% collateral; the same final no-go applies at 100 exact 28-day paths/30
   clusters/four weeks. A positive boundary result may calibrate only a separately
   registered bounded fixed-risk exit. Its final positive interpretation also needs
   a point-in-time BTC-dominance and aggregate-funding regime sensitivity. See
   [the frozen contract](docs/research/open-ended-margin-v1.md).
5. **[Completed, awaiting first run] Exit discovery on the matched tradeable
   cohort.** `virtual-exit-discovery-report` compares the baseline, registered
   breakeven, no-progress, combined, and bounded-extension exits with two fixed-risk
   stop variants: 1.5x the baseline stop and 3x prior 14-bar ATR clamped to 1x-2x
   baseline. Every arm uses the same first market-quality-allowed decision, next
   complete 5-minute entry, exact venue, prior-only ATR window, and longest forward
   path. Wider stops reduce notional by `baseline_stop / effective_stop`, and the
   primary metric is risk-normalized net return. The report also shows drawdown and a
   simple 3x price-distance buffer, which is not an exchange liquidation model. This
   historical result is discovery-only and cannot promote a policy or change
   production.
6. **[Completed, insufficient evidence] Maker OHLCV upper bound.**
   `maker-entry-report` fixes one primary passive level before reading the result:
   a hypothetical post-only sell at the recorded decision-time best ask. The order
   becomes active at the first bar strictly after the decision and expires after 15
   minutes. Use complete exact-venue one-minute bars when possible and label a
   complete five-minute fallback separately. A bar crossing the limit is only a
   potential fill. Exposure starts on the following bar, so the unknown ordering
   inside the fill bar cannot create look-ahead. Unfilled orders are cash, maker
   entry slippage is zero, the optimistic maker fee is explicit, and every protective
   exit remains taker with the recorded exit-impact model. Report fill rate, missed
   baseline winners, stops within 30 minutes, bars that may have made the old limit
   marketable and therefore rejected by post-only, path coverage, costs, and net
   return including cash. This discovery result cannot prove post-only acceptance,
   queue position, partial fill, executable size, or authorize a shadow or live
   change. Preserve the original 5m taker baseline for continuity, but add a
   same-resolution 1m taker control so candle granularity is no longer hidden inside
   the maker delta. Split immediate activation-time marketability from later
   between-bar gaps, publish fixed cash sensitivities for activation rejection and
   exact touches, and report median return, cluster concentration, cluster-bootstrap
   bounds, and the result without the largest asset cluster. At the frozen
   `2026-07-29T18:42:07.816848Z` cutoff, optimistic mean net was `+0.46%`, but it fell
   to `-0.03%` when activation-marketable fills became cash and to `-0.30%` when
   exact touches also became cash. Every cluster interval crossed zero and the
   defensive result was single-cluster fragile. OBS-009 is parked. Do not tune the
   limit or timeout on this cohort and do not build the paper post-only simulator.
7. **[Registered, starts 2026-08-01] Prospective liquid-taker wider-stop shadow.**
   `liquid_taker_wider_stop_shadow_v1` reproduces the complete HYP-008 selector and
   compares the unchanged liquid-taker baseline with exactly one challenger on the
   same exact-venue path. The challenger widens only the initial stop to 1.5x and
   reduces notional to two thirds, preserving modeled initial-stop dollar risk.
   No-trigger episodes are cash for both variants and any missing input makes the
   pair unresolved. Formal inference needs the earliest prefix with at least 100
   episodes, 30 asset clusters, four UTC weeks, and complete pairing. Both the
   challenger's absolute 95% lower bound and its paired-delta lower bound must be
   positive, including busiest-week and top-five-asset exclusions. A pass creates
   only a shadow candidate and cannot change production.
8. **[Implemented, collecting] Exit quote calibration.** The read-only
   `exit-liquidity-calibration-report` keeps every closed paper short in the coverage
   denominator and compares decision-time modeled impact with a complete executable
   close-time quote. At least 30 comparable observations permit only a directional
   reading; 100 are required for a decision. The report segments by venue, exit
   reason, duration, spread, requested depth, and modeled impact, fails closed on
   identity, timestamp, notional, and visible-depth mismatches, and never presents a
   paper quote as an actual fill or realized slippage.
9. **[Parked] Conditional maker paper simulator.** OBS-009 did not survive its
   defensive sensitivity checks, so no simulator is authorized. Reconsider only
   after a fresh registered maker cohort or an independently proven executable edge.
10. **Decision checkpoint.** On `2026-08-31`, review the accumulated evidence. Formal
    promotion still requires at least 100 eligible episodes, 30 asset clusters,
    complete pairing, positive net expectancy after costs, cluster sensitivity, and
    acceptable drawdown. If the sample is smaller, explicitly choose one bounded
    extension or shelve the strategy because the practical opportunity flow is too
    low. The allowed outcomes are a registered exit-v2 shadow, maker-v2 shadow, a
    narrow liquid taker segment, or parking pump-short and moving the platform to the
    next pre-registered signal.

Reporting duplication is reduced incrementally while implementing items 1, 3, 4, and
5 through the shared `reporting`, replay, and challenger-inference modules. A separate
large report-consolidation project is not allowed to delay the strategy decision.

The LBank perpetual-history limitation is parked in
[CCXT-003](docs/tasks/ccxt/003-lbank-perpetual-ohlcv-research.md). It remains visible,
but it must not block this sequence. Cross-venue fallback stays explicitly labelled;
a future scanner-derived path is a separate provenance-aware fallback, not fake
exchange OHLCV.

### Parallel evidence lane: historical data

Historical backfill may run while a prospective cohort matures, but it does not
reorder PRs 2-10 or authorize a production change. Its purpose is to reject weak
rules faster, estimate how often a setup occurred, and generate a small
pre-registered family for forward confirmation. The optimization target is net
risk-adjusted expectancy after fees, funding, slippage, and drawdown, not trade count
or win rate in isolation.

Use these sources in provenance order:

1. **Schurfer forward data is the execution authority.** Decisions, immutable
   point-in-time features, liquidity snapshots, signal lag, paper fills, outcomes,
   and derivatives context are the only source for claims that depend on what the
   system could really see and trade at time `t`.
2. **Official CEX APIs through CCXT are the first historical source.** Backfill
   candles, public trades, mark/index/premium candles, funding, OI, long/short
   ratios, liquidations, instrument launch times, and delisting times where each
   venue genuinely supports them. Preserve exchange, market id, market type,
   contract size, timestamp bounds, pagination, gaps, and the CCXT version.
3. **Exchange-native archives and APIs fill CCXT coverage gaps.** Start with
   [Binance Public Data](https://data.binance.vision/) and
   [Bybit V5 Kline](https://bybit-exchange.github.io/docs/v5/market/kline).
   Use only documented public sources and keep raw responses or immutable file
   checksums. Never silently combine spot, mark, index, and perpetual last-price
   series.
4. **Contract-address DEX history is a separate reference dataset.** Use the
   [GeckoTerminal keyless API](https://docs.coingecko.com/docs/keyless-public-api)
   for low-volume pool discovery and OHLCV experiments. Evaluate the paid
   [CoinGecko token OHLCV endpoint](https://docs.coingecko.com/reference/token-ohlcv-token-address)
   only after a free-source coverage report shows a material gap. Key by chain and
   contract address, not ticker. Store pool, quote token, liquidity, data tier, and
   inactive-pool status.
5. **Paid vendors are a measured buy decision.** Consider Tardis, Kaiko, CoinAPI,
   or CoinGlass only after documenting which hypothesis cannot be tested with
   Schurfer, exchange-native, or keyless data and what the missing coverage costs in
   delayed learning.

Every imported dataset must record `source_kind`, `source_exchange`, `market_id`,
`market_type`, `contract_address` when available, observation interval, first/last
timestamp, gaps, fallback status, identity confidence, and a content fingerprint.
Historical discovery must explicitly report survivorship bias, delisted-market
coverage, point-in-time feature availability, and whether spread/depth/impact were
unrecoverable. A promising historical result becomes a new hypothesis and must still
pass the registered live shadow and untouched forward cohort.

### Reference chart fallback contract

The token page should prefer the exact venue and instrument that produced the pump or
paper position. If that history is unavailable, it may show a reference chart in this
order:

1. a verified same-asset perpetual on another CEX;
2. a verified same-asset spot market;
3. a scanner-derived sampled path collected after this feature ships;
4. a contract-address-matched DEX pool as a visual reference only.

Ticker equality alone is never sufficient. Cross-venue CEX matching requires trusted
instrument identity; DEX matching requires chain plus contract address. The API must
return the requested venue/instrument, actual source venue/instrument, `source_kind`,
identity confidence, and limitations. The UI must show a visible notice such as
`Reference chart: Binance perpetual. LBank OROCHI perpetual history is unavailable.`
Reference candles may support visual inspection and exploratory analysis, but they
must not be passed off as exact-venue execution data or used to reconstruct spread,
depth, impact, or fills.

## Shipped

- Foundation: monorepo, Docker Compose (Postgres and TimescaleDB, Redis, NATS),
  structured logging, trade-journal schema, web scaffold (Vite, React, shadcn,
  auth), Bybit websocket collector (Go to NATS), `make verify` quality gate.
- Pump scanner: 17 CEX perp markets via ccxt, Redis `pumps:latest`, graceful
  degradation, the `/pumps` UI, `GET /api/pumps`.
- Pump history and token detail: `pump_events` with multi-episode tracking,
  snapshots at +1h, +4h, and +24h, history APIs, token detail page (OHLCV chart,
  exchange breakdown, episodes table), Telegram notifier.
- Short-readiness analytics: cross-exchange OI (`oi_snapshots`) and funding
  snapshots, composite score (`/api/pumps/{base}/signals`, 5 components, 0 to 10),
  historical stats card.
- Execution service: `apps/execution` (Python, FastAPI, ccxt). Balance, positions,
  order placement, risk chain (trading_enabled, pnl_ready, daily_loss,
  max_positions, duplicate, max_size, margin), Redis distributed lock, signal
  trader that reads `signals:{base}` with a freshness check, paper and DRY_RUN mode.
  Dry-run market clients cover the same 17 venues as the scanner; authenticated
  trading clients are isolated from public measurement clients so account and
  position loops do not query credential-free clients.
- Safety hardening: exchange-native stop-loss (reduce-only stop-market on entry),
  durable daily PnL (`journal:pending_close` retry marker, `risk:pnl_ready` positive
  lease, idempotent `close_trade`, Postgres as the source of truth), position
  reconciliation (detect a vanished position and close it from the filled SL order).
- OHLCV robustness: BingX, MEXC, and XT futures fetchers, LBank spot fetcher,
  volume-ranked fallback, unbounded exchange fallback for old episodes, and tolerant
  parsing for inconsistent numeric/string fields. The LBank spot path was verified
  in production with BRIAN; perpetual-only OROCHI remains the known unsupported case.
- Production deploy: Hetzner, Docker Compose prod stack, Caddy, Tailscale, Postgres
  backup and a tested restore, GitHub Actions CI (lint, tests for Go, Python, TS,
  security).

---

## The plan

### Phase 0: Measurement layer (now)

Start collecting the evidence that answers "is there edge?". Non-recoverable data
comes first.

- [x] Preserve scanner ticker data quality end to end: unavailable 24h volume must
      remain nullable rather than becoming a false `$0`; retain its availability and
      source; never infer derivative quote volume without verified contract units;
      use LBank's raw `lastTime` only as a narrow freshness fallback; expose partial
      totals as lower bounds in alerts and UI.

Status (2026-07-23): the decision + liquidity + price dataset is live and durable.
Per-exchange first-discovery attribution is the remaining non-recoverable capture;
lightweight dataset-health visibility remains operational follow-up.

- [x] Durable exchange-source attribution. Store one compact row per pump episode and
      venue with immutable first-seen price/change/volume plus last-seen, peak change,
      and observation count. Report unique discoveries, overlap, and lead time by
      venue. Do not retain every raw ticker: the source crossing timestamp is the
      non-recoverable fact required to decide whether broader coverage is valuable.
- [x] Discovery-only exchange-source economics (OBS-012). Join the attribution-safe
      source cohort to the unchanged HYP-008 selector, exact selected-venue 4h/8h
      outcomes, and full-v1 exit replay. Keep first source distinct from execution
      venue; report source-to-execution routes, cash episodes, path failures, costs,
      capacity, asset-cluster intervals, Holm correction, leave-one-cluster, busiest
      week, and scanner timing. A sole-source label is only a removal counterfactual.
      This inspected family cannot alter HYP-008/HYP-010 or production. Any useful
      source-aware rule requires a separately frozen prospective shadow contract.
- [x] Add the OBS-013 source-lead paired screen. For uniquely first MEXC/Gate
      observations, compare a hypothetical Binance/Bybit long after the source with
      a control long after the later target confirmation, using the same event,
      exact target symbol, and common exit endpoint. Keep all four routes and the
      confirmation-time short lane separate. Treat later confirmation as post-hoc
      sample construction, not a live feature; venue-local identity does not prove
      canonical cross-venue token identity. A positive screen can only authorize
      prospective identity, quote, and fill measurement.
- [ ] Make CEX alert latency and peak semantics measurable before tuning scan speed.
      Preserve exchange-ticker time, scanner observation time, threshold-crossing
      time, notification time, first observed change, and highest change actually
      observed by Schurfer. Label the exchange-derived rolling value as `24h high`,
      not `peak`.
  - [x] Measurement contract: persist per-venue scanner observation time, retain the
        Redis publication time, scope notification de-duplication to the durable pump
        event, and record successful Telegram delivery with its threshold, observed
        change, venue, ticker time, scanner time, publish time, and send time. Expose
        observed peak separately from the exchange-derived rolling 24h high. Retry
        transient Postgres failures through an AOF-backed Redis outbox with an
        idempotent insert and poison-message DLQ.
  - [x] Capture the pre-optimization latency baseline for at least 72 hours and 20
        delivered pump events, whichever takes longer. Read `2026-08-04T19:20Z` over
        78 alerts spanning `2026-07-27T01:42Z`-`2026-08-04T16:55Z` (207 hours): scan-to-publish
        p50 `4.6s`/p95 `8.1s`; notifier pickup (publish to notification start) p50
        `23.7s`/p95 `55.3s`; Telegram send p95 `0.2s`; end-to-end p95 `61s`; outbox and
        DLQ both empty. Notifier pickup — bounded by `SCAN_INTERVAL=60s`, since the
        notifier only checks for new pumps once per scan tick — dominates the total by
        a wide margin; scan-to-publish and Telegram send are not bottlenecks. This
        confirms the fast-loop decoupling below is where any speed win would actually
        come from, not scanner or delivery optimization.

  - [ ] Decouple a fast Redis-only notifier loop from the broad exchange scan interval,
        then promote active candidates into a bounded 1-to-5-second hot set using
        targeted polling or websockets. Use explicit WATCH, HOT, NEW_HIGH, and RETRACE
        transitions. Do not increase whole-market REST frequency until rate-limit and
        host-load measurements support it. On `2026-08-04` the production host (2 CPU,
        3.7GB RAM) hit resource exhaustion twice in one day from ordinary ad hoc
        analytics load (see the postgres `shm_size` fix and the report-container
        memory incident) — real evidence the host-load precondition is not yet met.
        Do not start this until host capacity is addressed (bigger host, or hard
        resource limits on ad hoc report containers) and re-checked.

        **2026-08-06:** `mem_limit`/`memswap_limit`/`cpus` added to the `analytics`
        service in `docker-compose.prod.yml` (1536m/1536m/1.0, overridable via
        `ANALYTICS_MEM_LIMIT`/`ANALYTICS_CPU_LIMIT`) — this covers the
        report-container half of the precondition. Host capacity itself has not
        been re-checked or upgraded; re-verify actual headroom under a real ad hoc
        report before treating this item as unblocked.

Latency baseline verification commands:

```bash
docker exec schurfer-postgres psql -U schurfer -d schurfer -c "
SELECT
  count(*) AS alerts,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (scan_published_at-scanner_observed_at))*1000
  ) AS scan_publish_p50_ms,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (scan_published_at-scanner_observed_at))*1000
  ) AS scan_publish_p95_ms,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (notification_started_at-scan_published_at))*1000
  ) AS notifier_pickup_p50_ms,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (notification_started_at-scan_published_at))*1000
  ) AS notifier_pickup_p95_ms,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (notification_sent_at-notification_started_at))*1000
  ) AS telegram_send_p95_ms,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (scanner_observed_at-ticker_at))*1000
  ) FILTER (WHERE ticker_at IS NOT NULL) AS ticker_age_p95_ms,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (notification_sent_at-scanner_observed_at))*1000
  ) AS end_to_end_p95_ms
FROM app.pump_alert_deliveries;"

docker exec schurfer-redis redis-cli LLEN notifier:alert_delivery_outbox
docker exec schurfer-redis redis-cli LLEN notifier:alert_delivery_dlq
```

Keep both Redis lengths at zero in steady state. If notifier pickup dominates,
shorten only its Redis loop first; if scanner observation/publication dominates,
build the bounded HOT polling set; if ticker age dominates, investigate the venue
adapter. Record the baseline cutoff before deploying any speed change.

#### AKE 2026-07-27 hot-path observability case study

AKE shows that a bounded hot path could have observed useful pre-impulse features.
It is one positive retrospective case, not evidence of net long expectancy and not a
reason to rank order-flow above the primary pump-reversion lane. Negative controls
and prospective outcomes are required before its priority can rise:

- Schurfer observed an HTX episode around `2026-07-27T04:39:00Z`, about 98 minutes
  before the main impulse. It correctly rejected a trade because score was 4 and
  spread was about 108 bps, but the token should have remained a watch-only
  candidate.
- From `06:10Z` through `06:16Z`, Binance one-minute quote volume rose from about
  1.9x to 10.6x its preceding 15-minute average while price was still only about
  1% to 3.4% above the `06:10Z` open.
- During `06:17Z`, Binance moved from `0.0037087` to a high of `0.0069`. Public
  aggregate trades show roughly +8% by second 30, +19% by second 40, +36% by second
  45, and +51% by second 50. The next one-minute candle closed about 30% below the
  impulse close.
- The broad scanner created the main episode around second 48. That was sufficient
  to observe the event but too late to assume a safe momentum entry.
- OI amount was roughly flat to slightly lower before the impulse, then fell about
  34% on Binance and 21% on Bybit by `06:20Z`. Together with negative funding, this
  is a squeeze hypothesis, not proof of a repeatable long edge.
- The first post-spike short decision had score 5 and was skipped. A later secondary
  peak would have moved about 36% against that decision price, so the case must also
  remain in the score-5 versus score-6 analysis.

The measurement implementation must separate three questions:

1. **Detection:** could an executable signal have fired before +5%, +10%, or +20%?
2. **Long squeeze/momentum:** after spread, depth, slippage, fees, and exact next-ask
   entry, did a hard-stop plus 30-to-120-second exit have positive net expectancy?
3. **Short blow-off/reversal:** after the impulse, which observable reversal trigger
   avoided the first rebound and captured the later retrace?

Record a small strategy family before reading aggregate results. Candidate triggers
may combine recent-pump TTL, 1-to-10-second price acceleration, volume multiple,
cross-venue confirmation, funding sign, OI direction, and market quality. Do not
optimize all thresholds at once. Treat no trigger as cash, record rejected fills,
and compare against a no-trade baseline. Long and short variants need isolated state,
capital, risk limits, and performance reports; a profitable short rule does not
validate the long rule or vice versa.

The intended stream topology is:

1. **Broad tier:** one or a few multiplexed websocket connections per supported
   exchange for lightweight tickers. This is whole-venue observation, not one process
   or connection per token.
2. **Hot tier:** dynamic subscriptions for recent or accelerating tokens. Capture
   aggregate trades and best bid/ask continuously, plus bounded order-book depth at a
   measured cadence.
3. **Durable aggregation:** normalize in the Go collector, publish versioned events,
   consume them, and persist 1-second or 5-second aggregates. Raw L2 retention must be
   short and explicitly budgeted.
4. **Shadow evaluation:** calculate exact signal, executable quote, latency, simulated
   fill, stop, trailing state, and exit without placing an order.

- [ ] Canonical instrument identity. A ticker is a display label, not an asset key:
      exchanges can retain disabled markets or reuse symbols for unrelated tokens.
      Persist the exchange market id/type, ticker timestamp, and listing/onboard date;
      use `chain + contract_address` for spot/DEX assets and a versioned
      `exchange + market_id + onboard_date` identity for derivatives. Do not merge
      obscure cross-venue assets solely by `base`; link them through an explicit
      nullable canonical asset id and surface unverified/conflicting identities.
      The scanner already rejects stale/inactive markets and exchange-native disabled
      trading flags; this follow-up prevents fresh same-symbol collisions.
  - [x] Foundation: retain a versioned derivative identity key, exchange market id,
        unified/display symbols, market type, base/quote/settle, contract size,
        ticker time, and supported listing/onboard time on every pump source. Surface
        identity changes inside one venue/episode as conflicts instead of silently
        treating them as the same instrument
        ([recorded cases](docs/research/instrument-identity-cases.md)).
  - [ ] Add reviewed canonical assets and explicit instrument links. Prefer
        chain + contract address for spot; do not infer links such as CHECK ↔
        CHECKMATE or GME ↔ GMEROBINHOOD from names alone.
  - [ ] Collect listing, delisting, relisting, suspension, and resumption events from
        official venue archives and live market-state changes. Normalize KRW event
        prices with a timestamped FX rate and run event-time studies at 1h through
        90d before using Korean listings as trading or portfolio signals.

- [x] Extend `app.trade_decisions`. It currently stores only `score` and `pump_pct`
      as scalars, and already logs every decision including skip reasons. Add:
  - `features jsonb`: the signal snapshot plus the decision context (candidate
    exchanges and a fingerprint of the effective config, so decisions stay
    comparable across rule changes).
  - `decision_id uuid` (unique) and `strategy_version`: to stitch decision, trade,
    and post together. `decision_id` also flows into `app.trades.setup_context`.
  - Liquidity snapshot: `spread_bps` and VWAP depth impact at $100, $500, $1000 via
    `fetch_order_book` at decision time, sampled for every candidate with a
    configured exchange and stamped with an explicit status. This is the only
    non-recoverable piece, so it is the most urgent.
  - Market-quality eligibility: fail closed before an entry when the two-sided book
    cannot fill 2x the configured position cap, spread exceeds 50 bps, or bid/ask
    VWAP impact exceeds 50 bps. The verdict and effective thresholds are stored with
    every decision; `AUTO_TRADE=true` cannot start with this gate disabled, and an
    exchange-minimum round-up cannot exceed the notional already liquidity-checked.
  - Which exchange the tradeable instrument lives on (coverage data, see Phase 2).
  - Also added: `price` (decision-time reference price, migration 0010).
- Direct episode attribution: scanner persists each `pump_event` before publishing the
  Redis snapshot, and every decision stores its nullable FK `pump_event_id`. Missing or
  stale signals are operational deferrals rather than trading decisions: they do not
  enter the durable decision stream and retry after one minute, while a valid low score
  is reconsidered after one 5-minute candle.
- [x] Schema and decision-write path (migrations 0008-0012 plus the execution write
      path). This is independent of where the score is computed, so it does not commit
      us to any later scoring decision.
- [x] Run 24/7 (already deployed) plus a stale-data Telegram alert (no fresh scans or
      signals for N minutes). A silently dead scanner rots the dataset.
- [ ] Operational health on the existing Status page: pipeline liveness (scanner
      alive, last-scan age, signal freshness), per-service error rate, container
      health, and basic host resources (RAM, disk) so a memory leak or a disk filled
      with data does not kill collection silently. Keep it lightweight. This is about
      "is the dataset being collected without gaps", not a performance product.
      The execution service already publishes a short-lived Redis snapshot for every
      trader tick, and the Status page shows ready/deferred signal evaluations plus
      their reason counts. Broader service and host telemetry remains open.
- [x] Dataset completeness metrics: decisions/hour, % features present, % liquidity
      present, % liquidity fetch_failed, and lag between signal computed_at and the
      decision. The read-only `measurement-report` CLI also reports quality reasons,
      due/unresolved outcome coverage, raw return/MAE/MFE by version and horizon, and a
      configurable exchange slice. It always shows decision and distinct-episode N so
      repeated observations are not presented as independent evidence, and reports
      direct episode-FK coverage explicitly.
- [x] Durable decision queue. Moved from the in-memory writer queue to a Redis Stream
      outbox (execution XADD atomic with SET seen, DB writer XREADGROUP -> INSERT ->
      XACK+XDEL after commit, XAUTOCLAIM recovery, poison DLQ). Prod + dev Redis run
      AOF (`--appendonly yes --appendfsync everysec`) with RDB kept and noeviction.
      Guarantee and remaining opened-decision window documented in the runbook; the
      two-phase intent/resolution + reconciliation is a follow-up, required before
      `AUTO_TRADE=true`.
- Outcome capture (MAE, MFE, forward price) is backfillable from OHLCV, so we do not
  plumb it live now. The analysis that uses it is the core deliverable, see Phase 1
  "Decision-quality analysis".

### Phase 1: Research (parallel, no dependencies)

- [ ] Decision-quality analysis (automatic). This is the core deliverable: it answers
      "was our decision right, and what would have made it right?" for every token that
      hit the radar, whether we traded it or skipped it.
  - [x] Strategy-agnostic outcome layer: a separate idempotent worker backfills 5-minute
        OHLCV at +15m, +30m, +1h, +4h, +8h, +24h, +72h, and +7d, then stores forward
        price, MAE/MFE, raw short return, venue provenance, coverage, retry status, and
        resolver version. It never uses the candle in progress at decision time and
        labels cross-venue fallback rather than silently mixing it with anchor-venue
        data.
  - [x] Descriptive measurement report: versioned cohort health, quality reasons,
        outcome completeness, raw forward return/MAE/MFE, and exchange segmentation in
        Markdown/JSON. This is operational visibility, not the virtual-strategy verdict.
  - [x] Separate prospective measurement and entry floors: persist and privately
        publish candidates from +20%, compute signals and capture decision-time
        liquidity under `pump_short_measurement_v1`, but independently hard-gate the
        v1 order path at +30%. Keep `pumps:latest` and Telegram at their existing
        public thresholds so research collection does not change user-facing alerts or
        entry eligibility. Preserve both the first measurement timestamp and immutable
        first entry-qualified timestamp; after +30%, signal age, OI baseline, and
        replay cohort boundaries use the entry-qualified anchor. A `pump_event` now
        spans the +20% measurement episode. For HYP-002, repeated +30% crossings inside
        that event remain one correlated inference unit rather than inflating N; this
        rule is locked before its 2026-07-29 cohort begins.
  - [ ] Versioned virtual-strategy layer: replay decisions by token episode under the
        actual v1 rules and pre-registered challengers, including fees, funding,
        liquidity-aware slippage, TP/SL/trailing/max-hold, and taken-vs-skipped labels:
    - taken and won, or taken and lost
    - skipped and would-have-won (missed edge), or skipped and correctly avoided

    The experiment boundary is locked in
    [episode replay protocol v1](docs/research/episode-replay-protocol-v1.md): direct
    episode attribution, complete chronological paths, a 50-episode descriptive look,
    a 100-episode/30-cluster first formal cohort, cluster-bootstrap confidence
    intervals, Holm correction for challenger families, strict point-in-time features,
    and a code/data provenance manifest.
    - [x] Baseline vertical slice: deterministic one-trade-per-episode selection,
          exact-anchor 5-minute paths, production dynamic exits, conservative
          within-bar ordering, explicit fee/funding/slippage costs, taken-vs-skipped
          classifications, and a versioned Markdown/JSON manifest. Entry is modeled at
          the next complete 5-minute bar open; statistical inference and challengers
          remain separate follow-ups.
    - [x] Pre-registered entry-confirmation family: compare the baseline with red
          candle, 1.5% retrace, and combined challengers on the same eligible episodes.
          Use six fully closed 5-minute candles, a one-bar execution gap, and at most a
          60-minute wait; preserve the baseline exit and cost models. Treat no
          confirmation as a zero-return cash episode and missing path data as
          unresolved. The dedicated cohort begins at `2026-07-29T00:00:00Z`.
          Delayed variants hold the decision-time liquidity impact constant because
          their historical entry books are unrecoverable; a future live shadow cohort
          must validate actual delayed-entry execution quality. Baseline episode
          eligibility is also held constant during the wait because future score and
          market-quality gates cannot be reconstructed from the current dataset; this
          report isolates entry timing rather than claiming an end-to-end strategy
          replay.
    - [x] Formal entry-challenger inference: lock the first 100 chronological eligible
          episodes, require 30 asset clusters and complete paired resolution, resample
          whole clusters for 10,000 deterministic iterations, report 95% expectancy
          intervals, apply null-centered paired tests with Holm correction to the
          three registered challengers, require a positive conservative 98.333...%
          Bonferroni paired interval, and run leave-one-out sensitivity over the five
          most frequent clusters. Formal values are withheld before readiness; a pass
          produces only a live-shadow candidate.
    - [ ] Entry-challenger verification after merge:
      - Data sources: `app.trade_decisions` and `app.pump_events` define chronological
        episodes; `app.trade_decision_outcomes` supplies the required exact-anchor 8h
        coverage; decision `features` and `liquidity` preserve point-in-time inputs and
        costs; CCXT supplies exact-venue 5m pre-entry and exit paths at report time.
      - Deploy only analytics, then wait at least eight hours after candidate episodes
        close so `forward_v1` can resolve the required horizon:

        ```bash
        make prod-deploy-svc SERVICE=analytics
        make prod-virtual-entry-challenger-report
        ```

      - Before a formal read, choose and record an exclusive UTC cutoff without looking
        at the challenger output. Archive the JSON manifest outside Git:

        ```bash
        mkdir -p backups/reports
        make prod-virtual-entry-challenger-report \
          ARGS="--until 2026-08-03T00:00:00Z --format json" \
          > backups/reports/entry-challengers-2026-08-03.json
        ```

      - Check `eligible_episodes`, locked formal sample IDs, input exclusions,
        `completely_paired_episodes`, unresolved paths, cluster concentration, trade
        rate, mean episode net return, paired mean delta, initial-SL rate, mean wait,
        avoided losing entries, and missed baseline winners. Investigate missing
        exact-anchor paths or cost inputs instead of dropping or replacing them.
        At 50 episodes only the descriptive directional reading is available. Formal
        evaluation requires the locked first 100 episodes, at least 30 clusters,
        complete resolution, 95% cluster-bootstrap expectancy intervals, Holm-adjusted
        paired tests, positive conservative familywise paired bounds, and top-five
        cluster sensitivity. Even a passing result advances only to live shadow so
        delayed-entry spread/depth/impact can be measured at the actual confirmation.

    - [x] Pre-registered entry-floor family (HYP-003): keep +30% as the baseline and
          compare +20%, +25%, +35%, +40%, and +50% on the same prospective +20%
          measurement episodes beginning `2026-07-27T07:00:00Z`. Select the first
          recorded crossing that passes its point-in-time score and market-quality
          gates, enter at the next complete exact-venue 5-minute open, and reuse the
          baseline exit and cost engine. A floor never reached is a zero-return cash
          episode; missing decision-time data or exact paths remain unresolved.
          Different floors may select different decisions and venues inside one parent
          `pump_event_id`, but never create additional inference observations.
    - [x] Entry-floor challenger verification after merge. Read `2026-08-03T21:50:50Z`
          (`backups/reports/entry-floor-2026-08-03.json`, archived outside git):
          891 eligible episodes, 100 completely paired, 34 asset clusters — the
          formal sample gate is reached. Baseline (+30%, production) is itself
          `inconclusive` (95% CI `[-0.52%, +0.02%]`, straddles zero). None of the
          five challengers reached Holm rejection (`holm_p=1.0` for all). `+20%`
          is `no_go` (paired delta `-0.017%` versus baseline). `+35%`/`+40%`/`+50%`
          show a directionally positive paired delta (`+0.075%`) but their own
          confidence interval and the familywise paired lower bound are not
          positive, so the pass bar above (positive own expectancy, positive
          familywise paired lower bound, Holm rejection, positive top-cluster
          sensitivity) is not met by any floor. No floor change is authorized;
          `+30%` stays in production.
          **Caveat found `2026-08-04` on closer inspection**: the `formal_sample_ready`
          gate (100 eligible episodes, 34 clusters) counts eligible episodes, not
          triggered trades. At baseline's `2.92%` trigger rate, the locked 100-episode
          formal window contains only 3 triggered trades for `+30%`, 4 for `+20%`/`+25%`,
          and exactly 1 — the same single episode (`event 1008`, COTI) — for
          `+35%`/`+40%`/`+50%` each (confirmed by filtering `episode_results` to
          `inference.formal_sample_event_ids`; this is also why those three floors'
          formal point estimates are numerically identical). A `no_go`/`inconclusive`
          verdict built on 1-4 trades is not informative either way — read this as "not
          enough triggered trades yet to judge these floors," not as a confirmed
          rejection. **Fixed `2026-08-04`**: `challenger_inference.build_challenger_inference`
          now takes an optional `minimum_triggered_episodes`, requiring every
          formal-sample episode to carry `baseline_triggered`/`challenger_triggered` and
          reporting a new `insufficient_triggers` status (with `least_triggered_variant`/
          `least_triggered_count`) instead of a false `formal_sample_ready` when the
          least-triggered strategy in the family falls short. Wired into
          `virtual_threshold_challenger_report.py` at a floor of 20 triggered episodes;
          existing callers that don't opt in (`virtual_exit_policy_report.py`,
          `virtual_score_challenger_report.py`'s current usage) are unaffected by
          default. `virtual_entry_challenger_report.py` and
          `virtual_score_challenger_report.py` likely have the same latent gap — not yet
          extended there, since their trigger rates (confirmation appearing, score
          crossing 4/5) are plausibly much higher than a rare price floor and this needs
          checking per report before assuming the same fix applies.
    - [x] Add a separate discovery-only pump-magnitude surface over +20%, +30%, +50%,
          +70%, +100%, +150%, and +200%. It reuses the same point-in-time gate
          reconstruction, exact selected venue, next complete 5-minute entry,
          production exit engine, and recorded cost model, while also reporting a
          fixed 240-minute gross return that removes stop/trailing differences. The
          surface includes no-trigger cash, opportunities per calendar day, asset and
          venue concentration, gross/net expectancy, P&L, profit factor, drawdown,
          MFE/MAE, stop rate, duration, and cost decomposition. It is not a
          retrospective extension of HYP-003 and cannot choose a production floor.
          The default starts at `2026-07-27T07:00:00Z`, after the measurement split;
          older episodes are excluded because their missing higher-floor decisions
          are indistinguishable from genuine no-trigger cash.
          Run and archive it with:

          ```bash
          make prod-pump-magnitude-report
          make prod-pump-magnitude-report \
            ARGS="--until 2026-08-05T00:00:00Z --format json" \
            > backups/reports/pump-magnitude-2026-08-05.json
          ```

          A promising magnitude region must be converted into one separately frozen
          prospective contract. Do not promote the best historical row directly.

    - [x] Pre-register and implement the HYP-006 score-threshold family. Keep score 6
          as baseline and compare score 4 and 5 on the untouched
          `2026-07-31T00:00:00Z` cohort. Select each policy's first recorded
          point-in-time score crossing that passes its recorded market-quality gate.
          A never-triggered policy is cash. Every policy reuses the exact selected
          venue, next complete 5-minute entry, baseline exit, and locked cost model.
          Score 7 and 8 remain reserved for isolated live-shadow state so censoring
          cannot make this formal family impossible to complete.
    - [ ] Score-threshold verification after merge:
      - Deploy analytics only after the registered cohort begins. Wait until candidate
        episodes close and their exact-anchor 8-hour outcomes resolve:

        ```bash
        make prod-deploy-svc SERVICE=analytics
        make prod-virtual-score-challenger-report
        ```

      - Before a formal read, choose an exclusive UTC cutoff without inspecting the
        score comparison and archive its JSON manifest:

        ```bash
        mkdir -p backups/reports
        make prod-virtual-score-challenger-report \
          ARGS="--until 2026-08-10T00:00:00Z --format json" \
          > backups/reports/score-thresholds-2026-08-10.json
        ```

      - Check exclusions, exact selected-decision paths, no-trigger cash, cluster
        concentration, trade rate, episode and
        conditional-trade net expectancy, profit factor, drawdown, initial stops,
        captured MFE, and paired deltas versus score 6. Formal output remains hidden
        before the first 100 episodes are fully paired across at least 30 clusters.
        A passing policy becomes only a live-shadow candidate and cannot change
        production `SCORE_THRESHOLD`.

    - [x] Pre-register and implement the banded price-extent hypothesis
          (2026-08-05). Informal reads across the entry-floor and decision-quality
          reports both showed a worse short win rate at both smaller (20-25%) and
          much larger (35%+) pre-entry pump magnitude than at the 30% baseline
          floor — a "sweet spot" shape, not the straight line the live
          `price_extent` score component assumes (it currently grants its maximum
          points to the LARGEST move, >100%). `score_6_with_banded_price_extent`
          (`decision_quality.py`) recomputes only that one component from its own
          already-recorded raw value: 2 points in [25, 40)%, 1 point in [15, 25) or
          [40, 60)%, 0 otherwise. Everything else about the decision is unchanged.
          A first attempt registered this challenger inside the general-purpose,
          full-history `decision_quality_report.py` discovery tool — reviewed and
          rejected before merge: that report's default cohort starts
          `2026-07-26T00:00:00Z`, which overlaps the exact window used to invent
          the bands, so any read from it would validate the hypothesis on the data
          it was fitted to. Corrected to a dedicated formal report,
          `virtual_banded_price_extent_report.py`, with its own report/inference
          version, a cohort locked to `2026-08-06T00:00:00Z` (the day after
          registration, enforced by exact-match, not merely "not earlier"), and a
          manifest that records the exact band boundaries and points instead of a
          code comment. Never widen this cohort backward to reach a faster read.
    - [ ] Banded price-extent verification after merge:
      - Deploy analytics only after the registered cohort begins. Wait until
        candidate episodes close and their exact-anchor 8-hour outcomes resolve:

        ```bash
        make prod-deploy-svc SERVICE=analytics
        make prod-virtual-banded-price-extent-report
        ```

      - Before a formal read, choose an exclusive UTC cutoff without inspecting
        the comparison and archive its JSON manifest:

        ```bash
        mkdir -p backups/reports
        make prod-virtual-banded-price-extent-report \
          ARGS="--until <chosen-cutoff> --format json" \
          > backups/reports/banded-price-extent-<chosen-cutoff-date>.json
        ```

      - Check exclusions, exact selected-decision paths, no-trigger cash, cluster
        concentration, trade rate, episode and conditional-trade net expectancy,
        profit factor, drawdown, initial stops, captured MFE, and the paired delta
        versus score 6. Formal output remains hidden before the first 100
        episodes are fully paired across at least 30 clusters. A passing
        challenger becomes only a live-shadow candidate and cannot change
        production scoring.

    - [x] Pre-registered exit-policy family (OBS-001): compare the production clock
          with breakeven-after-activation, no-progress timeout, their combination,
          and one recent-progress bounded extension on the same point-in-time decision
          and next complete 5-minute entry. Reuse the locked fee, funding, liquidity,
          and within-bar models. Require the complete longest registered candle window
          for every member of the paired family. The dedicated cohort begins at
          `2026-07-29T00:00:00Z`.
    - [x] Formal exit-policy report: emit versioned Markdown/JSON manifests, descriptive
          expectancy, recorded-size P&L, profit factor, sequential episode drawdown,
          exit reasons, duration, MFE/MAE, captured move, initial/protected stops, and
          paired deltas. Reuse the generic first-100 episode, 30-cluster, 10,000-iteration
          inference engine with Holm correction, conservative Bonferroni bounds, and
          top-cluster sensitivity. A passing policy is only a live-shadow candidate.
    - [ ] Exit-policy verification after merge:
      - Deploy analytics only after the registered cohort begins. Wait until candidate
        episodes have closed and their exact-anchor 8-hour outcomes are resolved:

        ```bash
        make prod-deploy-svc SERVICE=analytics
        make prod-virtual-exit-policy-report
        ```

      - Before a formal read, choose an exclusive UTC cutoff without looking at the
        policy output. Archive the reproducible JSON manifest outside Git:

        ```bash
        mkdir -p backups/reports
        make prod-virtual-exit-policy-report \
          ARGS="--until 2026-08-10T00:00:00Z --format json" \
          > backups/reports/exit-policies-2026-08-10.json
        ```

      - Check input exclusions, unresolved family paths, complete pairing, cluster
        concentration, net expectancy, profit factor, drawdown, initial-stop rate,
        protected-stop rate, exit-reason changes, duration delta, captured MFE, and
        paired improvement versus baseline. Investigate missing paths instead of
        shortening a challenger window. Formal output requires the locked first 100
        eligible episodes and 30 clusters. Do not change production exits from a
        discovery or directional result.

  - [x] Derive recoverable pre-decision candle features (HYP-005) from fully closed
        exact-venue 5-minute OHLCV. The registered `candle_anomaly_features_v1`
        contract uses a 24-hour formation window with four hours of warm-up,
        prior-only ATR and volume baselines, top-two positive-move concentration,
        bullish body/range/wick expansion, final bearish body, and returned-pump
        share. One shared path supplies both pre-decision features and the locked
        baseline virtual exit replay. The Markdown/JSON report groups episodes into
        the four pre-registered blow-off/reversal buckets and reports coverage,
        cluster concentration, net return, MFE/MAE, captured move, and initial-stop
        rate. It is descriptive only and cannot alter production scoring or entry.
  - [ ] Candle anomaly verification after merge:
    - Data sources: `app.trade_decisions` and `app.pump_events` define the selected
      baseline episode decision; `app.trade_decision_outcomes` provides exact-anchor
      8-hour eligibility; CCXT supplies the combined exact-venue 5-minute feature and
      exit path at report time. The prospective cohort begins at
      `2026-07-29T00:00:00Z`.
    - Deploy analytics only, wait at least eight hours after candidate episodes close,
      then inspect the descriptive report:

      ```bash
      make prod-deploy-svc SERVICE=analytics
      make prod-candle-anomaly-report
      ```

    - Before comparing buckets, choose an exclusive UTC cutoff without looking at the
      output and archive the JSON manifest outside Git:

      ```bash
      mkdir -p backups/reports
      make prod-candle-anomaly-report \
        ARGS="--until 2026-08-05T00:00:00Z --format json" \
        > backups/reports/candle-anomalies-2026-08-05.json
      ```

    - Check input exclusions, exact-path and feature coverage, partial/missing volume,
      all four registered buckets, largest-cluster share, net return, MFE/MAE,
      captured move, and initial-stop rate. Investigate missing paths rather than
      replacing venues. A useful split only becomes a hypothesis for a separately
      registered out-of-sample live-shadow cohort; do not tune the thresholds or
      production strategy from this descriptive report.

  - [x] Establish a bounded, read-only derivatives-context coverage probe for CCXT
        funding-rate history, open-interest history, mark/index/premium-index candles,
        long/short ratios, and public liquidations. It selects one recent completed
        exact-symbol target per exchange, reuses one rate-limited client per venue,
        records declared support separately from sampled timestamped coverage, fails
        closed on identity/parser/response errors, and emits versioned Markdown/JSON
        provenance without modifying the database or execution:

        ```bash
        make prod-deploy-svc SERVICE=analytics
        make prod-derivatives-context-report
        ```

        The exact data sources, limits, statuses, archive command, and interpretation
        checklist live in `docs/runbooks/README.md`.

  - [x] Harden the probe after the first production run on 2026-07-27. The v1 report
        tested 119 exchange/method pairs, selected 11 venue targets, and observed 30
        sampled results, but also showed that one successful page did not prove a
        complete regular series: OKX mark/index stopped at the venue's 100-row page
        cap, OKX long/short covered only part of the window, and HTX OI rejected the
        generic 5-minute timeframe. Probe v2 pins CCXT 4.5.68, paginates with bounded
        forward progress, distinguishes incomplete/window-mismatched data, reports
        row/gap/boundary coverage, and registers the HTX OI `1h` override explicitly.
        Funding and liquidation histories remain event series without a fabricated
        expected cadence. Re-run and archive v2 before selecting persistence adapters.

  - [x] Persist recoverable high-value derivatives context for each pump episode.
        The existing outcome-resolver process now drains a bounded, retryable work
        queue after the eight-hour forward window matures and writes versioned run
        diagnostics plus idempotent public CCXT samples. The initial evidence-based
        allowlist covers funding, OI, Binance long/short ratios, and HTX liquidations;
        mark/index/premium OHLCV remains recoverable on demand instead of being
        duplicated into Postgres. HTX funding and liquidations use the documented
        100-row request cap while the generic caller bound remains 200. Selection
        fails closed on missing market id/identity key, recorded conflicts, or a
        mismatch between recorded and currently loaded market identity. It starts
        from the locked `2026-07-27T00:00:00Z` cohort and records exact venue, market,
        method, CCXT/resolver version, request policy, status, coverage, attempts,
        errors, source timestamps, and payloads. Never replace the live decision
        snapshot with a historical approximation: exact order-book liquidity, signal
        lag, and finer-grained live OI remain non-recoverable, while historical
        endpoints have venue-specific retention and may exclude delisted instruments.
        Keep normalized identity, provenance, coverage, and quality contracts
        extraction-ready for the separate public market-events project, but do not
        introduce a runtime dependency between the repositories
        ([ADR-0009](docs/adr/0009-separate-public-market-events-project.md)).
  - [ ] Add episode-clustered statistical inference to the report. Bootstrap whole
        pump episodes rather than correlated decisions, report confidence intervals,
        and use market-adjusted/cluster-robust models before promoting an apparent
        funding, OI, listing, or exchange effect.

    Then aggregate. Expectancy of taken versus skipped by score bucket answers "is
    the threshold in the right place?" (if score-5 skips beat score-6 trades, it is
    not). Feature-level separation (which feature cut best splits winners from losers)
    is the automatic "what should we have done". Evaluate against the actual
    `strategy_version`, and allow sweeping a few exit variants. Notes: virtual fills
    for old decisions use a crude slippage assumption, while decisions made after the
    liquidity snapshot ships get realistic fills; treat vanished OHLCV (delisted
    tokens) as "outcome unknown", which is itself a delisting-short signal.

- [ ] DEX narrative radar (shadow-only research track). Measure whether unofficial
      tokens created around major company, IPO, listing, or news events contain a
      tradeable signal. This is a separate strategy and dataset from the CEX
      pump-short model; no wallet or automatic execution is part of the first
      version.
  - [ ] Start with Solana and Base. Discover new contracts from point-in-time feeds,
        initially using the
        [Birdeye new-listing API](https://docs.birdeye.so/reference/get-defi-v2-tokens-new_listing)
        within its free allowance and the
        [DEX Screener API](https://docs.dexscreener.com/api/reference) for pair
        enrichment. Identify assets by `chain + contract + pair`; names and tickers
        are narrative features, never identity keys.
  - [ ] Persist every eligible listing from discovery, not only later top gainers.
        Record source/event provenance, pair age, price, liquidity, FDV, transaction
        and unique-trader flow, buy/sell volume, holder/deployer concentration,
        contract authorities, security/sell-simulation verdicts, executable quote,
        estimated price impact, and data-source timestamps. Retain explicit missing
        and unsupported statuses.
  - [ ] Resolve point-in-time outcomes at short launch horizons and through 24 hours:
        executable return after fees/slippage, MFE/MAE, liquidity drawdown, rug or
        sell-failure status, and time to peak. Treat removed liquidity and untradeable
        exits as losses rather than silently dropping them.
  - [ ] Pre-register a small family of hypotheses before reading results: narrative
        match alone, minimum-liquidity/organic-flow filters, first pullback plus
        renewed acceleration, and later CEX-perpetual shortability. Top-gainer tables
        are discovery examples only because they contain survivorship and
        non-executable-price bias.
  - [ ] Run shadow collection first, then quote-based paper execution with no wallet.
        Consider an isolated tiny-capital experiment only after an out-of-sample
        cohort shows positive net expectancy, acceptable liquidity-loss tail risk,
        and reproducible results under a versioned manifest.

- [ ] On-chain intelligence and temporal wallet graph (parked, shadow-only research
      track). Build this as a source-neutral measurement system, not a wallet-copying
      bot. The detailed scope, data contracts, graph model, public/private boundary,
      resource limits, and promotion gates live in
      [the on-chain intelligence research plan](docs/research/onchain-intelligence-roadmap.md).
  - [ ] Start with a bounded Solana pilot over a curated watchlist and direct
        RPC/WebSocket observations. External RPC providers are transport; transaction
        decoding, provenance, point-in-time labels, scoring, outcomes, and signals
        remain our code. Do not attempt a full-chain firehose on the current 4 GB
        production host.
  - [ ] Normalize transfers, swaps, liquidity changes, deployer activity, CEX and
        bridge flows into a finality-aware event envelope with occurred, observed,
        ingested, and finalized times. Handle duplicates, reconnect backfill, and
        reorg or rollback tombstones before treating the stream as research data.
  - [ ] Project normalized events into a temporal wallet, token, pool, protocol, and
        entity graph. Keep evidence, confidence, and validity intervals for every
        entity label. Start with PostgreSQL and offline graph analysis; add a graph
        database only after a measured query or scale requirement.
  - [ ] Score wallets strictly point in time using only prior realized outcomes,
        sample size, hit rate, drawdown, concentration, holding time, entry timing,
        wallet age, and label confidence. A later profitable trade must never improve
        an earlier wallet score.
  - [ ] Measure coordinated accumulation before price, early DEX flow, smart exits,
        CEX deposits, liquidity removal, and recurring deployer or wallet clusters.
        Every alert must include price already moved, executable liquidity, estimated
        impact, source latency, and independent-wallet concentration.
  - [ ] Resolve forward outcomes and edge decay at 1m, 5m, 15m, 1h, 4h, and 24h.
        Run shadow alerts first. Wallet activity is a feature, not an order. No paper
        execution until the signal survives costs, latency, adverse selection,
        failed-exit penalties, cluster concentration, and an untouched forward
        cohort.
  - [ ] Keep generic event contracts, decoders, conformance fixtures, and graph
        projections eligible for a separate public open-source package. Keep curated
        wallet lists, private labels, scores, thresholds, raw licensed datasets, and
        strategy output private. Reuse the extraction discipline from
        [ADR-0009](docs/adr/0009-separate-public-market-events-project.md) without
        creating a runtime dependency on Schurfer.

- [ ] Backtest v0 for pump-shorts and delisting-shorts, with explicit blind spots
      (survivorship, look-ahead, no historical spreads). The output is an estimate
      with bounds, not a verdict. Delisting-shorts especially: known catalyst, clean
      public archives, no survivorship (the delisting list is the universe).
- [ ] Pre-register success criteria before running: net expectancy, profit factor,
      max drawdown, MAE and MFE, confidence interval, and the definition of "backtest
      converged with forward". Not just win rate.
- [ ] CI hardening: add `gitleaks` (secret scan on every PR) and wire the existing
      `make security` (pip-audit, govulncheck, pnpm audit) into CI as a gate.
- [ ] git-history secret audit (gitleaks or trufflehog over the full history). Cheap
      now (about 150 commits, no forks). Rotate anything it finds.
- [ ] Pre-live host/database hardening gate: patch and reboot the host, verify firewall
      and loopback-only PostgreSQL exposure, split migration/app/read-only DB roles,
      enforce private backup permissions plus encrypted offsite copies, test restore,
      and use withdrawal-disabled/IP-restricted exchange keys. Required before
      `AUTO_TRADE=true`, not a blocker for the current non-sensitive measurement phase.
- [ ] `make export` to parquet slices of episodes and snapshots (the interface to
      research work).

### Phase 2: Scaling and architecture (by touch, not big-bang)

- [x] Broaden the scanner from 12 to 17 configured perp venues. Quality remains more
      important than count: each adapter is a parse surface that can silently poison
      the dataset. The exchange-source report now decides which venues earn retention;
      do not continue blindly toward a long tail of 40.
- [ ] Korean spot observer, only after exchange-source measurement and the core
      episode replay. Collect public Upbit/Bithumb ticker, trade, and order-book data;
      normalize KRW with timestamped FX; retain both market-wide and token-specific
      kimchi-premium features. Test them first as virtual global-perp entry/exit
      challengers. Direct cross-border arbitrage remains gated on measured net edge,
      lawful Korean account access, transfer constraints, fees, and tax review.
- [x] Build the bounded Bybit public-trades pilot before any multi-venue
      microstructure platform. It observes every active linear perpetual from process
      start, aggregates sparse non-empty one-second buckets in a dedicated Go process,
      and stores only event and matched-control windows. Raw trades do not traverse
      NATS. The optional Compose profile has hard `384 MiB` memory, `0.75 CPU`, bounded
      queue, pending-record, active-event, retention, and `5 GiB` disk limits.
- [ ] Run the staged Bybit public-trades trial and decide whether the lane earns
      expansion. Observe every active linear perpetual from process start so pre-pump
      windows are not left-censored. The first 30-minute, 6-hour, and 24-hour runs
      measure actual events/s, CPU, RAM, bytes/day, compression, gaps, lag, and drops.
      Persist only sparse non-empty 1-second buckets; derive coarser rollups in
      analysis rather than duplicating them on disk until their value is proven.
      Include matched non-pump
      controls by time, liquidity, volatility, listing age, and market regime.
      Pre-register separate readings for early-long timing, squeeze avoidance, and
      delayed short entry; do not combine those books into one headline.
- [x] Freeze OBS-011 and add a streaming, read-only report over the bounded
      event/control files. The report validates the capture contract and
      activation boundary, fingerprints inputs, separates the three lanes,
      and withholds interpretation until 100 complete captures, 30 bases, and
      7 UTC market days.
- [ ] Gate all broader order-flow work on the Bybit pilot. Require useful lead time
      before the current pump trigger, point-in-time predictive lift, economic value
      after costs, more than one asset cluster and market day, and an out-of-sample
      check. If it fails, stop the lane. If it passes, add Parquet+Zstd event windows
      with checksum manifests, then replicate on Binance. Cross-venue identity,
      dynamic L2 capture, and additional venues remain later conditional steps.
- [ ] Collector to websocket data layer. The Bybit collector is the seed of the
      intended Go hot-path layer. It subscribes to all Bybit linear ticker topics in
      chunks of up to 200. On 2026-07-28 the collector and NATS had each moved
      hundreds of GB over ten days without improving scanner latency. The first
      consumer slice now retains only bounded measurement-feed hot symbols and
      records event-rate, lag, drops, and persistence errors. Validate its production
      budget before adding Binance, acceleration promotion, trades, or order-book
      depth. Reuse per-exchange connection pools and migrate only proven detection
      paths from polling. Keep ARCHITECTURE.md honest about this.
- [ ] Hot-path host budget and upgrade gate. The 4 GB production host baseline on
      2026-07-28 was load `0.52`, about `1.1 GB` available RAM, no swap, no OOM kills,
      and zero container restarts. The largest services were analytics at about
      `710 MB`, outcome-resolver at `471 MB`, and execution at `464 MB`. This is enough
      for one bounded Go consumer and aggregated hot data, but not for raw trades and
      L2 books across every symbol and 17 venues. Before rollout, add per-stage event
      rates, consumer lag, dropped-message count, DB batch latency, retained bytes per
      hour, and explicit container memory budgets. Upgrade to 8 GB or split the data
      worker when available RAM stays below `750 MB`, host memory exceeds 80% for 15
      minutes, any OOM/restart occurs, or consumer/DB lag breaches the registered
      threshold. Use 16 GB only if broad raw history or research workloads are kept
      on the production host; prefer separating those workloads instead. On
      2026-07-30 a full CCXT pump-magnitude replay reached about `1 GB` RSS and was
      killed by the host OOM policy while the live services were using most of the
      remaining memory. Before the Bybit public-trades trial, add a `2 GB` low-
      swappiness emergency swap file and finish streaming/bounded replay reads. Swap
      is crash protection, not report capacity. Heavy replay stays off the live host
      unless its memory preflight passes. The pilot starts on the existing two vCPU
      host with a hard container memory/CPU budget and staged 30-minute, 6-hour, and
      24-hour canaries. Buy or split compute only after measured lag or drops show
      that the bounded process cannot keep up.
- [x] Lightweight authenticated Status observability. Report real interval CPU
      utilization separately from load pressure, memory, swap, root-filesystem use,
      uptime, ticker event rate, hot/observed symbols, lag, drops, persistence
      errors, bounded order-flow trial health, and sanitized per-container CPU,
      memory, PIDs, health, and restarts through the existing health WebSocket. A
      host-side systemd collector writes an atomic snapshot that the API mounts
      read-only; the API and Web containers never receive the Docker socket. Keep a
      client-side rolling 60-minute CPU/memory peak while the Status page is open.
      Add heavy observability only after a second host or a proven load need.
- [x] Lightweight authenticated research-readiness dashboard. Expose exact
      exit-quote calibration counts, mature database-input proxies for HYP-008 and
      HYP-010, and bounded order-flow operational progress without running CCXT or
      replay in an HTTP request. Label every proxy and estimate explicitly; formal
      strategy output remains in the frozen reports. Use the shared page-width and
      spacing shell across the authenticated frontend. The 2026-07-31 eligibility
      correction ignores only explicitly marked `pump_short_measurement_v1`
      observation rows, while unexpected strategy versions still fail closed. Show
      closed candidates, ignored observation rows, and remaining input flags so a
      zero mature count cannot hide a scope failure again. Successful production
      HYP-008/HYP-010 reports append only bounded metadata to
      `app.research_report_runs`; the dashboard shows the latest cutoff, revision,
      fingerprint, sample diversity, status, and verdict without storing full market
      paths or episode payloads in Postgres.
- [ ] Deferred incident alerts, kept outside the evidence-producing PR budget. Add
      an external outbound heartbeat with Telegram down/recovery notification so it
      still works when the private Tailscale-only host is unreachable. When the
      notifier or Status health is next touched for product work, add deduplicated
      warning/recovery alerts for sustained host memory, swap activity, disk use,
      OOM/restart evidence, market-pipeline lag, and dropped events. Also rename the
      Status page already separates load pressure from real CPU utilization as of
      2026-07-30. Do not delay the Bybit order-flow pilot or the HYP-008/HYP-010
      decision for the remaining external alert work.
- [ ] Multi-venue execution, driven by coverage data and not by diversification. A
      signal fires on a token whose perp may only exist on certain venues, some
      blocked for Poland residents. After Phase 0 data we will know which accounts we
      actually need (for example "60% of score >= 6 signals are only tradeable on
      MEXC or Gate") instead of connecting everything blindly.
- [ ] Scoring stays in Go. No migration (decided 2026-07-19). It works and is tested,
      and a rewrite adds zero functionality. When the backtest needs parity, port the
      roughly 80-line pure scorer to Python as the backtest engine and lock both to
      identical output with a golden-vector conformance test. Parity does not require
      a single implementation. Delete the Go version only if it ever becomes a
      maintenance burden, which may be never.
- [ ] Move the notifier into a core module only when the Telegram logic is next
      touched.
- [ ] Heavy observability (Grafana, Prometheus, node_exporter, per-service p95
      latency). Only here, when there is more than one box or real load. The
      lightweight Status-page health from Phase 0 is enough until then.

### Open-source upstream workstream (non-blocking)

Upstream compatibility fixes reduce Schurfer-specific code, but they do not outrank
measurement, replay, or production reliability. The executable task set lives in
[docs/tasks/ccxt/](docs/tasks/ccxt/README.md).

- [ ] Research, implement, test, and upstream XT `fetchOpenInterest` as one atomic
      CCXT task. CCXT already declares XT's public linear/inverse open-interest
      endpoint but advertises the unified capability as unsupported; Schurfer's
      production fallback proves the linear endpoint and USD-value mapping work.
      Verify amount units, timestamp encoding, error shapes, and inverse behavior,
      then submit a TypeScript-only XT PR with static request/response fixtures
      ([CCXT-001](docs/tasks/ccxt/001-xt-fetch-open-interest.md)).
- [ ] After a released CCXT version contains the method, upgrade Schurfer, compare
      units against the current production fallback, preserve application-level
      freshness checks, and only then delete the raw XT adapter
      ([CCXT-002](docs/tasks/ccxt/002-adopt-upstream-xt.md)).
- [ ] Research LBank perpetual historical OHLCV as a separate exchange task. Submit
      an upstream proposal only if an official, public, unsigned endpoint exists;
      BRIAN confirms the documented spot endpoint works, while perpetual-only OROCHI
      confirms spot fallback is insufficient. If no supported contract-history
      endpoint exists, use durable scanner-derived candles inside Schurfer
      ([CCXT-003](docs/tasks/ccxt/003-lbank-perpetual-ohlcv-research.md)).
- [x] Upstream LBank swap ticker timestamp normalization as a small independent
      parser PR. The public contract response exposes second-based `lastTime`, while
      CCXT 4.5.58 leaves unified `timestamp` empty. The current PyPI 4.5.68 artifact
      was built before the merged parser change despite carrying the same upstream
      version number, so verify and adopt the first later release that contains it.
      Keep exchange-reported zero volume unchanged upstream; Schurfer owns the
      nullable/unavailable presentation policy. Merged as
      [ccxt/ccxt#29303](https://github.com/ccxt/ccxt/pull/29303)
      ([CCXT-004](docs/tasks/ccxt/004-lbank-swap-ticker-timestamp.md)).
- [x] Restore CCXT's development Docker image on Apple Silicon without mixing in
      unrelated cleanup. The focused fix replaces the x64-only .NET package feed,
      updates the stale editable Python install, and validates both ARM64 and AMD64.
      Merged as
      [ccxt/ccxt#29305](https://github.com/ccxt/ccxt/pull/29305)
      ([CCXT-005](docs/tasks/ccxt/005-apple-silicon-development-image.md)).
- [ ] After the Apple Silicon correctness fix is resolved, measure image size, cold
      and warm build time, and layer composition before proposing any Docker
      optimization. Submit only focused changes with repeatable before-and-after
      evidence
      ([CCXT-006](docs/tasks/ccxt/006-docker-image-optimization-research.md)).
- [ ] Research .NET installer reproducibility and integrity separately from Docker
      performance. Pin an SDK patch, verify the installer, or use an official image
      stage only if the change improves the current threat model without silently
      freezing security updates
      ([CCXT-007](docs/tasks/ccxt/007-dotnet-installer-hardening-research.md)).
- [ ] Reproduce LBank swap `fetchTrades` invalid-pair failures against current
      `master`. Propose a focused routing/parser fix only if an official public
      contract-trades endpoint provides stable unified fields; otherwise record the
      exchange limitation
      ([CCXT-008](docs/tasks/ccxt/008-lbank-swap-trades-research.md)).
- [ ] Reproduce and upstream HTX derivatives-history limit handling. Production
      evidence shows that funding and liquidation history fail with `limit=200` and
      both succeed with `limit=100`; verify the official contracts and current
      `master`, then propose a focused clamp or local validation without blocking
      Schurfer's own request policy
      ([CCXT-009](docs/tasks/ccxt/009-htx-derivatives-history-limits.md)).
- [ ] Research three lower-confidence conformance findings before calling them CCXT
      bugs: HTX index-OHLCV support by market subtype, OKX long/short history ignoring
      an older requested window, and symbol-specific empty histories on Bybit, Gate,
      and Bitget
      ([CCXT-010](docs/tasks/ccxt/010-htx-index-ohlcv-capability.md),
      [CCXT-011](docs/tasks/ccxt/011-okx-long-short-history-window.md),
      [CCXT-012](docs/tasks/ccxt/012-derivatives-empty-history-conformance.md)).
- [ ] Verify the Bybit unified open-interest window contract against current CCXT
      `master`. Production evidence shows that a request with only `since` returned a
      moving 200-row latest tail, while the adapter also supports an explicit unified
      `until` bound. Determine whether the upstream change is documentation, a
      conformance test, or adapter behavior before opening an issue
      ([CCXT-013](docs/tasks/ccxt/013-bybit-open-interest-window-contract.md)).

### Phase 3: Live ladder (gated on proven edge)

Shadow, then a Telegram button for human-in-the-loop, then auto with a report, then
auto.

- [x] Add a forward-only Gate source-lead capture before registering any early-long
      or four-hour hold contract. Persist the complete Gate denominator, exact
      first-source ties/exclusions, process-start left-censoring, sequential
      Binance/Bybit target attempts, onboarding metadata, bounded $50 executable
      quotes, four timestamp roles, and failure provenance. Network capture is
      isolated in one bounded worker so scanner cadence is not coupled to CCXT
      availability. Base-symbol matching is explicitly provisional and cannot
      authorize trading
      ([source-lead-prospective-capture-v1.md](docs/research/source-lead-prospective-capture-v1.md)).
- [ ] After a healthy production deployment, freeze the next clean UTC boundary as
      the cohort start for `gate_source_lead_4h_v1`. Before registration, add a
      versioned canonical identity approval and one deterministic point-in-time
      Binance/Bybit venue selector. Do not reuse the historical OBS-013 window.
  - [x] Add the fail-closed qualification foundation without claiming that canonical
        links already exist: packaged reviewed registry validation, append-only
        qualification rows, exact-identity matching, complete $50 two-sided-depth
        eligibility, and deterministic minimum round-trip-impact venue selection.
        The initial registry is deliberately empty; populate and independently
        review it before choosing a strategy cohort cutoff
        ([source-lead-qualified-capture-v1.md](docs/research/source-lead-qualified-capture-v1.md)).
  - [x] Add an auditable point-in-time identity review queue before populating the
        registry: exact Gate and target identity versions, executable two-sided $50
        route evidence, collision diagnostics, deterministic input fingerprint, a
        deliberately non-loadable unapproved registry skeleton, and continuous
        authenticated UI visibility. Equal tickers still cannot create approval.
  - [x] Alert once on source-lead captures stale for ten minutes or abandoned in the
        last 24 hours, recover once, and filter the detailed production health query
        at the explicit operational cohort cutoff by default.
- [x] Expose the `2026-08-02T00:00:00Z` source-lead forward cutoff and exact capture
      readiness on the authenticated Research page: full denominator, source and
      target eligibility, mature four-hour windows, clusters/weeks, one-hour
      confirmation count, Binance/Bybit quote latency, spread, $50 entry impact,
      stale collection, abandonment, and report-registry state. This is operational
      observability only and cannot issue a strategy verdict.

- Count eligible signals, not any decision. "50 signals" is meaningless when the
  split is 288 skipped / 1 opened. An eligible signal is one that passed the score
  gate and was a real trade candidate (taken, or a shadow entry). Thresholds:
  - 50 eligible shadow entries: first interim analysis only.
  - 100 to 200 labeled eligible cases plus a confidence interval: the basis for
    discussing a minimal live start.
  - A separate minimum per key score bucket, so no bucket is decided on a handful.
- Gate 1 to 2: backtest and forward results converge on the pre-registered criteria
  (measured on eligible signals, per the counts above).
- Gate 2 to 3: 20 to 30 button-approved trades with zero "I do not want to confirm
  this".
- Gate 3 to 4: a month at stage 3 with no interventions.
- Before any live money (execution checklist): a dedicated subaccount with limited
  capital, API keys with no withdrawal permission and an IP allowlist bound to the
  server egress IP, trade scope only, exchange-native SL on every position,
  idempotent orders (clientOrderId), startup reconciliation, a heartbeat alert, and
  durable daily limits (both loss and trade count).

### Phase 4: Portfolio and audience (parallel, months 2 to 5)

- [ ] Incubate a separate public exchange-market-events project after the internal
      event schema and collector survive production use. Its scope is public,
      strategy-neutral data: listing/delisting/relisting/suspension events, versioned
      exchange instruments, source provenance, coverage diagnostics, and reproducible
      event-study tooling. Schurfer remains private and consumes versioned public
      artifacts through an explicit boundary
      ([ADR-0009](docs/adr/0009-separate-public-market-events-project.md)).
- [ ] Publish a useful read-only site from that separate project: searchable event
      timeline, cross-venue availability, data-quality status, and delayed aggregate
      outcomes at 1h through 90d. Do not publish private decisions, live thresholds,
      account data, exchange keys, production topology, or a direct connection to the
      Schurfer database.
- [ ] A public shadow track record. Start it now while in shadow. A track record
      begun at "edge proven" looks like it started after a lucky streak. One begun in
      shadow is honest by construction. Append only, marked SHADOW or LIVE, never
      delete losing signals, show drawdown, and do not mix strategy versions.
- [ ] A public read-only demo. Separate deploy, read-only DB user, delayed data, no
      account routes. Blast radius is separated by infrastructure, not by code.
- [ ] A research long-read from the backtest (distributions public, live thresholds
      not).
- [ ] Source-availability decision after the backtest. Narrow or capacity-bound edge
      means private. Wide edge or no edge means open (the audit is already done in
      Phase 1). Source-available license, not MIT.

### Phase 5: Monetization (months 4 to 12, gated)

Free content, then a paid channel tier at 300 to 500 free subscribers (lawyer
consult before charging, sell "analytics access" with no return promises), then a
B2B data API (cleanest legally), then an aged-dataset Kaggle sample as marketing.
Never: executing trades for others, holding others' keys or funds, or a public
trading terminal. Legal and tax questions go to a professional. More exchanges
multiply legal complexity, they do not solve it.

---

## Tax and accounting

Capture clean per-trade records now (venue, timestamps, entry and exit, fees,
funding, size) as part of journaling. This overlaps with the PnL-accounting-precision
work. Do not build a bespoke tax-declaration engine. When real money flows, export
to an existing crypto-tax tool (Koinly or similar) or hand it to an accountant
(PIT-38). A cross-exchange activity and PnL dashboard is reasonable once multiple
real accounts exist, not at DRY_RUN.

Paper performance uses an explicit versioned estimate. Gross price movement stays
separate from modeled fees, funding, slippage, and net P&L. Historical rows are never
silently backfilled with invented costs. A future real-money path must reconcile
actual exchange fills, commissions, and funding ledger entries before it can claim
net performance suitable for tax or risk accounting.

## Security

- PostgreSQL SSL in production (`sslmode=require` plus a cert). Dev uses plain auth.
- Exchange API keys live in `.env.prod` only (gitignored), never in the DB, UI, or
  plaintext. The host encrypts env vars. Revisit at-rest encryption when multiple
  accounts connect.
- No direct DB access from the web. All reads go through api-gateway. Postgres is
  never public.
- Rate limiting on api-gateway before any public exposure.
- `gitleaks` plus the existing `make security` in CI (Phase 1). CodeQL or Semgrep
  later.

## Tech debt and DX (opportunistic)

- Pre-push hook: run `make verify` as a pre-push stage so broken code does not reach
  CI.
- CI caching (Go modules, pnpm store, uv cache) keyed on lockfile hashes.
- `golangci-lint` inside `make verify`, not just the pre-commit hook.
- Remove the unused `recharts` from `apps/web` (about 200KB of bundle).
- Docker: pin image versions (no `:latest`), add `mem_limit` and `cpus` per service.
- Frontend polish: `scrollbar-gutter: stable`, force the `en-US` locale in dates and
  the chart, auto-refresh the active OHLCV candle, pump-episode markers on the chart
  (`setMarkers`), and a position-origin badge (paper, bot, manual) on the account
  page plus an entry-price line on the chart.
- Pump scanner: make each per-exchange tag a deep link to that exchange's trade page
  for the pair (open in a new tab), so a token can be inspected on the venue in one
  click. Needs a small per-exchange URL-template map (symbol formats differ, spot vs
  perp). Pure UX convenience, not urgent.
- OHLCV storage in TimescaleDB (enables chart history beyond exchange lookback, plus
  ATR).
- Telegram: persist `seen_bases` in Redis to avoid a startup alert storm, plus
  drop-below and "still pumping" follow-up alerts.
