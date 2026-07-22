# Roadmap

> Living document. Updated as we progress. Last refreshed 2026-07-19.

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

## Current state (2026-07-19)

- Live in production on Hetzner. Private access over Tailscale only. Caddy serves
  the Tailscale hostname with a static cert. Public ports 80 and 443 are closed
  with ufw.
- Trading mode is `AUTO_TRADE=false`, `DRY_RUN=true`. No real orders. Paper
  simulation only, accumulating data. `SCORE_THRESHOLD=6`.
- Reality check: about 288 skipped versus 1 opened_dry_run over 3 days. Score below
  6 on almost everything, usually because OI is still growing, which means the pump
  is not exhausted. The scanner is deliberately broad and the entry filter is
  strict. This works as intended. Sample size is 1, so nothing to conclude yet.

## Shipped

- Foundation: monorepo, Docker Compose (Postgres and TimescaleDB, Redis, NATS),
  structured logging, trade-journal schema, web scaffold (Vite, React, shadcn,
  auth), Bybit websocket collector (Go to NATS), `make verify` quality gate.
- Pump scanner: 12 CEX perp markets via ccxt, Redis `pumps:latest`, graceful
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
- Safety hardening: exchange-native stop-loss (reduce-only stop-market on entry),
  durable daily PnL (`journal:pending_close` retry marker, `risk:pnl_ready` positive
  lease, idempotent `close_trade`, Postgres as the source of truth), position
  reconciliation (detect a vanished position and close it from the filled SL order).
- OHLCV robustness: BingX and MEXC futures fetchers, volume-ranked fallback,
  unbounded exchange fallback for old episodes, MEXC numeric and string field
  tolerance.
- Production deploy: Hetzner, Docker Compose prod stack, Caddy, Tailscale, Postgres
  backup and a tested restore, GitHub Actions CI (lint, tests for Go, Python, TS,
  security).

---

## The plan

### Phase 0: Measurement layer (now)

Start collecting the evidence that answers "is there edge?". Non-recoverable data
comes first.

Status (2026-07-22): the decision + liquidity + price dataset is live and durable;
the two remaining items are dataset-health visibility, not data capture.

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
- [x] Schema and decision-write path (migrations 0008-0010 plus the execution write
      path). This is independent of where the score is computed, so it does not commit
      us to any later scoring decision.
- [x] Run 24/7 (already deployed) plus a stale-data Telegram alert (no fresh scans or
      signals for N minutes). A silently dead scanner rots the dataset.
- [ ] Operational health on the existing Status page: pipeline liveness (scanner
      alive, last-scan age, signal freshness), per-service error rate, container
      health, and basic host resources (RAM, disk) so a memory leak or a disk filled
      with data does not kill collection silently. Keep it lightweight. This is about
      "is the dataset being collected without gaps", not a performance product.
- [ ] Dataset completeness metrics: decisions/hour, % features present, % liquidity
      present, % liquidity fetch_failed, and lag between signal computed_at and the
      decision. These tell us early if the dataset is degrading.
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
  - [ ] Versioned virtual-strategy layer: replay decisions by token episode under the
        actual v1 rules and pre-registered challengers, including fees, funding,
        liquidity-aware slippage, TP/SL/trailing/max-hold, and taken-vs-skipped labels:
    - taken and won, or taken and lost
    - skipped and would-have-won (missed edge), or skipped and correctly avoided

  - [ ] Derive recoverable pre-decision candle features (including blow-off concentration
        and reversal strength) from fully closed OHLCV and test whether they separate
        outcomes before promoting either to a live gate or score component.

    Then aggregate. Expectancy of taken versus skipped by score bucket answers "is
    the threshold in the right place?" (if score-5 skips beat score-6 trades, it is
    not). Feature-level separation (which feature cut best splits winners from losers)
    is the automatic "what should we have done". Evaluate against the actual
    `strategy_version`, and allow sweeping a few exit variants. Notes: virtual fills
    for old decisions use a crude slippage assumption, while decisions made after the
    liquidity snapshot ships get realistic fills; treat vanished OHLCV (delisted
    tokens) as "outcome unknown", which is itself a delisting-short signal.

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

- [ ] Broaden the scanner to about 15 to 20 solid perp venues (from 12). Quality
      over count. Each new exchange is a parse surface that can silently poison the
      dataset (see the BingX >5000% garbage filter and the MEXC field-type bug).
      Validate each one. Not the long tail to 40.
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
