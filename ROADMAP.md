# Roadmap

> Living document. Updated as we progress.

## Principles

- Ship working product fast, iterate later
- Architecture right from day one (service boundaries, NATS contracts), code can be dirty inside
- Tests and structured logging everywhere from Sprint 1
- UI progress on every sprint (Telegram + web)
- Go for hot path (collectors, execution); Python for analytics/ML
- Bybit as first exchange (Binance perps blocked in Poland)

## Sprint 1: Foundation (current)

- [x] Decisions: monorepo, Go workspaces, frontend stack
- [x] Repo init, structure, ADRs
- [ ] Docker Compose for local dev (Postgres + TimescaleDB + Redis + NATS)
- [ ] Structured logging standard (Python: structlog, Go: slog)
- [ ] Trade Journal schema + migrations (core layer, ADR-0007)
- [ ] AWS EC2 t4g.medium Frankfurt provisioned
- [ ] Domain + Cloudflare DNS + Tunnel
- [ ] Tailscale for SSH access
- [ ] CI: GitHub Actions self-hosted runner (spot instance)
- [ ] Web scaffold: Vite + React + shadcn/ui, System Status page

## Sprint 2: First vertical slice

- [x] Bybit WS collector (Go, publishes to NATS)
- [ ] NATS message format spec (contract for all downstream consumers)
- [ ] Pump detector v1 (price_change > 50%, near peak)
- [ ] Telegram bot: alert with approve/skip buttons
- [ ] Web: trade journal table (read from Postgres)
- [ ] Tests: detector logic, journal writes

## Sprint 3: Execution + live trading

- [ ] Execution: approve -> ccxt places short on Bybit
- [ ] Position tracking in journal
- [ ] Funding rate filter in detector
- [ ] Web: live positions, prices via WS, PnL view
- [ ] Deploy to AWS EC2 (Docker Compose on VPS)

## Sprint 4: Second exchange + charts

- [ ] Second exchange collector (OKX or Hyperliquid)
- [ ] Web: equity curve (Lightweight Charts), per-strategy stats
- [ ] Second exchange collector (OKX or Hyperliquid)
- [ ] OI integration into pump detector

## Sprint 5: ECS migration + news pipeline

- [ ] Migrate Docker Compose -> AWS ECS (EC2 launch type)
- [ ] ECR for Docker images, CloudWatch for logs
- [ ] CryptoPanic + RSS news sources
- [ ] Two-stage AI scoring (Groq Llama -> Claude)
- [ ] News-based alerts (manual approve only)

## Sprint 6: Multi-exchange + advanced signals

- [ ] Bybit + OKX + Hyperliquid full coverage
- [ ] Composite "sticky pump" signal (price + volume + OI + funding)
- [ ] Risk manager with guardrails
- [ ] Paper trading framework

## Sprint 7+: Advanced

- [ ] Smart money tracker for Solana (Helius)
- [ ] Polymarket CLOB integration
- [ ] CEX-Polymarket lag arbitrage detector
- [ ] Pre-launch short detector (TGE-aware)
- [ ] MM history database (DWF, Wintermute patterns)

## Future ideas (backlog)

### Detectors

- [ ] Investigator-based short detector (ZachXBT, MetaSleuth, peckshield)
- [ ] Pre-launch low-float VC token short detector (TGE-aware)
- [ ] Theme hunter (off-CEX memecoins, hype-driven)

### Strategy modes

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
- [ ] Multi-exchange capital management (Treasury module)
- [ ] Cross-venue execution (best venue picker)

### Security & infra

- [ ] CodeQL + Semgrep in CI
- [ ] SOPS + age for secrets
- [ ] Reserved Instance / Savings Plan after 1 month usage data

### Tax / compliance

- [ ] Trade journal tax export module (PIT-38 ready CSV)

## Technical debt / continuous

- [ ] Property-based tests for math
- [ ] Replay engine for backtests
- [ ] Daily reconciliation (code vs exchange)
- [ ] Monitoring (Grafana + Prometheus / CloudWatch)
- [ ] Secrets management (sops + age)
