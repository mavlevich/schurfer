# Momentum flow paper v1

`momentum_flow_paper_v1` is a prospective, unlevered paper-long probe attached to
the frozen `momentum_flow_watch_v1` decision stream. It records whether a WATCH could
have produced an executable entry on the same Bybit linear USDT market and measures
bounded, after-cost outcomes. It never sends a real order and does not change the
production pump-short strategy.

`momentum_flow_paper_v1_lev3` is a sibling sizing variant, not a venue expansion: the
same live Bybit WATCH signal, the same $50 of real capital committed per probe, but
the simulated position is sized at 3x that ($150 notional) instead of 1x. See
"Sizing variant: lev3" below.

## Frozen contract

| Field                        |                                                    Value |
| ---------------------------- | -------------------------------------------------------: |
| Direction                    |                                                     long |
| Venue and market             |   exact Bybit linear USDT perpetual market ID from WATCH |
| Position notional            |                                                      $50 |
| Leverage                     |                                                       1x |
| Maximum WATCH-to-quote delay |                                               30 seconds |
| Entry                        |          executable ask VWAP across up to 50 book levels |
| Stop loss                    |                         -5% gross return from entry VWAP |
| Maximum hold                 |                                              240 minutes |
| Outcome horizons             |                          5, 15, 30, 60, 120, 240 minutes |
| Exit and outcomes            |          executable bid VWAP across up to 50 book levels |
| Outcome quote grace          |                           60 seconds after each due time |
| Cost model                   | conservative fees and funding; no extra modeled slippage |

The executable VWAP already includes the observed spread and visible book impact.
Adding modeled slippage to that price would double count those costs. The accounting
layer still applies the project's conservative fee and funding model.

The canonical JSON is protected by `PAPER_CONTRACT_SHA256`. Any numerical or semantic
change requires a new paper version and a new prospective cohort.

## Point-in-time and failure rules

The worker claims a WATCH in Postgres before requesting the entry quote. This ordering
is intentional because an order book response cannot be reconstructed later.

- A WATCH older than 30 seconds is recorded as `rejected_stale`.
- A missing, timed-out, ambiguous, or insufficient book is `rejected_quote`.
- A crash after claim but before durable quote storage is
  `unresolved_interrupted` on restart. It is never converted into a later fill.
- Horizon rows are created with the entry, so a missed exit quote remains in the
  denominator as `missed_deadline`.
- A max-hold exit that cannot be quoted before its deadline becomes
  `exit_unresolved` rather than a fabricated close.
- Only one worker may own a paper version. A session-scoped Postgres advisory lock
  prevents concurrent writers for the same contract.

All entry and exit observations preserve requested, observed, and exchange event
timestamps; best bid and ask; mid; spread; VWAP; impact; filled notional; exact market
identity; and accounting results.

## Runtime

Start the worker explicitly after WATCH is running:

```bash
make prod-momentum-paper-start
make prod-momentum-paper-health
```

Health is published at `market:momentumpaper:health:<paper_version>` (scoped per
contract since `feat/binance-momentum-paper-v1`, so a second venue's own worker can
never overwrite this one's snapshot -- see `momentum_flow_paper_worker.health_key`'s
own doc comment), `market:momentumpaper:health:momentum_flow_paper_v1` for the live
Bybit worker specifically. The worker has its own Compose profile and resource limit,
so it is not part of the normal production deployment. Stopping it does not stop
capture, WATCH evaluation, the pump scanner, or execution.

## Sizing variant: lev3

`momentum_flow_paper_v1_lev3` (`LEVERAGED_PAPER_CONTRACT`) reuses every threshold in
the frozen contract table above verbatim -- same `watch_version`, same stop, hold
window, outcome horizons, timing bounds, and cost model -- except:

| Field                | `momentum_flow_paper_v1` | `momentum_flow_paper_v1_lev3` |
| -------------------- | -----------------------: | ----------------------------: |
| Position notional    |                      $50 |                          $150 |
| Leverage             |                       1x |                            3x |
| Real capital at risk |                      $50 |                           $50 |

Real capital at risk (`MARGIN_USD`) is a project-wide constant enforced by
`PaperContract.__post_init__`: `position_notional_usd` must equal `MARGIN_USD x
leverage`, so a future sibling contract cannot silently commit more real capital than
`MARGIN_USD` while only appearing to change a "leverage" label. Fees, funding, and P&L
are all computed on the position's own notional (not the margin), so this is not a
scaled replay of the leverage=1 contract's own results: a $150 simulated fill walks
further into the real order book than a $50 one, so `entry_impact_bps` and
`entry_spread_bps` genuinely differ, not just the dollar P&L.

Its own `paper_version` keeps its worker lock, `_runs` row, and Redis health key
(`market:momentumpaper:health:momentum_flow_paper_v1_lev3`) fully isolated from the
leverage=1 Bybit worker, so both independently claim and probe every WATCH decision
the live Bybit WATCH worker produces. Own Compose profile and resource limit, same as
every other sibling worker:

```bash
make prod-momentum-paper-lev3-start
make prod-momentum-paper-lev3-health
```

## Interpretation

This is discovery instrumentation, not a promotion verdict. It measures realized
WATCH capacity, entry latency, executable costs, MFE/MAE, bounded horizon returns,
and stop/max-hold economics on an untouched forward cohort. Decisions about a
strategy require the separate episode study with pumps, matched controls, opportunity
rate, false-WATCH rate, concentration checks, and the registered family correction.
