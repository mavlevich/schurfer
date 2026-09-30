# Research options after the pre-blind book-cost read

Status: discussion inventory recorded 2026-10-01, **not a hypothesis
registration, result, authorization to collect a new feed, or trading approval**.
The [book-cost readout](preblind-book-cost-baseline-v1-readout.md) has already
been viewed, as have the earlier pump studies. Choices below are consequently
ideas shaped by viewed data. A later test needs its own frozen rule and untouched
cohort. This note does not change HYP-012 v2, HYP-015, the HYP-012 v2 blind
window, or their formal reads.

## Evidence that constrains the options

- The pre-blind read measures $50 book quotes and hypothetical fee scenarios,
  not fills or strategy returns. Fresh Bybit paper quotes had mean required
  midpoint moves of about 16-17, 23 and 35-36 bps across the registered
  `<5`, `[5,20)` and `[20,50)` bps spread buckets at 5.5 bps per side. It
  cannot price a $500 or $5,000 order from historical depth.
- The [MEXC pre-move data audit](mexc-pre-move-data-feasibility-v1.md) found
  one-minute OHLCV and turnover, but no historical source trade tape, full-
  universe point-in-time OI/funding or pre-move depth. Pump-selected samples
  cannot supply quiet-minute controls. HYP-029's registered 5-minute price
  trigger failed; the audit does not reopen it.
- [HYP-024](orderflow-microstructure-v1-result.md) tested a registered
  ten-minute taker-imbalance measure on resolved venues and was inconclusive
  with 89 measured episodes. The executable pump-short line was net negative.
  These results do not prove that every pre-move or reversal feature fails,
  but they rule out presenting another scan of the same viewed paths as
  independent confirmation.

## Options and first admissible decisions

| Option                                            | Evidence gap and first step                                                                                                                                                                                                                                                                                                                                                                                                | Stop or advance rule                                                                                                                                                                                                                                                           |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Source-venue pre-move trades, OI and depth        | At the 2026-10-31 reassessment, consider **one bounded prospective data canary** on one source venue. Select instruments without conditioning on later pumps; capture quiet minutes, native payload, event and receive times, trade-side semantics, OI units, sequence gaps, and a complete eligible denominator. The canary first tests whether these fields are observable before movement, not whether they make money. | Advance to one separately registered economic hypothesis only if pre-move coverage, source-to-detection latency, exact execution identity, book capacity and server/storage budget are adequate. Otherwise stop this line. A WebSocket transport alone is no evidence of edge. |
| Same-venue buy pressure or short-horizon reversal | Existing Bybit bars permit exploratory feature work, but previous windows and nearby signal families have been viewed. A reversal must define its peak and lower high causally at the decision time; a final episode peak is future information.                                                                                                                                                                           | Do not tune volume multiples, stops or hold times on the viewed archive and call the best cell evidence. Nominate at most one rule for an untouched cohort after an explicit power and cost calculation, or park it.                                                           |
| Listing and other external catalysts              | Inventory announcement, listing-open, first-observed and delisting timestamps separately, with source archives and publication times. Third-party alerts may suggest an event source but do not establish when a trader could have acted or that a ticker matched the tradable contract.                                                                                                                                   | Proceed only with point-in-time event coverage, exact identity, a quiet-event denominator and a registered entry/exit rule. No direct Telegram-to-order path.                                                                                                                  |
| Spot/perpetual funding carry                      | The read-only feasibility line needs executable sizes on both legs, four book crossings, actual funding direction and schedule, borrow cost when short spot is needed, account-margin behavior and liquidation stress. The sign and magnitude of future funding are not locked by an observed rate.                                                                                                                        | Run its already bounded feasibility canary no earlier than its registered gate. Stop if legs, margin or total costs fail; only then consider a prospective net-carry cohort. No guaranteed yield assumption.                                                                   |
| Execution quality and larger size                 | The $50 historical quote summaries lack raw depth for repricing at $500/$5,000, maker fill probabilities, latency and adverse selection.                                                                                                                                                                                                                                                                                   | Capture prospective raw books and, if later authorized, actual fills at registered sizes. Report non-executable orders and tail costs; do not extrapolate a size curve from $50.                                                                                               |

## Engineering and longer horizon

DuckDB and Parquet already serve offline research. Polars, ConnectorX,
msgspec, alternate web servers, database drivers and message buses are
**conditional tools**, not a strategy plan. Try one only after profiling a
specific workload and defining a before/after wall-time, memory, latency and
semantic-equivalence gate. Faster iteration also increases the risk of
selecting chance results if experiments are not registered and counted.

The near-term sequence remains: finish the registered cost readout, maintain
the active cohorts, perform their allowed diagnostics and formal reads, then
make the 2026-10-31 research-slot decision. That date is a reassessment, not
automatic permission to narrow the blind window or start a feed. A future
registration should include effect size above the measured break-even cost,
cluster-aware power, expected calendar time, capital occupancy and a stopping
rule. Live probes, larger capital, multiple strategies, dedicated execution
hosts and data products are conditional milestones after credible standalone
after-cost evidence and operational reliability; the calendar and proposed
capital or annual-return figures are not forecasts.
