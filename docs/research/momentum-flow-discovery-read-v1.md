# Momentum-flow discovery read v1

Status: implemented, collecting. This is a descriptive read of the frozen
`momentum_flow_watch_v1` and `momentum_flow_paper_v1` contracts on Bybit and their
separately-versioned Binance counterparts. It does not alter execution or send an
exchange request.

## Scope and interpretation

The report reads registered WATCH runs, minute evaluations, same-venue pump sources,
and paper probes from PostgreSQL. PostgreSQL aggregates pump-window observability so
the application does not load the complete four-week universe-minute table into
memory. Bybit and Binance remain separate throughout the result.

It reports:

- observable WATCH opportunities per day;
- same-venue WATCH precision, false-WATCH rate, pump recall, and lead time over the
  frozen 240-minute lead horizon;
- executable paper win rate, profit factor, expectancy, MFE/MAE, drawdown, holding
  time, occupancy, and conservative accounting costs;
- WATCH/evaluator and paper quote latency;
- concentration by exact instrument and UTC week;
- WATCH evaluation gaps inferred from durable minute rows.

Missing observations are `null`/`unresolved`, never zero. The frozen $50 quote proves
execution at that size only, so capacity above $50 remains unresolved. BTC regime was
not frozen as an input to this cohort and is likewise unresolved.

## Readiness

Each venue stays `COLLECTING` until all of the following hold:

- registered WATCH and paper hashes match the frozen code contracts;
- at least four distinct UTC weeks are present;
- WATCH minute availability is at least 99%;
- WATCH decisions and at least one completely accounted executable paper probe exist.

Passing these checks produces `READY_FOR_MANUAL_REVIEW`. It does not produce a
strategy verdict. No outcome threshold was registered before this discovery cohort
began, so an automatic `CONTINUE` or `STOP` would be retrospective. A selected
candidate must be frozen and measured on the untouched Confirmation cohort described
in ROADMAP item 9.

## Cohort provenance

`--capture-epoch-started-at` is required and frozen independently at
`MOMENTUM_FLOW_DISCOVERY_COHORT_STATE_PATH` (production default:
`/runtime/momentum-flow-discovery-cohort.json`). It intentionally does not reuse or
rewrite the episode-study cohort file. A newly accepted boundary is persisted only
after the database read and report calculation both succeed.

Run locally:

```bash
make momentum-flow-discovery-report ARGS="--since <UTC> --until <UTC> \
  --capture-epoch-started-at <UTC>"
```

Run against production from the deployment host:

```bash
make prod-momentum-flow-discovery-report ARGS="--since <UTC> --until <UTC> \
  --capture-epoch-started-at <UTC>"
```
