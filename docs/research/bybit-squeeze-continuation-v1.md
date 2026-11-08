# HYP-033: short-squeeze continuation after a first impulse, long (protocol v1, draft)

Status: **draft for the design touchpoint, 2026-11-07; revised 2026-11-08 after the
first review** (G's span and the publication assumption, separate matching of the
impulse and the waiting hour inside one week, power on the frozen pairs). Nothing has been counted or
read for this question: no impulse, funding or open interest value has been computed
for it, and no return of its events or controls has been seen. It registers nothing
until committed after review; any later change needs a dated amendment before the
step it changes runs. It is family 1 of the [research strategy v2](research-strategy-v2.md)
list ("short-squeeze continuation").

## Question

On Bybit USDT perpetuals, among instruments that have just made a first hourly impulse
up, do funding below zero and open interest growing through the impulse predict a
higher 4-hour return than comparable impulses without that combination, and is that
return positive after costs?

The question is about the combination, not the impulse. "Does a coin keep rising after
it rose" would mostly repeat closed impulse lines (HYP-030 and the momentum-flow
lines); here every comparison is between impulses.

## Assumed mechanism, stated before any outcome

Funding below zero means shorts pay longs: the short side is the crowded one. If open
interest still grows while the price jumps, new positions keep opening into the move.
With shorts already paying carry, the simplest reading is that shorts add against the
move, and their stops and margin calls are fuel for a continuation. In a pure short
squeeze open interest would fall instead (shorts closing); growth means the fuel is not
spent yet.

This is an assumption the data cannot establish. Growing open interest does not show
who opened (new longs look the same), and negative funding can come from a hedged
basis trade. The test asks only whether the combination predicts returns beyond the
impulse itself.

Competing explanations, which the control exists to separate:

- **Impulse size.** Larger impulses may continue (or revert) more whatever funding
  and open interest do: the control is matched on the impulse's size.
- **Market state.** Combinations may cluster in market-wide rallies: both legs are
  measured against BTCUSDT over their own hold, and controls come from the same days.
- **Liquidity.** Thin instruments both squeeze and revert harder: controls share the
  event's turnover bin.

## How it differs from closed lines

| Closed line             | Its object                                                                                | Difference here                                                                                                   |
| ----------------------- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| HYP-030                 | Long after a closed 1-minute bar of at least +5% on at least 5x turnover (a burst alone)  | Hourly impulse; the question is what funding and open interest add, tested against impulses without them; no tape |
| HYP-032                 | Compressed range, growing open interest, funding at or below zero, masked sell absorption | An impulse is required (HYP-032 required that nothing had moved); no tape; one fixed 4 h horizon                  |
| HYP-031                 | Short reaction in the minutes after a settlement with r_s <= -0.30%                       | The trigger is a price impulse, not a settlement; funding enters only as a sign filter; long; hours               |
| HYP-024 (pump-short)    | Short after a pump decision                                                               | Long continuation, conditioned on the short side being crowded                                                    |
| Abnormal-flow v1        | 60-minute OI and taker-flow imbalance with limited price movement, long, 720 minutes      | The price has moved (an impulse is the trigger); funding sign required; no taker flow; 4 h                        |
| Momentum-flow (HYP-015) | Paper long after the program's own pump signals, 240 and 720 minutes                      | Public history only; the trigger is the instrument's own hourly bar; the test is the combination against impulses |

## Data boundary

- Only Bybit public V5 market data. No Schurfer capture, cohort or result is read.
- **Discovery: 2024-10-01T00:00Z to 2025-09-30T23:59Z.** Every datum lies inside it.
  The 14-day histories need about 15 days, so, as in HYP-032, **2024-10-16T00:00Z is
  the first decision hour**; the last is the latest decision whose exit bar ends by
  2025-09-30T23:59Z.
  - **Reuse.** This window was used by HYP-032 (features, tape, and the dispersion of
    random-hour returns) and by other studies, so it is discovery only for this
    question. No return of this setup, its controls or its impulses has been read.
  - 2025-10-01 to 2026-09-28 stays HYP-031's reserved test year; the holdout
    2023-02-01 to 2024-09-15 is read only in the strategy's stage 2 batch.
  - No trade tape is used, so HYP-031's tape reservation is not touched.
- **Universe:** every USDT-settled `LinearPerpetual` that traded in the window, live and
  delisted, as in HYP-032. Request ends are capped at `deliveryTime`.
- **Zero traffic for the counts and the power step.** Both run on HYP-032's features
  snapshot (`features-8522f0c.jsonl.gz`, SHA-256
  `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`: catalog, 1 h
  klines, 1 h open interest, funding history), verified by hash before use. Only the
  reading needs new data (Reading inputs, below), fetched only if counts and power pass.
- **Registry.** A row for the counts (features level) is added in the same change as
  the counting code, before it runs; a row for the reading's minute bars before they are
  fetched.

## Event definition (fixed now)

Hourly bars are [h, h + 1 h). The **impulse bar** is the bar [t - 1 h, t); the
**decision time** is t_d = t + 1 h, one hour after the impulse bar closes, so that the
open interest record covering the impulse is published (below).

**Liquidity:** perp turnover over the 24 bars ending t - 1 h (before the impulse) is at
least **USD 2,000,000**.

**Impulse trigger** on the impulse bar:

- r1 = close / previous bar's close - 1 is at least **+2%**, and
- z1 = r1 divided by the standard deviation of the instrument's own 1-hour returns over
  the 336 bars before the impulse bar is at least **3**.

**First impulse:** no bar in the 24 hours before the impulse bar meets the trigger on the
same instrument. This is also the independence rule: an event's hold ends at
t_d + 4 h + 1 min, inside that 24 hours, so an instrument has at most one event per
episode, and a continuing run of impulses yields only its first.

**The combination (both at t_d):**

- **F, funding below zero:** the last settled rate strictly before t_d is **< 0**.
- **G, open interest growing over two hours that include the impulse:** G =
  OI(record stamped t) / OI(record stamped t - 2 h) - 1, in coins. G > 0 and its own
  14-day percentile (the same G at each of the 336 previous hours) is at least
  **0.80**.

What G measures. Bybit stamps a 1 h record with a time without saying whether the value
is taken at the stamp or at the end of its hour. So G is a two-hour change that always
contains the impulse bar, plus one adjacent hour: the hour before the impulse when the
value is taken at the stamp ([t - 2 h, t]), or the waiting hour after it when the value is
taken at the end ([t - 1 h, t_d]). Under either reading G uses no value after t_d, so
it is a valid predictor at t_d given the publication assumption below; it is not a
measure of the impulse bar alone.

**Assumption, not shown by history:** the record stamped t is published by the entry at
t_d + 1 min. A historical record carries its stamp, not the time it first appeared, so
the reading rests on this; a pass earns a live check of the publication time before
paper. The cost of the delay is one hour: the decision comes after the impulse bar and
one more bar.

**Event:** a liquid first impulse with F and G both true. **Impulse without the
combination:** a liquid first impulse with F or G determinably false; these are the
control pool.

**Determinability.** Each item is `true`, `false` or `undefined` with a reason
(`kline_gap`, `oi_gap`, `no_settlement`, `rank_history_short`). A standard deviation or
percentile needs at least 269 of its 336 history values (the 80% rule of HYP-032).
"First" is undefined when any bar of its 24-hour lookback is missing. Undefined
impulses are counted, never treated as false.

**Entry:** long at the open of the 1-minute bar starting t_d + 1 min (taker). **Exit:**
at the close of the 1-minute bar ending t_d + 1 min + 4 h. **H = 4 h is fixed**, not
chosen: HYP-032's dispersion step put the 12 h and 24 h effects out of reach for samples
of a few hundred events, and choosing among horizons spends power.

**Cluster:** symbol-day of t_d; every statistic weighs clusters equally.

**Completeness:** an event without its entry bar, exit bar, a BTCUSDT bar it needs, or a
funding mark inside its hold is `incomplete`, counted by reason and kept in the
denominator.

**Funding during the hold:** for each settlement s with entry < s < exit, the long's
funding return is -r_s x mark(s) / entry price, mark(s) the open of the 1-minute
mark-price kline starting at s (the HYP-032 rule). Net is reported with and without it;
the reading uses net with it.

## Costs

Taker entry and exit: 5.5 bps fee per side plus 9.5 bps per side spread and slippage,
**30 bps round trip** primary, **50 bps** sensitivity, as HYP-031 and HYP-032. The
break-even round-trip cost (mean gross) is reported too. Spreads right after an impulse
may be wider than this: a pass earns a live quote measurement before paper.

## The matched control (fixed now)

The impulse and the waiting hour are matched separately, so a strong impulse that has
already reverted is never paired with a moderate one that kept going. For each event,
candidates are impulses without the combination (above) that:

- are on another instrument;
- have t_d in the same UTC ISO week as the event's t_d and within **72 hours** of it, so
  a control used by several events stays inside one bootstrap block;
- are in the same 24-hour turnover bin (USD 2 to 10 million, 10 to 50 million, at least
  50 million, the turnover of the liquidity rule);
- have z1 (the impulse bar, as in the trigger) within **0.5** of the event's;
- have zw within **0.5** of the event's, where zw is the waiting bar's return,
  close(t_d) / close(t) - 1, divided by the same hourly standard deviation as z1.

Of those, the at most **5 nearest** by the distance sqrt((z1 difference)^2 + (zw
difference)^2), ties by turnover log ratio. A control may serve more than one event; its
total weight is accounted for in the power step. Events near a week boundary have fewer
candidates; the no-control share counts them.

Each leg is measured on its own hold, entry and exit as above, minus BTCUSDT's return
over the same interval. The event's **excess** is its BTC-relative price return minus the
mean BTC-relative price return of its controls (no funding; both legs pay the same
costs). So the control has the event's impulse size, recent path, liquidity bin and
days, and lacks the funding and open interest combination: this is the test of "more
than the impulse".

## Step 1: counts (outcome-blind, no traffic)

From the verified features snapshot, report with no price, return or feature value
printed:

- the funnel of decision hours (liquid, trigger, first impulse, F and G each true,
  false or undefined by reason, the waiting bar), and events by ISO week;
- the four cells of first impulses (F and G, F only, G only, neither), each with
  impulses, clusters, distinct symbols and ISO weeks;
- for the events: clusters, symbols, weeks, the top-5 symbol share and the top-5 ISO
  week share of clusters, and the share of events with at least one control, with the
  number of controls (median, 10th percentile) and the largest number of events one
  control serves;
- the minimum detectable effects of step 2;
- the frozen pairs (`pairs.json`): every event and its controls by symbol and t_d,
  hashed, the list the power step and the reading both use.

## Step 2: power (outcome-blind, no simulation, no forward data)

HYP-032's simulated pseudo-samples measured mostly how well the transplant reproduced
the events. Here the power estimate is computed on the frozen list of events and their
event-control pairs from step 1, with data known before each impulse only:

- **Units.** Every event hold and every distinct control hold is one unit u. Holds on
  one instrument never overlap (the first-impulse rule spaces them by more than 24 h).
- **Scales, from before the impulse.** For (a), sigma_a(u) is the standard deviation of
  the instrument's own 4-hour returns at each hourly close of the 14 days before its
  impulse bar (336 overlapping values, the 80% rule applies). For (b), sigma_b(u) is the
  same on BTC-relative 4-hour returns (the instrument's 4-hour return minus BTCUSDT's
  over the same 4 hours), the quantity the excess compares.
- **Weights from the frozen pairs.** Both estimates are linear in the unit outcomes. With
  K event clusters and n_k events in cluster k, an event e in cluster k has weight
  1 / (K n_k) in (a). In (b) it has the same weight, and each of its m_e controls gets
  -1 / (K n_k m_e); a control's weight w(u) is the sum over every pair it is in, so a
  control used by ten events carries its one outcome ten times, not ten independent
  outcomes. Events without a control drop out of (b), and the weights use the clusters
  left.
- **Variance:** sum over units of w(u)^2 x (1.5 sigma(u))^2, times the design effect 1.2.
  MDE = (1.645 + 0.842) x its square root, for (a) and (b).
- **Planning assumptions, fixed now, not measured:** the forward inflation **1.5**
  (volatility after an impulse is higher than before it) and the **design effect 1.2**
  (HYP-032 measured 1.09 at 4 h on random hours; events cluster in market episodes more
  than random hours do). Both treat units as independent apart from the design effect.
- **An undefined scale** (fewer than 269 defined 4-hour returns) takes the 90th
  percentile of the defined scales of that leg, conservatively; the substitutions are
  counted.
- Sensitivity, reported and deciding nothing: inflation 1.0 and 2.0, design effect 1.0
  and 1.5.

The scales use returns before each impulse only; nothing after any t_d is read. A
measured post-impulse dispersion is not part of v1: if a calibration is ever wanted, it
needs a dated amendment and its own registry row before it runs.

## Floor and stop (fixed now)

The study goes on to the reading only if all hold:

1. at least **150 event clusters, 40 distinct symbols and 26 ISO weeks**;
2. the top-5 symbols hold at most **30%** of event clusters;
3. at least **80%** of events have a control;
4. the item determinability: F and G determinable for at least 90% of liquid first
   impulses;
5. MDE at most **50 bps** for both (a) and (b). Kept at 50, not 75: the reading's size
   threshold is +30 bps, and a sample sized for 75 bps would only see effects well
   above it.

The top-5 week share is reported, not a floor: squeeze impulses cluster in market
episodes by nature, and HYP-032 showed that a fixed week cap on events is hard to set.
Concentration in weeks is tested in the reading instead (condition 4).

**Stop:** if any floor item fails, HYP-033 v1 stops at `insufficient_data` or
`insufficient_power`, the trigger, the combination, the horizon and these thresholds are
not loosened, and the next family is B (spot against perpetual premium). Counts are
features-level exposure only, so the window's returns for this setup stay unseen.

## Reading inputs (fetched only after step 2 passes)

Under its own registry row, before the first request: 1-minute perp klines for each
event and control hold, 1-minute BTCUSDT klines for the same intervals, and the 1-minute
mark-price kline at each settlement inside a hold. One request per hold and one per
settlement: a few thousand requests and a few MB. Every response is hashed (SHA-256) and
the reading uses only verified snapshots.

## Reading (fixed now, at H = 4 h)

Discovery passes only if all hold:

1. **Floor** as above, and at least 95% of events complete.
2. **Size:** cluster mean net at 30 bps, funding included, is at least **+30 bps**.
3. **Uncertainty:** the 5th percentile of the ISO-week block bootstrap of the mean net is
   above 0 (10,000 resamples, seed 33, whole ISO weeks; each event's controls share its
   week, so pairs resample together).
4. **Not one corner:** without the 5 symbols with the largest total net, and separately
   without the 5 ISO weeks with the largest total net, the mean net stays above 0; each
   half (t_d before 2025-04-01, and from it) has a positive mean net.
5. **More than the impulse:** the cluster mean excess over the matched control is at
   least **+30 bps**, its week-block bootstrap 5th percentile is above 0, and at least
   80% of complete events have a control.

A negative mean net at 30 bps is `fail` whatever else holds. A pass makes the primary a
stage 1 candidate (purged walk-forward, top-5 removal, 50 bps stress); never orders.

## Variants (declared now, each read once after the primary, never the primary)

- **V1 no delay:** decision at t, the impulse bar's close, entry at t + 1 min; G over the
  24 hours strictly before the impulse (records stamped t - 26 h and t - 2 h, percentile
  0.80): open interest built into the impulse rather than through it. Under the
  end-of-hour reading the record stamped t - 1 h would already hold the impulse's own
  open interest, so it is not used. V1 changes both the entry time and the open interest
  feature, and its controls are matched on z1 only (there is no waiting bar).
- **V2 any growth:** G > 0 without the percentile.
- **V3 deeper funding:** the last settled rate at or below -0.01%.

Each enters the [discovery ledger](discovery-ledger.md) with the count: primary plus 3.
Each variant has its own control pool under its own rule, and passes or fails on the
same reading conditions.

## Diagnostics (reported, decide nothing)

- The four cells: mean net of every cell, so the trigger alone (all first impulses) and
  each of F and G alone are visible beside the combination.
- Net without funding, and funding received as a share of net.
- Excess without the BTC adjustment.
- Return per dollar-hour; maximum adverse excursion in the hold (99th percentile).
- By month, by BTC 24 h return sign, by hour of day of t_d.

## Runbook for steps 1 and 2

From a merged revision with a clean tree, on the HYP-032 run's snapshot (the code
refuses any other SHA-256):

```bash
uv run --package schurfer-analytics bybit-squeeze-continuation counts \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz \
  --features-sha256 a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6 \
  --output-dir runtime/research/hyp033/counts-REV \
  --code-revision REV --no-working-tree-dirty
```

It writes `counts.json`, `counts.md` and `pairs.json` once each and prints the verdict
(`pass_to_reading`, `insufficient_data` or `insufficient_power`).

## Effort

Counts and power: one module on the existing snapshot, no traffic, about a day with
tests. If they pass, the reading's fetch and report: about a day more. Stop at any
failed floor.
