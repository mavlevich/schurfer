# HYP-024 order-flow microstructure -- result stub (NOT YET RUN)

**Status: placeholder.** This document records the result of the frozen
pre-registration in
[orderflow-microstructure-v1.md](orderflow-microstructure-v1.md). Every number
below is a `TODO` to be copied verbatim from the report output. Nothing here is
filled in yet, because the report has not been run against production data in
this change (the implementation was written and unit/integration tested
without production access). Do not fabricate any figure: run the report, then
transcribe.

## How to produce the numbers

Read-only, against prod via the SSH tunnel (does not restart the analytics
service):

```
make prod-hyp-024-orderflow-report ARGS="--format=json"
```

Local (against a tunnelled or local `DATABASE_URL`):

```
make hyp-024-orderflow-report ARGS="--format=json"
```

`cohort_start` is frozen at `2026-08-10`; `--cohort-end` defaults to the
held-out boundary `2026-08-25` and is refused past it, so this discovery pass
can never read the held-out window. A `candidate` verdict earns a read of the
held-out window only through a later, separately registered pass.

## Provenance to record (copy from the report header)

| Field                               | Value                                         |
| ----------------------------------- | --------------------------------------------- |
| report_version                      | `orderflow_microstructure_v1`                 |
| strategy_version                    | `pump_short_v1_market_quality`                |
| resolver_version / horizon          | `forward_v1` / 60m                            |
| capture_version (pinned)            | `v1`                                          |
| cost_model_version                  | `conservative_costs_v1`                       |
| cost deduction (net = gross - this) | `0.20625%` (2x10bps taker + 5bps/8h x 60/480) |
| cohort_start / cohort_end           | `2026-08-10` / `TODO`                         |
| db_snapshot_at                      | `TODO`                                        |
| dataset_fingerprint                 | `TODO`                                        |
| code_revision / formal_run          | `TODO` / `TODO`                               |

## Coverage (copy from the coverage funnel and per-exchange table)

The measure is only defined where the bars exist (bybit from 2026-08-10,
binance from 2026-08-15). Decisions on venues without bars, decisions whose
`base` could not be resolved to exactly one native market at decision time, and
decisions without a complete ten-bar pre-window are all **coverage loss**, not
negative outcomes.

| Coverage step                                | Count  |
| -------------------------------------------- | ------ |
| Cohort decisions with a resolved 60m outcome | `TODO` |
| Identity resolved to a single native market  | `TODO` |
| Complete ten-bar pre-window                  | `TODO` |
| Measured episodes                            | `TODO` |

Per exchange: `TODO` (unresolved / ambiguous identity, missing bars, measured).

## Primary metric (registered ten-minute taker imbalance)

| Quintile               | Episodes | Clusters | Median net short return |
| ---------------------- | -------- | -------- | ----------------------- |
| Q1 (lowest imbalance)  | `TODO`   | `TODO`   | `TODO`                  |
| Q2                     | `TODO`   | `TODO`   | `TODO`                  |
| Q3                     | `TODO`   | `TODO`   | `TODO`                  |
| Q4                     | `TODO`   | `TODO`   | `TODO`                  |
| Q5 (highest imbalance) | `TODO`   | `TODO`   | `TODO`                  |

- Top-minus-bottom median net spread: `TODO` pp
- Monotone across all five quintiles: `TODO`
- Rule 6: distinct feature values `TODO`, largest tied group `TODO`, adjacent
  boundaries distinct `TODO`
- Compared-quintile episode floor met (>=150 each): `TODO`
- Compared-quintile asset-cluster floor met (>=30 union): `TODO`

## Secondary context (never replaces the registered measure)

- Median MFE / MAE per quintile: `TODO`
- Five-minute lookback top-minus-bottom spread: `TODO` pp
- Twenty-minute lookback top-minus-bottom spread: `TODO` pp

The alternative lookbacks are context only: they show whether any relationship
is a knife edge. Reaching for them if the ten-minute measure fails is a new
hypothesis with a new id and an untouched window, not a refinement here.

## Verdict

`TODO` (one of `candidate` / `rejected` / `inconclusive`), with the report's
own `reasons`. Per the frozen decision rule, the sufficiency floor binds first
and can only ever produce `inconclusive` -- the bars start on 2026-08-10, so
this floor is the one most likely to bind. A `rejected` verdict is reachable
only above the floor.

## Assumptions a human should confirm before trusting the numbers

1. **Cost model.** `short_return_pct` from the forward resolver is a raw price-
   path return; the report subtracts the shared `conservative_costs_v1` fee +
   funding deduction (0.20625 pp at 60m). Slippage is not subtracted because
   the forward-outcome path carries no fills or order-book depth. Because the
   deduction is a constant at this fixed horizon, it cancels out of the
   top-minus-bottom spread and only shifts the absolute per-quintile levels.
2. **Asset cluster = `base`.** The diversity floor counts the exchange-
   independent `base` as the asset cluster (so the same asset on bybit and
   binance is one cluster). Confirm this matches the family-rules intent of
   "asset clusters" for pump_short.
3. **capture_version pin = `v1` for both venues.** If a venue were captured
   under a different contract, its bars fall out as visible per-exchange
   coverage loss (never a wrong number); confirm binance is on `v1` from the
   per-exchange coverage row rather than assuming it.
4. **Point-in-time identity via the momentum-universe snapshot at or before the
   decision `ts`.** Ambiguous (`base` -> more than one native market) and
   unresolved identities fail closed as coverage loss.
