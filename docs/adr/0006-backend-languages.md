# ADR-0006: Go + Python for backend, Rust only if needed

Date: 2026-05-08
Status: Superseded

Supersession note: the project still uses Go and Python, but the service allocation
changed: execution is Python and notifier is Go. Current executable boundaries are
defined by the service entrypoints and production Compose file. The original decision
below is retained unchanged as history.

## Context

Need languages for different service types: networking-heavy (collectors),
order execution, signal generation, ML/data analysis.

## Decision

- **Go** for all networking services (collectors, execution, api-gateway)
- **Python** for analytics, news pipeline, telegram bot
- **Rust** - NOT using now. Will add selectively if we reach
  latency-critical territory (MEV, sub-millisecond arbitrage)

## Rationale

- Go: goroutines are ideal for thousands of WebSocket connections,
  simple deployment, already in the work stack at Bayer
- Python: ML/data ecosystem (pandas, numpy, scikit-learn) is irreplaceable
- Rust: real edge only in latency-critical tasks. Schurfer is not there.
  Adding Rust would delay time-to-market by months.

## Consequences

- Pro: focus on two languages (Go+Python) speeds up development
- Pro: Go skills grow in parallel with day job
- Con: will hit Go GC pauses if HFT ever becomes a goal, then Rust
- Revisit: when developing MEV/sniper modules or sub-ms arbitrage
