# HYP-034: perpetual discount to the index, long (protocol v1)

Status: **steps 1 and 2 passed 2026-11-08 (`pass_to_reading`, below); no return after any
decision was read.** Drafted for the design touchpoint 2026-11-08 and revised the same
day after the first review (the convergence diagnostic, the last hour's direction in
the match, overlapping control holds in the power step, a live publication check);
the premium index was fetched and counted after that. Any later change needs a dated
amendment before the step it changes runs. It is family 4 of the
[research strategy v2](research-strategy-v2.md) list ("spot against perpetual
premium"), next after [HYP-033](bybit-squeeze-continuation-v1.md) stopped at
`insufficient_data`.

## Question

On Bybit USDT perpetuals, when the perpetual has just closed an hour at a deep discount
to its spot index, is the perpetual's 4-hour return higher than that of comparable
hours without a dislocation, and is it positive after costs?

## Assumed mechanism, stated before any outcome

Bybit's premium index compares the perpetual's book with the spot index at impact size:
P = [max(0, impact bid - index) - max(0, index - impact ask)] / index, sampled every
minute. P < 0 means that even the impact ask is below the index: a buyer of a normal
size can buy the perpetual below spot. A dislocation like that is opened by one-sided
perpetual flow (forced selling, leveraged shorts) and closed by arbitrage (long perp,
short spot) and by the funding it creates, which pays longs. If part of the closing
happens through the perpetual's price, a long earns the convergence.

This is an assumption the data cannot establish. Competing explanations, which the
control and the diagnostics exist to separate:

- **The perpetual leads.** The perpetual is often where price is found first; then the
  gap closes by the index falling to the perpetual, and a long earns nothing. The
  diagnostic splits each event's convergence into the perpetual's and the index's part.
- **A fall and its reversal.** A discount usually comes with a fall; short-term reversal
  after a fall alone would look like convergence. The control is matched on the fall.
- **Market state.** Discounts cluster in market-wide sell-offs: both legs are measured
  against BTCUSDT over their own hold, and controls come from the same days.
- **Liquidity.** Thin books dislocate and revert harder: controls share the event's
  turnover bin.

## How it differs from closed lines

| Closed line                    | Its object                                                                | Difference here                                                                                                               |
| ------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| HYP-012b, HYP-012c source-lead | A lagging venue's price after another venue's move (cross-venue lead-lag) | One venue; the object is the perpetual's own book against its spot index, measured at impact size; the trade is the perpetual |
| HYP-031 post-funding           | Short in the 30 minutes after a settlement with r_s <= -0.30%             | Any hour, not tied to a settlement; the trigger is the live premium, not a settled rate; long; 4 h                            |
| HYP-033 squeeze continuation   | Long after a first impulse up with funding < 0 and growing open interest  | No impulse; the discount comes with a fall, not a rise; funding is not a filter                                               |
| Carry screen v1.1              | Holding spot long and perpetual short for funding over days               | Directional perpetual only, hours, the price convergence is the object (funding received is reported as its part of net)      |
| HYP-024 pump-short             | Short after a pump                                                        | Long, after a dislocation, no pump required                                                                                   |

The strategy keeps cross-venue lead-lag off its list because the source-lead family
failed. This question stays on it as family 4: the gap is between the perpetual and the
index Bybit itself computes and funds against, and the mechanism that closes it
(funding and basis arbitrage) does not exist between two venues' spot prices.

## Data boundary

- Only Bybit public V5 market data. No Schurfer capture, cohort or result is read.
- **Discovery: 2024-10-01T00:00Z to 2025-09-30T23:59Z**, first decision hour
  2024-10-16T00:00Z (the 14-day histories), as HYP-032 and HYP-033.
  - **Reuse.** The window was used by HYP-032 and HYP-033 (features: 1 h klines, open
    interest, funding) and by other studies, so it is discovery only. No premium index
    value has been read in it, and no return of this setup, its controls or its hours.
  - 2025-10-01 to 2026-09-28 stays HYP-031's reserved test year; the holdout
    2023-02-01 to 2024-09-15 is read only in the strategy's stage 2 batch.
- **Universe:** the instruments of HYP-032's features snapshot (`features-8522f0c`,
  SHA-256 `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`), live and
  delisted, request ends capped at `deliveryTime`.
- **New fetch (counts need it):** `/v5/market/premium-index-price-kline`, interval 60,
  for every instrument of the snapshot over the window. About 9 pages of 1,000 bars per
  instrument, about 6,000 requests, about 0.5 GB raw and under 100 MB compressed, under an hour at the HYP-032
  fetcher's rate. Stored once as a gzip JSON-lines snapshot whose SHA-256 the reports
  record. The 1 h perp klines and funding come from the features snapshot, verified by
  hash.
- **Registry.** A row for the premium index fetch and the counts (features level) is
  added in the same change as the fetching code, before it runs; a row for the
  reading's minute bars before they are fetched.

## Event definition (fixed now)

Hourly bars are [h, h + 1 h). The decision time t is the close of the premium bar
[t - 1 h, t); P(t) is that bar's close (the last minute's premium).

- **Liquidity:** perp turnover over the 24 bars ending t is at least **USD 2,000,000**.
- **Dislocation:** P(t) is at most **-0.50%**, and P(t) is at or below the 5th
  percentile of the instrument's own P closes over the 336 hours before t (the 80% rule:
  at least 269 defined). The absolute level sets the depth of the signal, the
  percentile its rarity for the instrument: alone, the first would catch chronic
  discounts and the second small ones. Neither says what a long earns: P measures the
  book at impact size against the index at t, the gap may change before the entry, and
  it may close through the index.
- **First:** no hour in the 24 before t meets the dislocation on the same instrument.
  This is also the independence rule (the hold ends inside those 24 hours).
- **Event:** a liquid first dislocation. **Ordinary hour:** a liquid hour with
  -0.10% < P(t) < +0.10% and no dislocation in the 24 hours before t; these are the
  control pool.
- **Determinability:** each item is `true`, `false` or `undefined` with a reason
  (`premium_gap`, `kline_gap`, `rank_history_short`). Undefined hours are counted,
  never treated as false. An event or ordinary hour whose r1, 4-hour return or hourly
  standard deviation (the match inputs, below) is undefined is counted by role and
  dropped.
- **Publication, an assumption for discovery:** the premium bar closed at t is
  assumed final and available by the entry at t + 1 min. The API documents a bar's
  start time and close, not when the final close first appears; the live check under
  Reading inputs settles this before any return is read.
- **Entry:** long at the open of the 1-minute perp bar starting t + 1 min (taker).
  **Exit:** close of the 1-minute bar ending t + 1 min + 4 h. **H = 4 h fixed.**
- **Funding during the hold:** each settlement inside the hold, at the HYP-033 rule
  (rate times mark over entry), using the settled rates. A negative P(t) may lower the
  coming rate, but the sign of a payment is set by the whole interval, so nothing about
  it is assumed. Net is reported with and without it; the reading uses net with it.
- **Cluster:** symbol-day of t; every statistic weighs clusters equally.
- **Completeness:** as HYP-033; incomplete events stay in the denominator.

## Costs

Taker both sides, **30 bps round trip** primary, **50 bps** sensitivity, as HYP-031 to
HYP-033. The break-even round-trip cost is reported. Books right after a dislocation may
be wider: a pass earns a live quote measurement before paper.

## The matched control (fixed now, one caliper)

HYP-033 lost 39% of its events to two calipers at once. Here the competing explanation
is the fall that came with the discount, so the match has one numeric caliper and one
category:

- an ordinary hour (above) on another instrument;
- t in the same UTC ISO week as the event's and within **72 hours** of it;
- the same 24-hour turnover bin (USD 2 to 10 million, 10 to 50 million, at least 50
  million);
- z4 within **0.5** of the event's, where z4 is the perp's return over the 4 hours
  ending t divided by twice the standard deviation of its own 1-hour returns over the
  336 hours before (the 80% rule);
- the same direction of the last hour, r1 = close(t) / close(t - 1 h) - 1: down
  (r1 < 0) or not down (r1 >= 0). The same z4 can follow a fresh fall or a fall whose
  rebound has begun; the category keeps them apart without a second caliper.

Of those, the at most **5 nearest** by |z4 difference|, ties by turnover log ratio. Each
leg is measured on its own hold minus BTCUSDT's return over the same interval; the
event's **excess** is its BTC-relative price return minus the mean of its controls' (no
funding; both legs pay the same costs). So the control has the event's fall, liquidity
bin, last-hour direction and days, and no dislocation.

**Balance, reported before any reading:** for the events with a control and their
controls (weighted as in (b)), the mean and the standardized mean difference of the raw
r1 and the raw 4-hour return ending t. These are returns before t only. They decide
nothing; they show how close the match is before anything after t is seen. If fewer than
80% of events have a control, the written stop applies.

## Step 1: counts (outcome-blind)

From the verified premium snapshot and features snapshot, with no price, return or
premium value printed: the funnel of decision hours (liquid, premium defined,
dislocation, first, ordinary, last-hour direction), events by ISO week; for the events clusters, symbols,
weeks, the top-5 symbol and week shares; the share with a control, controls per event
(median, 10th percentile), and the most events one control serves; and the frozen pairs
(`pairs.json`), hashed. The positive side (P(t) at least +0.50% at or above its 95th
percentile) is counted the same way, with its own pairs, for V1. The overlap
components of step 2 (their number, the largest, and the units in components of more
than one) are reported with the counts.

## Step 2: power (outcome-blind)

On the frozen pairs, HYP-033's method with one change: units are holds, weights
1 / (K n_k) for events and the summed -1 / (K n_k m_e) for controls, scales from the 336
hours before t (own 4-hour returns for (a), BTC-relative for (b)), inflation **1.5**,
design effect **1.2**, MDE = 2.487 x the square root of the variance, an undefined scale
takes the 90th percentile. Sensitivity grid inflation 1.0, 1.5, 2.0 by design effect
1.0, 1.2, 1.5, deciding nothing. The floor compares unrounded fractions of integer
counts.

**The change: overlapping holds.** In HYP-033 the first-impulse rule spaced every hold on
an instrument by more than 24 hours. Here ordinary hours are not spaced: X at 10:00 and X
at 11:00 can both be controls and share three hours of returns, and summing a control's
weights over its pairs does not cover that. So, before the MDE:

- all units of a leg (events and controls) are grouped by instrument into **overlap
  components**: two holds on one instrument share returns when their decision times are
  less than 4 hours apart, and are then in one component, transitively;
- a component c contributes (sum over its units of |w(u)| x 1.5 sigma(u))^2 to the
  variance, the bound for any correlation inside it (it equals the single-unit term
  for a component of one); the variance is the sum over components, times the design
  effect 1.2.

The bound is conservative: it treats overlapping holds as one movement counted with
all its weight, so (b) cannot pass 50 bps by reusing one price path.

## Floor and stop (fixed now)

As HYP-033: at least **150 clusters, 40 symbols, 26 ISO weeks**; top-5 symbols at most
**30%**; at least **80%** of events with a control; the premium defined for at least
**90%** of liquid hours; MDE at most **50 bps** for (a) and (b).

**Stop:** any failure stops HYP-034 v1 at `insufficient_data` or `insufficient_power`;
the threshold, the percentile, the caliper, the horizon and the floor are not loosened,
and the next family is C (relative strength).

## Reading inputs (fetched only after step 2 passes)

**Live publication check, first.** On a separate live window of at least 48 hours,
after counts and power pass and before any return is read: for a fixed set of
instruments, the premium index kline endpoint is requested every 5 seconds from each
hour's close to 3 minutes after it, recording when the closed bar first appears and
whether its close changes on later requests. If the 95th percentile of first
availability is after t + 1 min, or a close changes after it first appears in more than
1% of bars, the entry time moves, by a dated amendment before the reading, to the first
whole minute after which the bar is final. Only publication times and premium values are
recorded; no return is read.

Then, under its own registry row: 1-minute perp klines for each event and control hold,
1-minute BTCUSDT klines for the same intervals, the 1-minute mark-price kline at each
settlement inside a hold, the 1-minute index-price kline at each event's entry and exit
(the convergence diagnostic), and the 1-minute premium index kline at the same two
minutes. A few thousand requests, a few MB, hashed.

## Reading (fixed now, at H = 4 h)

HYP-033's five conditions unchanged, seed 34: floor and at least 95% complete; cluster
mean net at 30 bps, funding included, at least **+30 bps**; the ISO-week block bootstrap
5th percentile of the mean net above 0 (10,000 resamples); without the top 5 symbols,
and separately without the top 5 weeks, the mean net above 0, and both halves (before
2025-04-01 and from it) positive; the cluster mean excess at least **+30 bps** with its
bootstrap 5th percentile above 0 and at least 80% of complete events with a control. A
negative mean net at 30 bps is `fail`. A pass makes the primary a stage 1 candidate,
never orders.

## Variants (declared now, each read once after the primary, never the primary)

- **V1 premium, short:** P(t) at least +0.50% and at or above its own 95th percentile,
  short. The other side of the same mechanism; rarer, and closer to the closed pump-short
  lines, so not the primary.
- **V2 shallower:** -0.30% instead of -0.50%, the percentile kept.
- **V3 1 hour:** H = 1 h.

Each enters the [discovery ledger](discovery-ledger.md) with the count: primary plus 3.

## Diagnostics (reported, decide nothing)

- **Who converged:** per event, the change of log(perp / index) between entry and exit,
  split exactly into the perpetual's log return and minus the index's log return over the
  same two minutes; the change of P between them is shown separately, since P is
  measured at impact size and log(perp / index) on last prices.
- Net without funding, and funding received as a share of net.
- Excess without the BTC adjustment.
- By depth of P(t) (-0.50 to -1%, -1 to -2%, beyond), by turnover bin, by month, by BTC
  24 h return sign.
- Maximum adverse excursion in the hold (99th percentile).

## Runbook for the fetch and steps 1 and 2

From a committed revision with a clean tree:

```bash
uv run --package schurfer-analytics bybit-premium-dislocation fetch-premium \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz \
  --features-sha256 a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6 \
  --snapshot runtime/research/hyp034/premium-REV.jsonl.gz
```

The printed SHA-256 is pinned as `PREMIUM_SHA256` in the code by its own commit before
the first count, and recorded here. The fetch from a6a1408 (2026-11-08, about 10
minutes) covered 671 instruments with no error in 4,619 responses (41 MB):
`premium-a6a1408.jsonl.gz`, SHA-256
`ec05a2c39f6e9eaa5fba52396ff286b331253ad42a30678e24204c720c28e43a`. Then:

```bash
uv run --package schurfer-analytics bybit-premium-dislocation counts \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz \
  --features-sha256 a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6 \
  --premium runtime/research/hyp034/premium-REV.jsonl.gz --premium-sha256 SHA \
  --output-dir runtime/research/hyp034/counts-REV \
  --code-revision REV --no-working-tree-dirty
```

It writes `counts.json`, `counts.md` and `pairs.json` once each and prints the verdict.

## Steps 1 and 2 result (2026-11-08)

`bybit-premium-dislocation counts` from 875cb52 (this branch, clean tree, before merge),
2026-11-08, 6 min 17 s. `counts.json` SHA-256
`bd77abdd803cf5db2bf7114987dcabb9e2ef505bd9efea293eb74a3487bbb72c`, `pairs.json`
`5a4582b614073cd603f5b9133c682223b7dabe8df684ffc496263beffa0e776c`. Nothing after any
decision time was read; the only returns in the outputs are the pre-t balance returns.

**Funnel.** 4,214,277 decision hours, 2,391,909 liquid, premium determinable for 99.95%
of them. 10,245 liquid discount hours, 1,957 of them first (the events); 1,258 liquid
premium hours, 600 first (V1). 2,020,489 ordinary hours in the primary control pool.

| Side          | Events (= clusters) | Symbols | ISO weeks | Top-5 symbols | Top-5 weeks | With a control | Controls per event (median, p10) |
| ------------- | ------------------- | ------- | --------- | ------------- | ----------- | -------------- | -------------------------------- |
| Primary, long | 1,957               | 441     | 51        | 7.3%          | 19.9%       | 91.6%          | 5, 1                             |
| V1, short     | 600                 | 190     | 51        | 14.3%         | 39.5%       | 98.0%          | 5, 5                             |

**Power** (overlap components bounded):

| Leg                | Units | Components | Largest | MDE, bps (1.5, 1.2) | Grid range    |
| ------------------ | ----- | ---------- | ------- | ------------------- | ------------- |
| Primary (a) net    | 1,957 | 1,957      | 1       | 38.9                | 23.7 to 58.0  |
| Primary (b) excess | 9,554 | 8,897      | 7       | 42.5                | 25.9 to 63.3  |
| V1 (a) net         | 600   | 600        | 1       | 97.5                | 59.3 to 145.3 |
| V1 (b) excess      | 3,464 | 3,398      | 3       | 96.5                | 58.7 to 143.8 |

**Floor (primary):** every item holds. Verdict **`pass_to_reading`**. V1 is far from 50
bps; it stays a declared variant, read once after the primary.

**Balance before t (decides nothing).** Only 491 of the 1,957 events (25%) follow a down
hour. The cluster-weighted mean return of the hour before t is +470 bps for the events
against +199 bps for their controls (standardized difference 0.36), and of the 4 hours
before t +739 against +604 bps (0.13). So a deep discount mostly comes with a fast rise,
where the index has moved ahead of the perpetual, not with a fall as the mechanism
section assumed; and the z4 caliper with the direction category leaves the events'
last hour well above their controls'.

**Open before the reading:** whether to amend the control (for example a caliper on the
last hour's z as well) before any return is read, the live publication check (Reading
inputs), and the owner's go for the reading.

## Effort

Fetch about 1 hour; code (fetch, counts, power, reusing HYP-033's matching and power
functions) a day; counts and power minutes.
