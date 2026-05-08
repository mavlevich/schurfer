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

## Future ideas (not in current sprint)

### Detectors

- [ ] Investigator-based short detector (ZachXBT, MetaSleuth, peckshield)
- [ ] Pre-launch low-float VC token short detector (TGE-aware)
- [ ] MM history database (DWF, Wintermute pattern matching)
- [ ] Composite "sticky pump" signal (price + volume + OI + funding)
- [ ] Theme hunter (off-CEX memecoins, hype-driven)

### Strategy modes (cross-cutting)

- [ ] Paper mode framework
- [ ] Shadow mode framework
- [ ] Live micro mode framework (small position sizes)

### Research & validation

- [ ] Signal contract v1 (market event -> feature snapshot -> signal -> order intent -> journal entry)
- [ ] Replay / simulation harness with parity across paper, shadow, and live micro
- [ ] Detector scorecard (hit rate, MFE/MAE, holding cost, funding drag, time-to-reversion)

### Risk & execution

- [ ] Portfolio risk budget engine (per exchange / theme / liquidity bucket / correlated basket)
- [ ] Execution quality layer (slippage, partial fills, rejects, latency by venue)

### Operational features

- [ ] Task orchestrator (runtime tasks: "watch X for short", "wait for liquidity")
  - Web UI to view, pause, cancel tasks
- [ ] Multi-exchange capital management (Treasury module)
  - Auto-suggest rebalance between Bybit / Hyperliquid
- [ ] Cross-venue execution (best venue picker)

### Security & infra

- [ ] CodeQL + Semgrep in CI
- [ ] SOPS + age for secrets
- [ ] Self-hosted GitHub Actions runner on VPS

### Tax / compliance

- [ ] Trade journal tax export module (PIT-38 ready CSV)

## Technical debt / continuous

- [ ] Property-based tests for math
- [ ] Replay engine for backtests
- [ ] Daily reconciliation (code vs exchange)
- [ ] Monitoring (Grafana + Prometheus)
- [ ] Secrets management (sops + age)
