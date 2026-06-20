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
- [ ] OI concentration ratio: if one exchange holds >60% of total OI, flag it — likely a single large actor; liquidation will be sharp and directional

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

- [ ] Per-token stats: average retrace %, time-to-retrace after X% pump (needs weeks of history); compare current pump % to token's own historical distribution — "for SOL, a +15% move is routine; for XYZ, it's a 3σ event"
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

## Revised shipping plan (agreed 2026-06-19)

_Goal: get to a real trade as fast as possible. Analytics sprints (5, 5.5) are deprioritised — execution comes first._

| #   | What                                                                      | Branch / PR                              |
| --- | ------------------------------------------------------------------------- | ---------------------------------------- |
| 1   | OHLCV exchange fallback — BingX + MEXC fetchers, volume-ranked retry      | ✅ `feat/ohlcv-exchange-fallback` PR #32 |
| 2   | `apps/account` service — balance + positions across all exchanges         | in progress                              |
| 3   | Telegram approval button → short (first week manual, then switch to auto) | —                                        |
| 4   | Full automation — auto-short when score ≥ threshold, no button needed     | —                                        |
| 5   | Emergency stop + daily loss limit                                         | —                                        |
| 6   | Trade queue + UI visualization                                            | —                                        |
| 7   | Position tracking in trade journal (entry, exit, PnL)                     | —                                        |
| 8   | Hosting + domain + SSL + production deploy                                | —                                        |

Sprint 5 (cross-market signals) and Sprint 5.5 (listing/delisting) are parked until after first real trade.

## apps/account service (Python/FastAPI + ccxt)

_Single service for all exchange interactions. Decided 2026-06-19._

**Architecture decisions:**

- Python + ccxt — handles auth (HMAC, passphrase, nonce) for all exchanges out of the box
- Go is for speed-critical path (pump detection, WebSocket); Python handles trading execution
- Single service, not split — strategy logic stays here alongside balance/positions
- API keys in `.env` only — no DB storage, no UI for key management; hosting platform encrypts env vars
- Hyperliquid (Ethereum wallet auth) deferred — different paradigm, add later

**Endpoints:**

- `GET /balance` — aggregated balance across all configured exchanges
- `GET /positions` — all open positions across exchanges
- `POST /order` — place order (internal pre-checks before executing)
- `GET /risk` — current slot usage, daily P&L, limits

**Pre-trade checks (inside POST /order):**

- `trading:enabled` Redis flag — if false, reject all orders (emergency stop)
- Max open positions cap
- Already have position in this token?
- Sufficient margin?
- Daily loss limit not breached?
- Funding rate not too high (would eat PnL)?

**Emergency stop:**

- Redis key `trading:enabled` — checked before every order
- `/stop` Telegram command → sets flag to false
- Big red button in web UI
- Daily loss limit auto-stop (e.g. -$X in 24h → pause automatically)

**Trade queue + UI visualization:**

```
⏳ Analyzing BEAT...           (score calculation)
🔄 BEAT short queued $200 2x  (order pending)
✅ BEAT short open @ $0.0023  (active position)
🏁 BEAT closed +$34           (completed)
```

## Go pump scanner (replaces Python analytics polling)

_Discussed 2026-06-19. Currently Python ccxt polls exchanges every 5 min — up to 5 min detection lag._

**Why Go:**

- Go HTTP clients already built for OHLCV fallback — same pattern for tickers
- <0.5% CPU constant vs Python 3-5% spike per cycle, no GIL
- Natural stepping stone to WebSocket — swap `fetchTickers()` → `subscribeWS()` later

**Plan:**

- New Go service or extend `apps/collector`
- One goroutine per exchange, parallel ticker fetch every 60s
- Multi-stage alerts: +20% "on radar" → score threshold "short setup" → retrace started
- Python analytics keeps OI, funding, stats (heavy analytics, ML-friendly long-term)

**WebSocket follow-up (after Go polling stable):**

- Same exchange clients, swap HTTP → WS
- Binance: `!miniTicker@arr` (one connection, all futures, ~80KB/3s)
- Detection latency: 5 min → 60s → ~3s

---

## Sprint 5.5: Listing & Delisting Event Signals (future sprint)

_Goal: exploit predictable price patterns around exchange listing and delisting announcements._

Both events fit the existing short-readiness logic — different trigger, same framework. Historical data is free: exchanges publish announcement archives, OHLCV is available via their APIs retrospectively, so backtesting requires no custom data collection.

**Listing pumps**

Pattern: announcement → spot pump → perp listing → second pump wave → retrace.
Often stretched over 2–3 days, giving more time to enter than a regular pump.

- [ ] Monitor exchange listing announcement feeds (Binance/Bybit/OKX publish RSS/API)
- [ ] Flag new perp contracts the moment they appear (compare current contract list vs cached)
- [ ] Feed into existing pump scanner with "listing" tag on the signal
- [ ] Backtest: pull historical listing dates from exchange archives + OHLCV → compute median retrace depth/speed

**Delisting shorts**

Pattern: announcement → sharp dump → dead cat bounce (+20–40%, short squeeze + "I'll buy the dip") → prolonged bleed to zero over ~1 week as holders withdraw.
Swing position, longer hold than listing pump. Multiple delistings happen simultaneously → natural portfolio of uncorrelated shorts.

- [ ] Monitor delisting notices (Binance publishes ~1 week ahead; Bybit/OKX similar)
- [ ] Dead cat detector: detect the bounce phase using our existing pump scorer (the bounce looks like a pump)
- [ ] Timeline tracker: show "N days until delisting" as a countdown on the signal card
- [ ] Risk: some tokens have withdrawal period after delisting — longs can't exit cleanly → drives the bleed
- [ ] Backtest: Binance has delistings going back to 2019; take 50+ events, compute typical dead-cat amplitude and duration

**Historical backtest (no data collection needed)**

Exchange listing/delisting announcement archives are public. OHLCV for the surrounding period is available via standard API endpoints. We can run the first backtest pass entirely on external data before building any infrastructure.

- [ ] Script: pull last 100 Binance delisting events from announcement page (or community-maintained list)
- [ ] For each: fetch OHLCV via Binance futures API from announcement date + 14 days
- [ ] Compute: dump depth, dead-cat magnitude, time from announcement to dead cat, time to final floor
- [ ] Output: distribution charts + recommended entry window ("short the dead cat when price recovers >20% from the dump low")

---

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

**Market microstructure**

- [ ] Liquidation heatmap: fetch open interest by price level (Binance `/fapi/v1/openInterestHist`, Bybit `/v5/market/risk-limit`) — show price zones where cascade liquidations will occur; pump heading toward a short-liquidation cluster = continuation likely
- [ ] Spot vs Perp divergence: price difference between CEX spot and perp for the same token; if perp > spot by >0.5% during a pump = leveraged demand, no real buyers; divergence closing = retrace imminent
- [ ] Volume anomaly: compare current 1h volume to 30-day rolling average; pump at 1x average = weak, 5x+ = real event; filters noise from thin-book moves
- [ ] Order book imbalance: bid/ask volume ratio in the top N levels of the order book; >80% on ask side during a pump = distribution, not accumulation
- [ ] Thin book flag: tokens where moving the price 2% requires <$100K; easy to manipulate, pumps are less meaningful — separate risk tier
- [ ] Taker/maker ratio: aggressive buy orders (takers) vs passive; rising taker ratio on buy side = real demand; price rising on maker bids = someone painting the tape

**Macro timing signals**

- [ ] BTC dominance shift: BTC.D falling = alt season, rising sharply = risk-off; overlay on pump scanner to avoid shorting alts into a bull market
- [ ] Aggregate funding rate index: average funding across top-20 perp markets; when index >0.08% per 8h + Fear&Greed >75 = macro crowded-long, best window to fade individual pumps
- [ ] Regulatory calendar: scheduled SEC/CFTC hearings, ETF approval dates, major unlock dates for top tokens — events that create predictable vol spikes

**Overvaluation screening**

- [ ] FDV vs Market Cap ratio: Fully Diluted Valuation / circulating market cap; ratio >20x means most supply is not yet in circulation — future dilution will suppress price; flag tokens where pump happens at extreme FDV ratios
- [ ] TVL efficiency (DeFi): TVL / Market Cap; healthy range ~1:1 to 1:5; ratio >1:50 = protocol severely overvalued relative to actual usage
- [ ] Token velocity: volume / market cap per day; very high velocity = nobody holds, purely speculative; very low = illiquid or dead token; context for interpreting pump significance

**On-chain analytics**

- [ ] Holder concentration (Gini): top-10 wallet % of total supply per chain (Etherscan, Solscan APIs); if one address holds >50% = price fully controlled by that entity; flag as "whale trap" on scanner
- [ ] Token unlock / vesting calendar: aggregate unlock schedules from TokenUnlocks/Vestlab; pump 2 weeks before a major team/VC unlock = likely exit liquidity setup — highest-confidence short
- [ ] Wallet clustering: group addresses that received tokens from the same source transaction; real concentration is always worse than visible — one actor behind 1000 addresses
- [ ] Bridge flows: cross-chain inflows via Wormhole, LayerZero, Stargate for a specific token; targeted bridging into a thin-liquidity chain before a pump = coordinated move
- [ ] Smart money identification: addresses that bought >5 days before a +50% move in the last 6 months; if such an address starts accumulating now = pre-pump signal
- [ ] Wash trading detection: round-trip patterns (A→B→A within 1h, same size), exchange pairs with suspiciously high volume vs OI; flag exchanges where pump volume is likely artificial

**Squeeze protection**

- [ ] Short squeeze scanner: funding rate deeply negative (shorts paying longs) + price rising = squeeze in progress; add "squeeze risk" flag that suppresses short_setup verdict — prevents entering a short into an ongoing squeeze
- [ ] Squeeze magnitude estimate: size of short OI × funding rate × time since funding went negative = pressure gauge; higher = more violent the squeeze, avoid until it resolves

**OI coiled spring**

- [ ] OI spike without price move: if OI grows >15% while price moves <2% — someone is building a large position silently; fire a "coiled spring" alert; direction unknown but explosion imminent; use existing `oi_snapshots` data
- [ ] Spring direction hint: if OI spike happens during a slow grind up = likely long accumulation; during sideways = ambiguous; monitor which way price breaks within 1h of the spike

**Funding rate arbitrage (basis trade)**

- [ ] Basis trade alert: when funding >0.3%/8h (≈328% APR), flag as "basis trade opportunity" — short perp + long spot captures funding with zero directional risk; show estimated daily yield at current rate and position size
- [ ] Basis trade tracker: log when the threshold is crossed + how long it sustained; builds a dataset of which tokens have recurring elevated funding (chronic crowded-long = repeated short setups)

**Correlation break detector**

- [ ] BTC-relative move: compute each token's % change vs BTC % change over last 1h; if BTC is flat (±0.5%) and token is +10%+ → isolated pump, not market-wide move; fire earlier than pump scanner since no threshold breach required
- [ ] Correlation score: rolling 24h correlation coefficient between token and BTC price; drop from >0.8 to <0.3 = decorrelation event = token being specifically targeted; early warning layer before our main scanner picks it up

---

## Security

- **PostgreSQL: SSL in production** — dev uses plain password auth, prod needs `sslmode=require` + certificate
- **Exchange API keys encryption** — when Sprint 7 connects accounts, keys must be encrypted at rest (AES-256 or libsodium) before storing in DB, never in plaintext
- **DB credentials rotation** — env-var based now; move to SOPS + age (Sprint 8) or AWS Secrets Manager on ECS
- **No direct DB access from web** — all DB reads go through api-gateway, PostgreSQL port never exposed publicly
- **Rate limiting on API** — add per-IP rate limiting to api-gateway before going public (Sprint 8)
- **CodeQL + Semgrep in CI** — static analysis for SQL injection, secrets in code (Sprint 8)

## Technical debt / optimization

**Token detail page (known issues + improvements)**

- ~~**retrace_from_peak scoring is inverted**~~ — fixed: near peak now scores 2 pts, far below peak scores 0 pts
- **Short Readiness card only shows short perspective** — same OI + funding + pump age data is useful for longs (e.g. low funding + OI growing = accumulation) and for long-term investment framing; consider a "Bias" toggle (Short / Long / Neutral) or a separate section that interprets data for each direction
- **Token page nav label** — active nav item shows "Pump Scanner" when viewing a token; should be a breadcrumb `Pump Scanner / BTC` or a separate "Token" entry so the user knows where they are
- **OHLCV chart is static** — loaded once on page open; add optional auto-refresh (e.g. every 60s for the active candle) so the current candle updates while the user is watching
- **Pump episode markers on chart** — `first_seen_at` and `closed_at` are already available per episode; overlay flag markers on the OHLCV chart so the user can see "we detected the pump here" and "episode closed here" → visualises what happened after detection; lightweight-charts supports `series.setMarkers()`
- **Chart history limited to exchange lookback** — OHLCV is fetched live from the exchange on each request; no historical data is stored; to show older pump episodes on the chart, start writing OHLCV candles to TimescaleDB (continuous storage) so the chart can go back months
- **Real-time OI on token page** — OI is fetched once on load; add polling every 30-60s so the user sees OI moving while watching a pump
- **Live prices on pump scanner** — scanner prices come from Redis cache updated every ~120s; frontend can poll `/api/pumps` every 30s for a near-live feel without websocket complexity
- **Pump scanner pagination** — API returns up to 500 rows, all rendered at once; add pagination or virtual scrolling for when the list grows

**Frontend bugs (do before Sprint 4)**

- **Scrollbar layout shift** — switching between Status and Pump Scanner tabs causes the scrollbar to appear/disappear, shifting content width; fix with `scrollbar-gutter: stable` on `<html>` in `apps/web/src/index.css`
- **Charts not always rendering** — OHLCV fetch silently fails for some tokens/exchanges showing "Chart unavailable"; root cause: `pickExchange` only supports binance/bybit/okx/gate; tokens traded exclusively on bingx/mexc get 404; fix: add `fetchBingX`/`fetchMEXC` to ohlcv.go, replace single-pick with sequential fallback ordered by per-exchange volume (highest first); if an exchange returns fewer candles than threshold (e.g. <20), try next — this naturally handles new Binance listings where full history lives on a smaller exchange
- **Interval switch re-renders full page** — changing candle interval re-fetches pump + history alongside OHLCV; split `useEffect` so pump+history only re-runs on `[base]`, OHLCV on `[base, chartInterval]`; also fix chart container height: should always be `h-[380px]` regardless of loaded state so the block doesn't shrink when showing "Chart unavailable"
- **Candle period label** — each interval covers a different time window (5m→24h, 15m→48h, 1h→8d, 4h→30d) but nothing shows this in the UI; add a label like `"15m · last 48h"` near the selector
- **Locale in dates and chart axis** — `fmtTs()` uses `toLocaleString(undefined, ...)` which picks browser locale, showing dates in Russian/Polish; replace `undefined` with `'en-US'`; also pass `localization: { locale: 'en-US' }` to lightweight-charts `createChart()`

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

## Token Risk Profile (future sprint)

_Goal: for each token, answer "how much leverage is safe and what is the real cost of holding?"_

A great signal with wrong leverage = blown account. This module sits between signal detection and execution.

**Inputs already available:**

- `pump_event_snapshots` at +1h/+4h/+24h → proxy for Maximum Adverse Excursion (MAE)
- `funding_rate_snapshots` → daily carry cost at given leverage
- `pump_events.exchanges` JSONB → 24h volume as liquidity proxy
- `retrace_pct` per episode → historical win rate and avg magnitude

**Inputs still missing (need OHLCV storage first):**

- ATR / volatility — how much does this token move per candle on average
- Max intraday wick — worst-case spike that could trigger stop/liquidation before the retrace

**What the module should produce:**

```
Token Risk Profile — SOL

Risk rating: HIGH
Max recommended leverage: 2x–3x

Factors:
- Historical MAE: up to +28% adverse before retrace started
  → 3x short gets liquidated at +33% → safety margin is thin
- Funding drag: 0.14%/8h → 5-day hold costs 6.3% of notional
- Liquidity: $380M/24h volume → entry/exit realistic at normal size

Position sizing guidance:
- At 2x leverage: survives up to +40% adverse move
- Suggested SL: +18% above entry (above historical MAE 95th percentile)
- Historical base rate: 6 past episodes above 50% → 5 retraced >30% in 4h (83% hit TP)
```

**Components to build:**

- [ ] MAE calculator: per token, per pump magnitude bucket (30-50%, 50-100%, >100%), compute p50/p75/p95 of adverse excursion from peak using existing snapshots
- [ ] Funding drag calculator: given leverage N and funding rate R, show cost per day / per week
- [ ] Leverage suggestion: `max_safe_leverage = (liquidation_buffer) / (MAE_p95)` — e.g. MAE p95 = 28%, want 20% margin → max leverage = 1/0.48 ≈ 2x
- [ ] Historical base rate: for this token at this pump magnitude, how often did it retrace >X% within Y hours
- [ ] Risk rating (Low / Medium / High / Extreme) from composite of volatility + MAE + funding + liquidity
- [ ] Display on token detail page as "Risk Profile" card alongside Short Readiness

**Kelly criterion (longer term):**
After accumulating real trade P&L — compute optimal fraction of capital per trade as `f = (win_rate × avg_win - loss_rate × avg_loss) / avg_win`. Prevents both over-betting (ruin) and under-betting (missed edge).

---

## Backlog (no sprint yet)

- Tokenized assets (stocks/metals on Bybit/OKX) — separate scanner filter, same ccxt fetch
- ECS migration (after proving out on EC2)
- Paper trading / shadow mode framework
- Replay / backtesting harness — given a past pump event, show what optimal entry/exit would have been; validate score thresholds against accumulated history
- Backtesting on own data — after 2-3 months of retrace snapshots (+1h/+4h/+24h), compute: at which score thresholds was win rate >60%? which OI+funding combinations predicted retrace >30%?
- Portfolio risk budget engine (per exchange / correlated basket)
- Real-time correlation matrix — if 5 open shorts all correlate 0.9 with BTC, that is one position at 5x size, not 5 independent positions; cap exposure by correlation-adjusted notional
- Multi-exchange capital management (Treasury module)
- Polymarket CLOB integration
- Meme stock / short squeeze scanner — same mechanics as crypto pump: short interest + OI spike + price move = squeeze setup; applicable to GME-type events via Alpaca or IBKR API; lower priority than crypto but reuses all existing signal logic
- Weighted social sentiment — sentiment score adjusted by source influence (1 tweet from an account followed by 50 known whales > 10,000 bot tweets); raw tweet volume is noise
- Toxic flow detection — identify wallet/API-key patterns that consistently trade against market makers and win; if such a counterparty is on the other side of your signal, reconsider the trade
- Cross-exchange arbitrage gaps — if Binance and Coinbase spot prices diverge >0.3% beyond normal basis, one market has not yet priced in a news event; information asymmetry window
