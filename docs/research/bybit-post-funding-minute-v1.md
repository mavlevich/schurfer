# HYP-031: Bybit post-funding reaction, minute variant (protocol v1, draft)

Status: **draft for the owner's review, 2026-10-25.** Nothing is fetched, no price
after any settlement has been read, and this changes no registered rule, cohort or
roadmap order. It becomes binding only when committed unchanged before the first
fetch. The roadmap parks "funding-only short baskets"; this is a different object
(an intraday timing effect around one settlement, flat before the next one), and
opening it is the owner's decision after the 2026-10-31 review, not this document's.

## Question

On Bybit USDT perpetuals, after a settlement on an instrument whose funding is
extremely negative, does a short opened in the first full minute after settlement and
closed 30 minutes later earn enough to clear costs, on public history, out of sample?

## Mechanism, stated before any outcome

A large per-settlement payment rewards closing the paying side just before the
snapshot and reopening it just after. When funding is extremely negative the shorts
pay: they buy to close before settlement and sell to reopen after it, so the price
should drift down shortly after settlement. A data check of 2026-10-25 (no returns
read) found that 96% of settlements with |rate| >= 0.10% are negative, so the
negative side is the primary population. The positive side (longs pay, buy after) is
a diagnostic only.

Competing explanation: extreme negative funding marks a crowded short being squeezed,
and the squeeze continues. The test does not assume either; the control and the
pre-settlement diagnostic below exist to tell them apart.

## Data boundary

- Only Bybit public V5 market data. No Schurfer capture, cohort or result is read.
- **Discovery:** settlements from 2024-10-01T00:00Z to 2025-09-30T23:59Z.
  **Test:** 2025-10-01T00:00Z to 2026-09-28T23:59Z. No request ends after
  2026-09-28T23:59:59.999Z (the carry screen's boundary, clear of the HYP-012 v2
  closed windows); the fetcher refuses a later end before any request.
- **Universe:** every USDT-settled `LinearPerpetual` that traded in the window,
  live (`instruments-info`) and delisted (`instruments-info?status=Closed`, with
  `deliveryTime`). Delisted instruments are fetched up to their delivery; every
  kline `end` is capped at `deliveryTime` (backward paging from a later end returns an
  empty first page).
- **Per instrument:** every settlement from `/v5/market/funding/history`; 60-minute
  klines for the liquidity filter; 1-minute perp klines for each event window
  [t - 60 min, t + 40 min). BTCUSDT 1-minute klines for the market diagnostic.
- Every native response is stored once in a gzip JSON-lines snapshot whose SHA-256
  the report records; `report` reads only that verified snapshot.

## Event definition (fixed now)

For a settlement of instrument i at time t (the actual `fundingRateTimestamp`; the
catalog's current `fundingInterval` is never used, since 147 of 287 checked pairs
changed interval at least once):

- **Trigger:** the rate settled at i's previous settlement, r_prev, is at most
  **-0.10%** per settlement. r_prev was public well before t, so no publication-time
  assumption is needed; the rate settled at t itself is not used for the decision.
- **Liquidity:** perp turnover over the 24 hourly bars ending at t is at least
  **USD 2,000,000**.
- **Entry:** short at the **open of the 1-minute bar starting t + 1 min**.
- **Exit:** at the **close of the bar ending t + 31 min** (30 minutes held). The
  shortest observed interval is 1 hour, so no settlement falls inside the hold and no
  funding is paid or received.
- **Return (gross, bps):** -(exit / entry - 1) x 10,000.
- **Missing data:** a missing entry bar forms no trade, counted by reason. A missing
  exit bar closes at the first later bar up to t + 40 min, flagged; otherwise the
  event is `unresolved`, counted, and never dropped silently.

## Costs

Taker fees 5.5 bps per side (Bybit base tier) plus a **9.5 bps per side** spread and
slippage scenario: **30 bps per round trip** in the primary reading, 50 bps as a
sensitivity. History has trades and closes, not bid and ask, so the report also gives
the **break-even round-trip cost** (mean gross). The spread at settlement moments is
the main unmeasured risk; a pass earns a live quote measurement, not belief.

## Independence and control

- **Cluster = symbol-day.** Extreme funding often moves to 1-hour settlements, so one
  symbol gives many events a day. Each cluster's events are averaged first; the
  statistics are over clusters. Uncertainty: cluster bootstrap, 10,000 resamples,
  seed 31.
- **Control:** settlements in the same window and universe, passing the same
  liquidity filter, with |r_prev| <= 0.01%, the same entry, exit and costs. It
  measures what shorting any settlement earns.

## Reading (fixed now, applied to discovery, then unchanged to test)

A stage passes only if all hold:

1. **Floor:** at least 300 resolved clusters, 40 distinct symbols and 26 distinct UTC
   weeks; unresolved events at most 5% of events. Otherwise `insufficient_data`.
2. **Size:** cluster mean net at 30 bps cost is at least **+20 bps** (mean gross at
   least 50 bps, which is also the 50 bps sensitivity at break-even).
3. **Uncertainty:** the 90% cluster-bootstrap lower bound of the mean net at 30 bps is
   above 0.
4. **Not one corner:** without the 5 symbols with the largest total net, the mean net
   at 30 bps stays above 0; each half of the stage (6 months each) has mean net
   above 0.
5. **Not a general short:** event mean gross exceeds control mean gross by at least
   30 bps.

A mature negative mean net at 30 bps is `fail` regardless of the other conditions.
Sequence: read **discovery** first. If it does not pass, the line stops and the test
year stays unread. If it passes, read **test** with the same code revision and
parameters; nothing is tuned in between. A test pass permits only the next stage (live
measurement of settlement-time spreads and rate publication, then paper), never orders.

## Diagnostics (reported, decide nothing)

- Holds of 10 and 60 minutes (60 only where no settlement falls inside the hold).
- Triggers at -0.05% and -0.25%; the positive side (r_prev >= +0.10%, long).
- By settlement interval (1, 2, 4, 8 h) and by month.
- **Pre-settlement drift** from t - 30 min to t: the round-trip mechanism predicts a
  rise before settlement; its absence weakens the mechanism even if the main result
  passes.
- BTC-adjusted returns; maximum adverse excursion within the hold (99th percentile).
- **Dollars at USD 50 notional, one position at a time** (the event with the most
  negative r_prev at each settlement time): trades per month, net USD per month.

## Effort and stop

Fetch: about one request per event window plus funding and hourly pages; the 287-pair
check suggests tens of thousands of windows over the full universe, within public rate
limits, likely in under an hour. Code mirrors the carry screen (fetch once, report from a
verified snapshot). Stop if the discovery read fails, the floor is not met, or the
fetch cannot cover delisted instruments; the seconds-level (t + 10 s) variant is not
opened unless this one passes test.
