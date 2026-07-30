# Bybit public-trades pilot v1

Status: registered discovery contract
Capture contract: `bybit_orderflow_pilot_v1`
Report contract: `bybit_orderflow_pilot_report_v1`
Cohort start: `2026-07-30T18:15:00Z`

## Question

Does point-in-time public trade flow around a pump contain useful information before
the current pump trigger, or improve the timing of an existing short after the
trigger?

This pilot has three separate discovery lanes:

1. `early_long`: buying pressure observed before the current trigger and subsequent
   long return;
2. `squeeze_avoidance`: late buying pressure and adverse price movement for an
   immediate short;
3. `delayed_short`: fading buying pressure and subsequent short return.

The lanes are separate books. They must not be combined into one headline,
position, or recommendation.

## Point-in-time capture

The Go collector observes every active Bybit linear perpetual from process start and
keeps a sparse, non-empty one-second prebuffer in memory. An event is accepted only
after the full 30-minute prebuffer is available for its symbol. The activation
second is excluded. Future records begin with the next complete second.

Each capture contains:

- one event symbol;
- three controls selected at activation time from prior 30-minute notional and
  price return;
- 30 minutes before and 60 minutes after `first_observed_at`;
- exchange event time and local receive time;
- buy/sell notional, quantities, trade counts, OHLC, and maximum observed lag.

Raw trades are not retained and do not enter NATS, PostgreSQL, or the trading
decision path. Only bounded event and matched-control windows are stored.

## Frozen features and windows

Pre-trigger windows:

- `30m_to_15m`: `[-30m, -15m)`;
- `15m_to_5m`: `[-15m, -5m)`;
- `5m_to_1m`: `[-5m, -1m)`;
- `1m_to_trigger`: `[-1m, 0)`.

For each window the report calculates buy and sell notional, notional imbalance,
trade-count imbalance, wall-clock notional per second, within-window price return,
and the remaining move from the window end to the last complete pre-trigger price.

Post-trigger horizons are 1, 5, 15, and 60 minutes. The price anchor is the last
complete pre-trigger bucket. Both anchor and horizon endpoint must be no more than
five seconds stale. Missing or stale endpoints remain unresolved rather than using a
later price.

The event feature is compared with the median of its point-in-time matched controls.
The report uses rank correlation only as a descriptive association:

- early-long correlates pre-trigger imbalance lift with long return from the end of
  that feature window through the registered post-trigger horizon;
- squeeze-avoidance correlates the final one-minute imbalance lift with post-trigger
  long return and reports how often a positive feature preceded an adverse move for
  an immediate short;
- delayed-short defines exhaustion as the event-minus-control difference in
  `5m_to_1m imbalance - 1m_to_trigger imbalance` and relates it to signed short
  return at 15 and 60 minutes.

Every lane reports asset-cluster concentration plus the weakest rank correlation
after separately excluding each asset and each UTC market day. These are robustness
diagnostics, not multiplicity-adjusted formal inference.

No threshold, fee model, fill assumption, leverage, or position size is selected by
this report.

## Readiness and interpretation

The report remains `collecting` until it has at least:

- 100 fully mature event/control captures;
- 30 distinct event bases;
- 7 distinct UTC market days;
- one event plus all three controls with non-empty registered pre-windows and
  non-stale endpoints at every registered horizon.

Before the gate, output is capture QA only. After the gate, associations are still
discovery evidence, not a trading rule. Any proposed threshold or executable
strategy requires a separate pre-registered, untouched forward cohort.

The report fails closed for unknown contract versions, mixed identities, duplicate
or non-monotonic buckets, activation-boundary leakage, invalid numeric fields, and
capture windows that differ from the registered one-hour contract. Its input
fingerprint is computed while files are streamed and is independent of the absolute
storage path.

## Expansion and stop gates

Do not add Binance, L2 books, long-term raw storage, paid data, or live execution
from this cohort alone.

The lane earns a Binance replication only if it shows useful lead time, stable
point-in-time lift across assets and market days, and an economically plausible
effect after later execution costs. Stop the lane if the association exists only
after the current trigger, depends on one asset cluster or market day, or fails on
the next untouched cohort.
