# Roadmap

> Living document. Updated as we progress. Last refreshed 2026-07-27.

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

## Current state (2026-07-27)

- Live in production on Hetzner. Private access over Tailscale only. Caddy serves
  the Tailscale hostname with a static cert. Public ports 80 and 443 are closed
  with ufw.
- Trading mode is `AUTO_TRADE=false`, `DRY_RUN=true`. No real orders. Paper
  simulation only, accumulating data. `SCORE_THRESHOLD=6`.
- The durable decision/outcome dataset and market-quality gate are live. The scanner
  now has 17 configured linear-USDT perp venues. The immediate task is measuring
  which venues add unique discoveries or useful lead time before adding more feeds.

## Near-term delivery sequence: next 10 pull requests

The measurement foundation is sufficiently complete. The next cycle is strategy
first: use the captured data to reject, retain, or replace rules instead of adding
more infrastructure by default. "More aggressive" means evaluating more variants in
parallel in virtual, shadow, and paper modes. It does not mean enabling real orders
before measured edge and the live-safety gates.

This sequence is ordered, but adjacent research PRs may be developed while a
prospective cohort matures. A failed hypothesis is a useful result and should remove
a rule or stop a workstream rather than trigger unbounded tuning.

1. **Automatic decision-quality report.** Aggregate episode-level net replay results
   by total score, individual score components, pump-size band, venue, liquidity
   quality, and taken/skipped status. Include cluster-aware uncertainty, missingness,
   concentration, and minimum-N warnings. Treat the existing cohort as
   discovery-only: this is the primary diagnostic of whether the score has predictive
   value, not a source of a confirmatory winner.
2. **Pre-registered score-threshold family.** Keep score 6 as baseline and compare a
   small locked family such as 4, 5, 7, and 8 on the same episodes. Treat no trigger
   as cash, reuse the existing entry/exit/cost engine, correct for multiple
   comparisons, and start a new untouched confirmation cohort after the manifest is
   committed. Promote at most a shadow candidate. This tests whether current
   selectivity is suppressing useful paper trades or avoiding bad ones without
   declaring the discovery sample a result.
3. **Pre-registered exit family for OBS-001.** Compare the production exit with
   breakeven-after-activation, a no-progress timeout, and their combination. Keep
   partial take-profit plus runner separate unless the first family shows that exit
   capture, rather than entry quality, is the dominant problem.
4. **Derivatives-context analysis.** Join the persisted funding, open-interest,
   long/short, and liquidation context to eligible episode outcomes. Use only
   point-in-time windows, explicit availability/coverage, clustered inference, and a
   locked small hypothesis family. Do not turn every available field into a score.
5. **Live multi-variant shadow evaluator.** Run registered score, confirmation, and
   exit candidates beside the baseline without placing orders. Give every variant
   isolated state and capture the actual quote, spread, depth, impact, lag, and
   rejection reason at its own trigger time. This closes the historical-order-book
   blind spot in delayed-entry replay.
6. **Durable shadow track record and automatic report.** Persist versioned shadow
   positions and resolutions, then report expectancy, profit factor, drawdown,
   initial-stop rate, captured MFE, execution coverage, and disagreement with the
   baseline. Schedule the report so strategy progress is visible without manual SQL.
7. **Bounded CEX hot set.** Promote +20% WATCH candidates to a targeted 1-to-5-second
   polling or websocket set, decouple notification pickup from the broad market scan,
   and measure threshold-to-alert and peak/retrace timing. Do not accelerate all
   symbols or venues indiscriminately.
8. **Out-of-sample shadow champion.** Freeze the strongest candidate selected by
   PRs 1-4, bump its strategy version, and collect a new untouched cohort through the
   live shadow path. No parameters may be changed after the confirmation boundary.
9. **Paper champion promotion.** If the out-of-sample shadow gate passes, run the
   champion through the existing paper order/monitor/journal lifecycle while the
   baseline remains a control. Add operational alerts for missing market data,
   divergent fills, stale positions, and strategy-level drawdown.
10. **Go/no-go checkpoint.** If at least 100 eligible episodes, 30 asset clusters,
    complete paired resolution, positive net expectancy after costs, familywise
    improvement, acceptable drawdown, and stable out-of-sample paper behavior all
    pass, prepare the human-confirmed tiny-capital stage from Phase 3. If they do not,
    do not lower the safety bar: reject or pause pump-short v1 and move the research
    budget to the pre-registered delisting-short or DEX narrative tracks.

The LBank perpetual-history limitation is parked in
[CCXT-003](docs/tasks/ccxt/003-lbank-perpetual-ohlcv-research.md). It remains visible,
but it must not block this sequence. Cross-venue fallback stays explicitly labelled;
a future scanner-derived path is a separate provenance-aware fallback, not fake
exchange OHLCV.

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
  - [ ] Capture the pre-optimization latency baseline for at least 72 hours and 20
        delivered pump events, whichever takes longer. The durable source is
        `app.pump_alert_deliveries`; `app.pump_event_sources` contains the later
        highest change actually observed for the same event/venue. Check component
        p50/p95 rather than only the total. Use the verification commands directly
        below this checklist.

  - [ ] Decouple a fast Redis-only notifier loop from the broad exchange scan interval,
        then promote active candidates into a bounded 1-to-5-second hot set using
        targeted polling or websockets. Use explicit WATCH, HOT, NEW_HIGH, and RETRACE
        transitions. Do not increase whole-market REST frequency until rate-limit and
        host-load measurements support it.

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
    - [ ] Entry-floor challenger verification after merge:
      - Deploy analytics only. Wait until the prospective events have closed and every
        recorded decision has its exact-anchor 8-hour outcome. The default command is
        locked to the registered cohort and both measurement/entry strategy versions:

        ```bash
        make prod-deploy-svc SERVICE=analytics
        make prod-virtual-threshold-challenger-report
        ```

      - Before the formal read, choose an exclusive UTC cutoff without inspecting the
        threshold results. Archive the reproducible JSON manifest outside Git:

        ```bash
        mkdir -p backups/reports
        make prod-virtual-threshold-challenger-report \
          ARGS="--until 2026-08-10T00:00:00Z --format json" \
          > backups/reports/entry-thresholds-2026-08-10.json
        ```

      - Check excluded episodes, unresolved decisions/paths, selected decision and
        venue per floor, no-trigger cash episodes, cluster concentration, conditional
        trade rate, net expectancy, initial-SL rate, and paired delta versus +30%.
        The five challengers are one Holm-corrected family. Formal output stays hidden
        before the locked first 100 episodes are fully paired with at least 30 asset
        clusters. A pass requires positive own expectancy, positive conservative
        familywise paired lower bound, Holm rejection, and positive top-cluster
        sensitivity, and produces only a live-shadow candidate.

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
- [ ] Selective real-time microstructure capture before a full websocket migration.
      Use the already-installed CCXT Pro feeds only for active pump/watch episodes,
      not every symbol on every venue. Persist bounded 5-to-10-second aggregates for
      spread, depth, imbalance, taker buy/sell flow, basis, and liquidation bursts;
      keep raw L2 data only for a short explicitly budgeted window. This gives the
      replay non-recoverable entry-confirmation features without exhausting the
      current 4 GB host.
- [ ] Collector to websocket data layer. The Bybit collector is the seed of the
      intended Go hot-path layer, but its consumer was never built (it publishes to
      NATS and nobody reads). Develop it here, when exchange count and latency
      actually matter: add a consumer that persists the stream, add exchanges, and
      migrate the scanner from polling to websockets (detection lag 5 min to 60s to
      about 3s). Keep ARCHITECTURE.md honest about this.
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
