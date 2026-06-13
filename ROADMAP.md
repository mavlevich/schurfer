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

## Sprint 3: Pump History + Token Detail + Alerts

_Goal: know what happened after a pump, drill into a single token, get notified in real time._

- [ ] PostgreSQL `pump_events` table (base, exchange, peak_pct, detected_at, retrace_pct, closed_at)
- [ ] Scanner writes event on first detection and on token disappearance (retrace)
- [ ] Tokens stay visible for 24h after first detection: show `peak_pct` (max during event) + `current_pct` (live or last seen); disappear only after 24h, not on retrace
- [ ] `GET /api/pumps/history` with filters (exchange, base, date range)
- [ ] Web history page: "COAI +78% peak → retraced to +12% · 4h ago"
- [ ] Token detail page `/pumps/:base` — click any token in the table to open:
  - Price chart (OHLCV fetched on demand via ccxt from best exchange)
  - Exchange breakdown: which CEXs are pumping it and by how much
  - Pump history for this token: previous events, peak %, retrace %, duration
  - Basic stats: avg retrace, fastest/slowest retrace from history
- [ ] Telegram bot: alert on new pump detection with exchange + % info

## Sprint 4: Pump Analytics — "Short or Wait?"

_Goal: answer "is this pump still going or time to short?" using real historical data._

Requires Sprint 3 data (a few weeks of history).

- [ ] Per-token stats: average retrace %, time-to-retrace after X% pump
- [ ] Signal on active pumps page: "median retrace after +50% = −35% in 2h"
- [ ] Age indicator: "token has been pumping for 3h — historically late to enter"
- [ ] OI spike detector: sudden open interest growth = new money entering, not just price move
- [ ] Funding rate filter: high funding = crowded short, factor into sizing recommendation
- [ ] Composite score per pump: combines age, OI change, funding, historical retrace pattern

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
- [ ] Risk guardrails: max position size, max open positions, funding drag check
- [ ] Web: live positions page with unrealized PnL

## Sprint 7: Account Integration + Tax

_Goal: know if the strategy is actually profitable, and handle taxes._

- [ ] Connect exchange API keys (stored encrypted in DB)
- [ ] Import trade history via ccxt (all positions, not just Schurfer-placed)
- [ ] Win rate, avg PnL, MFE/MAE per strategy type
- [ ] Tax export: realized positions with cost basis in fiat (PIT-38 ready CSV)

## Sprint 8: Observability + Deploy

_Goal: run in production without babysitting._

- [ ] Web logs tab: SSE stream of structured logs from api-gateway (dev/ops tool)
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

- **Analytics: WebSocket ticker subscriptions** — replace REST `fetchTickers` polling with WS updates; reduces bandwidth ~10-20x (Sprint 4)
- **Analytics: persistent exchange connections** — currently reconnects every scan; keep sessions alive between scans to reduce TLS handshake overhead
- **Docker resource limits** — add `mem_limit` / `cpus` per service in docker-compose to prevent one container starving others
- **Collector: limit symbols in dev** — BYBIT_SYMBOLS should default to a small set locally; subscribe to all only in production
- **Scan interval tuning** — 120s in production is enough for pump detection; 60s only needed once we have execution
- **ccxt footprint** — 500MB+ RAM for Python + ccxt; long-term consider native HTTP calls to exchange APIs for frequently-polled exchanges

---

## Backlog (no sprint yet)

- Tokenized assets (stocks/metals on Bybit/OKX) — separate scanner filter, same ccxt fetch
- ECS migration (after proving out on EC2)
- Paper trading / shadow mode framework
- Replay / backtesting harness
- Portfolio risk budget engine (per exchange / correlated basket)
- Multi-exchange capital management (Treasury module)
- Polymarket CLOB integration
