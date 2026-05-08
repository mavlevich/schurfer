# ADR-0006: Go + Python для backend, Rust только точечно

Date: 2026-05-08
Status: Accepted

## Context

Нужны языки для разных типов сервисов: networking-heavy (collectors),
order execution, signal generation, ML/data analysis.

## Decision

- **Go** для всех networking сервисов (collectors, execution, api-gateway)
- **Python** для analytics, news pipeline, telegram bot
- **Rust** - НЕ используем сейчас. Добавим точечно если дойдём
  до latency-critical (MEV, sub-millisecond arbitrage)

## Rationale

- Go: goroutines идеальны для тысяч WebSocket connections, простой
  deployment, хорошо учится, на Bayer уже в стеке
- Python: ML/data ecosystem (pandas, numpy, scikit-learn) - не заменишь
- Rust: real edge только в latency-critical задачах. Schurfer не там.
  Добавление Rust удлинит time-to-market на месяцы.

## Consequences

- Pro: фокус на двух языках (Go+Python) ускоряет development
- Pro: знание Go растёт параллельно с работой на Bayer
- Con: упрёмся в Go GC pauses если когда-то пойдём в HFT - тогда Rust
- Revisit: при разработке MEV/sniper модулей или sub-ms арбитража
