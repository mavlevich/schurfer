# HYP-031: Bybit post-funding reaction, minute variant (protocol v1, draft)

Status: **protocol v1 with Amendments 1 to 3, 2026-10-26.** Nothing is fetched, no price
after any settlement has been read, and this changes no registered rule, cohort or
roadmap order. It binds as committed before the first fetch; any later change needs
its own amendment before any return is read. The roadmap parks "funding-only short baskets"; this is a different object
(an intraday timing effect around one settlement, flat before the next one), and
opening it is the owner's decision after the 2026-10-31 review, not this document's.

## Question

On Bybit USDT perpetuals, after a settlement on an instrument whose funding is
extremely negative, does a short opened in the first full minute after settlement and
closed 30 minutes later earn enough to clear costs, on public history, out of sample?

## Mechanism, stated before any outcome

When funding is extremely negative, shorts pay and longs receive at the snapshot. Two
behaviours predict selling just after it: longs that held through the snapshot to
receive the payment close, and shorts that closed before it to avoid paying reopen.
Either needs the payment to be worth a round trip (two taker fees plus two spreads,
roughly 25 to 30 bps for a taker), which is why the primary trigger sits at that
level. A data check of 2026-10-25 (no returns read) found that 96% of settlements with
|rate| >= 0.10% are negative, so the negative side is the primary population. The
positive side (longs pay, buy after) is a diagnostic only.

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
  [t - 60 min, t + 65 min), and for the control settlements at the same t.
  BTCUSDT 1-minute klines for the market diagnostic.
- Every native response is stored once in a gzip JSON-lines snapshot whose SHA-256
  the report records; `report` reads only that verified snapshot.

## Event definition (fixed now)

For a settlement of instrument i at time t (the actual `fundingRateTimestamp`; the
catalog's current `fundingInterval` is never used, since 147 of 287 checked pairs
changed interval at least once):

- **Trigger:** the rate settled at t itself, r_t, is at most **-0.30%** per
  settlement. A live check at the 2026-10-25 23:00 UTC settlement (see Amendment 1)
  saw r_t in `/v5/market/funding/history` 0.4 to 3.6 s after t on all 7 instruments
  settling then, well before the decision at t + 1 min. The previous settled rate,
  r_prev, is a diagnostic only: at -0.30% about 44% of r_prev triggers fall on a
  settlement whose own rate has already normalized.
- **Liquidity:** perp turnover over the 24 hourly bars ending at t is at least
  **USD 2,000,000**.
- **Entry:** short at the **open of the 1-minute bar starting t + 1 min**.
- **Exit:** at the **close of the bar ending t + 31 min** (30 minutes held). The
  shortest observed interval is 1 hour, so no settlement falls inside the hold and no
  funding is paid or received.
- **Return (gross, bps):** -(exit / entry - 1) x 10,000.
- **Eligible events and completeness:** every settlement passing the trigger and the
  liquidity filter is eligible and stays in the denominator. An eligible event is
  complete when its entry bar exists and its exit bar exists (or, if missing, the first
  later bar up to t + 40 min, flagged). Every incomplete event is counted by where it
  failed (entry, exit); none is dropped silently.

## Costs

Taker fees 5.5 bps per side (Bybit base tier) plus a **9.5 bps per side** spread and
slippage scenario: **30 bps per round trip** in the primary reading, 50 bps as a
sensitivity. History has trades and closes, not bid and ask, so the report also gives
the **break-even round-trip cost** (mean gross). The spread at settlement moments is
the main unmeasured risk; a pass earns a live quote measurement, not belief.

## Independence and control

- **Cluster = symbol-day.** Extreme funding often moves to 1-hour settlements, so one
  symbol gives many events a day. Each cluster's events are averaged first; every
  statistic weights clusters equally.
- **Uncertainty: block bootstrap by UTC ISO week.** Symbol-days are not independent:
  many tokens move together on one day, and one token's days follow each other. Each
  resample draws whole weeks with replacement, with every cluster inside them, and
  its mean is the pooled mean over all clusters drawn (sum of cluster values over
  number of clusters), not a mean of weekly means; 10,000 resamples, seed 31.
- **Matched control.** For each event, the control is the mean short return, same
  entry, exit and costs, of **at most 5** other instruments settling at the **same t**
  that pass the same liquidity filter and whose r_t is within +-0.01%: the 5 closest
  to the event's instrument in 24-hour turnover (smallest absolute log ratio, ties by
  symbol), all of them if fewer. The event's
  **excess** is its gross minus that control; an event with no control at its t has no
  excess and is counted. Excess is averaged per cluster and weighted like the main
  result, so both use the same weights and the market move at t cancels.

## Reading (fixed now, applied to discovery, then unchanged to test)

A stage passes only if all hold:

1. **Floor:** at least 300 clusters, 40 distinct symbols and 26 distinct UTC weeks,
   and at least **95% of eligible events complete**. Otherwise `insufficient_data`.
2. **Size:** cluster mean net at 30 bps cost is at least **+20 bps** (mean gross at
   least 50 bps, which is also the 50 bps sensitivity at break-even).
3. **Uncertainty:** the 90% week-block bootstrap lower bound of the mean net at 30 bps
   is above 0.
4. **Not one corner:** without the 5 symbols with the largest total net, the mean net
   at 30 bps stays above 0; each half of the stage (6 months each) has mean net
   above 0.
5. **Not a general short:** the cluster mean excess over the matched control is at
   least 30 bps, and at least 80% of complete events have a control.

A mature negative mean net at 30 bps is `fail` regardless of the other conditions.
Sequence: read **discovery** first. If it does not pass, the line stops and the test
year stays unread. If it passes, read **test** with the same code revision and
parameters; nothing is tuned in between. A test pass permits only the next stage (live
measurement of settlement-time spreads, then paper), never orders. That stage reads
data after 2026-09-29, inside the HYP-012 v2 blind window, so it waits until v2 is
terminal. The verdict is reported beside the dollars at one position (diagnostics).

## Diagnostics (reported, decide nothing)

- **The first minute** [t, t + 1 min): short from the open to the close of the bar
  starting at t. Not tradable on minute data; it shows whether a reaction happens
  before the entry.
- Holds of 5 (t + 1 to t + 6 min), 10 and 60 minutes (60 only where no settlement
  falls inside the hold).
- Triggers at -0.10% and -0.50% (dose response); the r_prev trigger at -0.30%; the
  positive side (r_t at least +0.30%, long).
- By settlement interval (1, 2, 4, 8 h) and by month.
- Excess over the control split by settlement hour: 4-hour boundaries (00, 04, 08, 12,
  16, 20 UTC) and other hours, where calm controls are 1-hour contracts.
- **Pre-settlement drift** from t - 30 min to t: the round-trip mechanism predicts a
  rise before settlement; its absence weakens the mechanism even if the main result
  passes.
- BTC-adjusted returns; maximum adverse excursion within the hold (99th percentile).
- **Dollars at USD 50 notional, one position at a time** (the event with the most
  negative r_t at each settlement time): trades per month, net USD per month.

## Effort and stop

Fetch: about one request per event window plus funding and hourly pages; the 287-pair
check suggests tens of thousands of windows over the full universe, within public rate
limits, likely in under an hour. Code mirrors the carry screen (fetch once, report from a
verified snapshot). Stop if the discovery read fails, the floor is not met, or the
fetch cannot cover delisted instruments.

**Seconds-level variant.** If this variant passes, or if discovery fails while the
first-minute cluster mean short return is at least 30 bps gross, a separate
seconds-level protocol (entry a few seconds after t, from the public trade archive,
trades only, no quotes) may be registered. It designs on the discovery year only; the
test year stays unread until that protocol is committed.

## Amendment 1 (2026-10-26, before any fetch or return)

Colleague reviews of v1 (`40489d8`) were taken point by point; nothing here was chosen
after seeing a price.

- **Trigger r_t at -0.30%,** was r_prev at -0.10%. The mechanism needs a payment worth a
  taker round trip; r_prev partly tests a different question (about 44% of its events have
  a normal current rate). The switch was conditional, fixed before the check: r_t
  becomes primary if the settled rate is public within 30 s of t. Check (script and
  output sha256 prefixes `380bf385`, `f2e3f027`): at 2026-10-25 23:00 UTC the 7
  instruments settling (all 1-hour intervals) showed their row in funding history
  0.4 to 3.6 s after t, polled about every 1 to 2 s. The last ticker `fundingRate`
  before t differed from the settled rate by up to about 0.006 percentage points, so the
  ticker is not used. One settlement only; the live stage re-measures it, including 4
  and 8-hour intervals.
- **Completeness over eligible events**, was unresolved exits only: missing entries can
  no longer leave the denominator.
- **Week-block bootstrap**, was a symbol-day bootstrap: tokens move together on a day.
- **Matched control at the same t** with the same weights, was an event-weighted
  control over the whole window.
- **Event window to t + 65 min**, was t + 40: the 60-minute diagnostic needs it.
- **Mechanism names both sellers** (paid longs closing, shorts reopening).
- **First-minute and 5-minute diagnostics added; the seconds-level variant is no longer
  blocked** by a minute-variant failure when the first minute shows at least 30 bps.
- **Live stage waits for HYP-012 v2 to be terminal** (its blind window starts
  2026-09-29).

Unchanged: costs (30 bps, 50 bps sensitivity), the +20 bps net bar, the floor, the
split, the discovery-then-test sequence.

## Amendment 2 (2026-10-26, before any fetch or return)

From a colleague review of Amendment 1 (`f16598e`), checked on settled rates only
(287-pair snapshot, no prices): 93% of r_t <= -0.30% events have a calm settlement at
the same t before the liquidity filter (100% on 4-hour boundaries, 82% at other hours),
so the 80% control condition is not mechanically out of reach; but the median event
has 142 such instruments, which would multiply the fetch.

- **Control capped at 5 instruments** per event, nearest in 24-hour turnover.
- **Bootstrap mean stated:** the pooled mean over the clusters of the drawn weeks.
- **Diagnostic added:** excess by settlement hour type, since controls at non-4-hour
  hours are 1-hour contracts.

Unchanged: the trigger, costs, the +20 bps bar, the 80% control condition, the floor,
the split and the sequence.

## Amendment 3 (2026-10-26, before any fetch or return)

From a colleague review of the implementation (#526). None changes a threshold, cost or
the split; each closes a way the reading could pass on something other than the effect.

- **A failed fetch blocks the read.** An instrument whose fetch failed or returned
  another symbol's rows is dropped whole, often a delisted one, so its events are
  unknown. The floor now also requires that no instrument failed; otherwise the stage is
  `insufficient_data`.
- **The control exits on the event's own exit bar.** When the event's exit uses the
  fallback bar, its controls are priced at that same bar, with no fallback of their own;
  a control without that bar has no value. Before, an event exiting one bar late against
  controls exiting on time could show excess that was only the later market move.
- **Every window stops at its stage's end,** including a symbol missing from the index
  (BTCUSDT after a failed fetch), so discovery never stores test-year prices.
- **The test year needs a verified discovery pass.** `fetch-index --stage test` refuses
  to run without the discovery `result.json` of this version and its SHA-256, with
  verdict `pass`.
- **The seconds-level variant opens from a pass, or from a discovery failure with a
  strong first minute;** a test-year first minute never opens it.
- **The one-position diagnostic chooses before the outcome:** the most negative r_t among
  eligible events at each time; a chosen event without a complete path counts as
  unresolved and is never replaced by another.
- **The uncertainty bound is the 5th percentile,** the lower end of the two-sided 90%
  week-block interval (stricter than a one-sided 90% bound).
