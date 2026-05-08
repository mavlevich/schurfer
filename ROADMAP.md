# Roadmap

> Living document. Updated as we progress.

## Sprint 1: Foundation (current)

- [x] Decisions: monorepo, Go workspaces, frontend stack
- [x] Repo init, structure, ADRs
- [ ] Hetzner Tokyo VPS provisioned
- [ ] Docker Compose for local dev (Postgres + Timescale + Redis + NATS)
- [ ] CI: self-hosted runner on VPS
- [ ] First Go collector skeleton (Binance perp BTCUSDT)
- [ ] Trade Journal model in Postgres + migrations
- [ ] Telegram bot skeleton

## Sprint 2: First strategy + alerts

- [ ] Binance perp full collector (top 30 symbols)
- [ ] Funding rate cross-exchange comparator
- [ ] Pump detector v1 (price + volume)
- [ ] Telegram alerts with approve/skip buttons
- [ ] Paper trading framework

## Sprint 3: Multi-exchange + Bybit

- [ ] Bybit collector
- [ ] Hyperliquid collector
- [ ] OKX collector
- [ ] OI integration into pump detector
- [ ] Composite "sticky pump" signal

## Sprint 4: Pump-short live

- [ ] Risk manager with all guardrails
- [ ] Execution engine (Go)
- [ ] Position tracking
- [ ] Pump-short v1 paper → live (small size)
- [ ] Dashboard v0: equity curve + per-strategy stats

## Sprint 5: News pipeline

- [ ] CryptoPanic + RSS sources
- [ ] Telegram channel parsing (Telethon)
- [ ] Two-stage AI scoring (Groq Llama → Gemini → Claude)
- [ ] News-based alerts (manual approve only)

## Sprint 6: Smart money + Polymarket

- [ ] Smart money tracker for Solana (Helius)
- [ ] Polymarket CLOB integration
- [ ] CEX-Polymarket lag arbitrage detector
- [ ] Polymarket "No bot" baseline

## Sprint 7+: Advanced

- [ ] Pre-launch short detector (TGE-aware)
- [ ] MM history database (DWF, Wintermute patterns)
- [ ] Theme hunter (off-CEX memecoins)
- [ ] Public AlphaScope-style read-only views (if monetization desired)

## Technical debt / continuous

- [ ] Property-based tests for math
- [ ] Replay engine for backtests
- [ ] Daily reconciliation (code vs exchange)
- [ ] Monitoring (Grafana + Prometheus)
- [ ] Secrets management (sops + age)
