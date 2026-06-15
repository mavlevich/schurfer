# Roadmap

> Living document. Updated as we progress.

## Principles

- Ship working product fast, iterate later
- Architecture right from day one (service boundaries, NATS contracts), code can be dirty inside
- Tests and structured logging everywhere from Sprint 1
- UI progress on every sprint
- Go for hot path (collectors, execution); Python for analytics/ML
- Bybit as first exchange (Binance perps blocked in Poland)

---

## Sprint 1: Foundation ✅

- [x] Monorepo, Go/Python workspaces, frontend stack decisions
- [x] Repo structure, ADRs
- [x] Docker Compose: PostgreSQL + TimescaleDB + Redis + NATS
- [x] Structured logging (Python: structlog, Go: slog)
- [x] Trade journal schema + migrations
- [x] Web scaffold: Vite + React + shadcn/ui, auth, System Status page
- [x] Bybit WS collector (Go, publishes to NATS)
- [x] make verify quality gate (ruff, mypy, pytest, go test/vet, web lint/build)

## Sprint 2: Pump Scanner ✅

- [x] Multi-exchange pump scanner (12 CEX perp markets via ccxt)
- [x] Redis-backed `pumps:latest` with 5-min TTL
- [x] Graceful degradation: skip Redis write if all exchanges fail
- [x] Web UI at `/pumps`: live table, exchange badges, color-coded %
- [x] `GET /api/pumps` in api-gateway
- [x] Analytics service in Docker Compose

## Sprint 3: Pump History + Token Detail + Alerts ✅

_Goal: know what happened after a pump, drill into a single token, get notified in real time._

- [x] PostgreSQL `pump_events` table (base, episode, peak_pct, last_pct, retrace_pct, closed_at, miss_count)
- [x] Scanner writes event on first detection; cooling period (3 consecutive misses) before closing episode
- [x] Multi-episode tracking per token: `(base, episode)` composite key, new episode opens automatically after prior one closes
- [x] Snapshots at +1h/+4h/+24h stored per event regardless of episode state — feeds the historical dataset
- [x] `GET /api/pumps/history` with filters (exchange, base, since/until); defaults to last 24h
- [x] `GET /api/pumps/{base}/history` — all episodes for a single token
- [x] Token detail page `/pumps/:base`:
  - [x] Price chart (OHLCV) with interval selector: 5m / 15m / 1h / 4h; defaults to 15m
  - [x] Chart picks exchange where pump was strongest (not just Binance-first)
  - [x] Exchange breakdown: which CEXs are pumping and by how much
  - [x] Pump episodes table: first seen, ended, peak %, retrace %, LIVE/closed status
  - [ ] Basic stats: avg retrace, fastest/slowest retrace from history (Sprint 4 data)
- [x] BingX sanity cap: `abs(pct) > 5000%` filtered in scanner (stock-index futures garbage)
- [x] Scanner returns `(pumps, errors)` — retrace close skipped on any exchange error
- [x] Telegram notifier: Go service, alerts on new pumps, deduplication via Redis `notifier:seen:*` 24h TTL

**Data model notes:**

- `pump_events` records _events_, not tokens — same token can have many rows (one per pump episode); no blacklisting
- `notifier:seen:*` (24h TTL) deduplicates alerts within a single pump episode only; a new episode months later gets a fresh alert
- `miss_count` tracks consecutive scan misses before closing — prevents false closes on single network blip
- Token trading cooldown (Sprint 6) = "don't re-enter the same fading episode", not "avoid this token forever"

## Sprint 4: Pump Analytics — "Short or Wait?"

_Goal: answer "is this pump still going or time to short?" using real market structure data._

Requires Sprint 3 data (a few weeks of history) for retrace stats.

**Why history matters even without trades:**
Every pump event we record — whether we trade it or not — contributes to retrace distributions and timing models. A token that pumps 3x a year is more valuable in the dataset than one that pumped once. The goal is: "historically, after a +80% pump on Binance, median retrace was −42% in 4h" — this needs volume of data, not trades.

**Open Interest analysis (cross-exchange)**

- [ ] Fetch OI per exchange: Binance `/fapi/v1/openInterest`, Bybit `/v5/market/open-interest`, OKX `/api/v5/public/open-interest`
- [ ] Aggregate total OI across exchanges → store snapshots in `oi_snapshots` table (base, exchange, oi_usd, ts)
- [ ] OI delta: compare current OI vs OI at pump start (first_seen_at) — is new money still entering?
- [ ] OI divergence signal: price rising + OI flat/declining = weak move, likely to retrace
- [ ] OI spike signal: price +X% AND OI +Y% in same window = real accumulation, pump may continue
- [ ] Per-exchange OI breakdown: which exchange has dominant position (carries the most risk)?

**Funding rate**

- [ ] Fetch current funding rate: Binance `/fapi/v1/premiumIndex`, Bybit `/v5/market/funding/history`, OKX `/api/v5/public/funding-rate`
- [ ] Funding rate threshold: >0.1% per 8h = longs paying heavily = unsustainable, short setup
- [ ] Funding annualized display: show as APR so easier to compare across coins

**Composite short-readiness score** (`GET /api/pumps/{base}/signals`)

```
score components:
  pump_age_hours      → >4h adds points (late)
  price_change_pct    → >100% adds points (extended)
  oi_trend            → declining OI adds points (distribution)
  funding_rate        → >0.1% adds points (crowded longs)
  price_velocity      → slowing 1h momentum adds points

verdict: Pumping / Cooling off / Short setup / Prime short
```

- [ ] Show score on token detail page alongside chart
- [ ] Historical stats: "after +80% pumps on Binance, median retrace was −42% in 4h"

**Data**

- [ ] Per-token stats: average retrace %, time-to-retrace after X% pump (needs weeks of history)
- [ ] Age indicator: "token has been pumping for 3h — historically late to enter"
- [ ] Pump lifecycle tracking: record price snapshots at +1h, +4h, +24h after first detection — needed to build retrace distributions and entry/exit timing models
- [ ] Leverage suggestion: based on retrace % distribution (e.g. median −42% → 2x short has high win rate), volatility-adjusted max leverage per token category

**Professional-grade analytics (longer term within Sprint 4):**

- [ ] Volume profile per pump event: where did volume cluster during the pump? high-volume nodes = likely support/resistance on retrace
- [ ] Repeat-pumper detection: tokens that pump on a regular cadence (weekly/monthly pattern) flagged separately — higher confidence trades
- [ ] Cross-event correlation: does a pump on Binance predict a follow-through on OKX within N minutes? lag analysis across exchanges
- [ ] Retrace speed classification: fast retrace (< 1h back to baseline) vs slow bleed (12h+) — determines optimal TP placement
- [ ] "Dead cat" filter: pumps that briefly recover then dump lower than pre-pump baseline — pattern to avoid on the long side
- [ ] Historical replay: given a pump event from the past, show what the optimal entry/exit would have been — sanity check for strategy parameters

## Sprint 5: Cross-Market Signals (CEX Spot + DEX)

_Goal: catch pumps earlier by watching markets where they start before hitting perps._

- [ ] CEX spot scanner: Coinbase, Upbit for `/USDT` pumps as early signal source
- [ ] DEX scanner: DexScreener/GeckoTerminal API for Solana + EVM memecoins
  - Separate feed from perp scanner — different risk profile (no perp hedge possible)
  - Filters: min liquidity, age, security scan status
  - Signals: spot/DEX pump → look for correlated perp on Bybit/OKX to short
- [ ] Hyperliquid perp support (DEX perps, different symbol format)
- [ ] UI: "signal source" column — where the pump started vs where to trade it
- [ ] Correlation data: does Upbit pump predict Bybit perp retrace?

## Sprint 6: Execution

_Goal: go from signal to actual trade._

- [ ] Approve flow: Telegram button → ccxt places short on Bybit/OKX
- [ ] Position tracking in trade journal (entry, exit, PnL)
- [ ] Risk guardrails: max position size, max open positions cap (e.g. 3-5), funding drag check
- [ ] Web: live positions page with unrealized PnL
- [ ] TP/SL management: set take profit and stop loss on entry; auto-adjust both as price moves
- [ ] Trailing stop on retrace: when a retrace is expected but position stays open, tighten SL toward entry (reduce risk without closing) — only close on SL hit, not on forecast alone
- [ ] Liquidation price awareness: after position opens, check that liquidation price is at a safe distance; move margin (partial close or add margin) if it drifts too close

**Signal prioritization (needed before any automation)**

- [ ] Score each pump signal: weight by pump %, 24h volume (liquidity proxy), and exchange tier (binance > bybit > okx > gate)
- [ ] Direction heuristic: if `current_pct` is close to `peak_pct` — momentum is still running, consider long; if peak is well above current — retrace already started, prefer short
- [ ] Slot cap enforcement: keep at most N concurrent positions; queue stronger incoming signals and drop weaker ones that never got filled
- [ ] Position rotation: if all slots are full and a new signal scores higher than the weakest open position, close the weakest (at market) and open the new one — only when PnL on the old one is not deeply negative (configurable threshold)
- [ ] Cooldown per token: after closing a position on a token, ignore new signals for that token for X minutes to avoid re-entering a fading pump

## Sprint 7: Account Integration + Tax

_Goal: know if the strategy is actually profitable, and handle taxes._

- [ ] Connect exchange API keys (stored encrypted in DB)
- [ ] Import trade history via ccxt (all positions, not just Schurfer-placed)
- [ ] Win rate, avg PnL, MFE/MAE per strategy type
- [ ] Tax export: realized positions with cost basis in fiat (PIT-38 ready CSV)

## Sprint 8: Observability + Deploy

_Goal: run in production without babysitting._

**Status page (extended)**

- [ ] System resource metrics on Status page: CPU %, RAM used/total, disk, network I/O — exposed via lightweight sidecar (e.g. `node_exporter` or a small Go poller hitting `/proc`)
- [ ] Per-service detail stats: analytics scan latency + exchange error rate, api-gateway request count + p95 latency, pump count trend (sparkline last 24h)
- [ ] Web logs tab: SSE stream of structured logs from api-gateway (dev/ops tool, auth-gated)

**Infra**

- [ ] Grafana + Prometheus dashboards (collector throughput, scan latency, pump count)
- [ ] AWS EC2 t4g.medium Frankfurt deploy (Docker Compose on VPS)
- [ ] Domain + Cloudflare DNS + Tunnel
- [ ] CI: GitHub Actions
- [ ] Secrets management: SOPS + age

## Sprint 9+: Advanced Signals

- [ ] News pipeline: CryptoPanic + RSS → Llama pre-filter → Claude scoring
- [ ] Smart money tracker for Solana (Helius)
- [ ] Pre-launch short detector (TGE-aware, low-float VC tokens)
- [ ] MM history database (DWF, Wintermute patterns)
- [ ] Investigator-based signals (ZachXBT, MetaSleuth)

---

## Security

- **PostgreSQL: SSL in production** — dev uses plain password auth, prod needs `sslmode=require` + certificate
- **Exchange API keys encryption** — when Sprint 7 connects accounts, keys must be encrypted at rest (AES-256 or libsodium) before storing in DB, never in plaintext
- **DB credentials rotation** — env-var based now; move to SOPS + age (Sprint 8) or AWS Secrets Manager on ECS
- **No direct DB access from web** — all DB reads go through api-gateway, PostgreSQL port never exposed publicly
- **Rate limiting on API** — add per-IP rate limiting to api-gateway before going public (Sprint 8)
- **CodeQL + Semgrep in CI** — static analysis for SQL injection, secrets in code (Sprint 8)

## Technical debt / optimization

**DX / CI quality (do before Sprint 4)**

- **Pre-push hook** — add `make verify` as a pre-push stage in `.pre-commit-config.yaml` + `pre-commit install --hook-type pre-push`; right now broken code reaches CI before anyone notices locally
- **CI caching** — no caching for Go modules, pnpm store, or uv cache; every run re-downloads everything including ccxt and miniredis; add `actions/cache@v4` for `~/.cache/go-build`, `~/go/pkg/mod`, `~/.local/share/pnpm/store`, `~/.cache/uv` keyed on lockfile hashes
- **golangci-lint in `make verify`** — currently `verify` runs `go test` + `go vet` but golangci-lint only via pre-commit hook; someone running `make verify` without hooks installed misses the linter
- **Remove recharts** — `recharts` is in `package.json` but unused in code; `lightweight-charts` covers all chart needs; saves ~200KB from the bundle (`pnpm --filter @schurfer/web remove recharts`)
- **Coverage thresholds** — coverage is collected in CI but no minimum is enforced; set `fail_under = 70` in `[tool.coverage.report]` (Python) and add a threshold check after `go test -coverprofile` (Go)
- **wrapcheck + goconst in golangci-lint** — `wrapcheck` enforces consistent `fmt.Errorf("...: %w", err)` wrapping for external package errors; `goconst` flags repeated string literals that should be constants
- **Vitest for web utils** — `test-ts` CI job runs `pnpm run test` but there are no tests; add Vitest and cover `pumpsCache` TTL logic and `fmtVol`/`fmtPct` formatters

- **Analytics: WebSocket ticker subscriptions** — replace REST `fetchTickers` polling with WS updates; reduces bandwidth ~10-20x (Sprint 4)
- **Analytics: persistent exchange connections** — currently reconnects every scan; keep sessions alive between scans to reduce TLS handshake overhead
- **Docker resource limits** — add `mem_limit` / `cpus` per service in docker-compose to prevent one container starving others
- **Docker image pinning** — replace `timescale/timescaledb:latest-pg17` and similar with exact versions (e.g. `2.17.0-pg17`); avoids silent breakage on `docker pull`
- **Docker restart policy** — `restart: unless-stopped` missing on postgres, redis, nats, api-gateway; add before production deploy
- **Docker port binding** — ports 5432 and 6379 bind to `0.0.0.0` in dev; production compose should bind to `127.0.0.1` only
- **NATS healthcheck** — uses `wget --spider` (HEAD); verify NATS monitoring endpoint handles HEAD or switch to GET like api-gateway
- **Collector: limit symbols in dev** — BYBIT_SYMBOLS should default to a small set locally; subscribe to all only in production
- **Scan interval tuning** — 120s in production is enough for pump detection; 60s only needed once we have execution
- **ccxt footprint** — 500MB+ RAM for Python + ccxt; long-term consider native HTTP calls to exchange APIs for frequently-polled exchanges
- **Telegram: seen_bases resets on restart** — in-memory set clears on analytics restart, causing alerts for all active tokens at startup (could be noisy); consider persisting seen_bases in Redis so restart is transparent
- **Telegram: drop-below alerts** — currently only alerts on new pumps; consider sending a follow-up when a token drops back below threshold (e.g. "BTC back to +18%, was +45%")
- **Telegram: alert deduplication window** — if a token stays above threshold for hours, no repeat alerts; might want a "still pumping" reminder after N hours

---

## Backlog (no sprint yet)

- Tokenized assets (stocks/metals on Bybit/OKX) — separate scanner filter, same ccxt fetch
- ECS migration (after proving out on EC2)
- Paper trading / shadow mode framework
- Replay / backtesting harness
- Portfolio risk budget engine (per exchange / correlated basket)
- Multi-exchange capital management (Treasury module)
- Polymarket CLOB integration
