# HYP-035: relative strength, long against BTC (protocol v1)

Status: **stopped 2026-11-08 at steps 1 and 2 (`insufficient_balance`, and both
legs above the MDE limit, below); no return after any decision was read.** Drafted for
the design touchpoint and revised the same day after the review (a dollar-neutral daily
book closed every day, costs per long notional, equal day weights, power on daily
portfolios over pre-formation segments, beta balance). Any later change needs a dated amendment before the step it changes runs. It is family 6 of the
[research strategy v2](research-strategy-v2.md) list ("relative strength"), next after
[HYP-033](bybit-squeeze-continuation-v1.md) and HYP-034 (perpetual premium, PR #564)
stopped at `insufficient_data`.

## Question

On Bybit USDT perpetuals, does an instrument in the top decile of the liquid cross-section
by its 7-day return relative to BTC keep outperforming over the next 24 hours: does a
daily dollar-neutral book (long the decile, short BTC) earn after costs and funding, and more than same-day instruments of similar volatility and
liquidity from the middle of the ranking?

## Assumed mechanism, stated before any outcome

Information and attention spread slowly across a few hundred perpetuals: a coin that has
led the market for a week keeps drawing trend-following and narrative flow for a while,
and that flow does not end at once. If so, a leader keeps beating BTC for the next day.

This is an assumption the data cannot establish. Competing explanations, which the
control and the diagnostics exist to separate:

- **Short-term reversal.** Pumps revert (the pump-short lines found that), so a leader
  with a large last day may give it back. Reported by the return of the 24 hours before t;
  the primary does not filter on it.
- **Volatility and beta.** Leaders are volatile and move more with the alt market; a
  long against BTC then earns the alt factor, not momentum. The control comes from the
  same decision time (the same alt market) and is matched on volatility and liquidity.
- **Funding.** Leaders are often crowded longs paying high funding. Net counts the
  settled funding of both legs.
- **New listings.** Freshly listed coins rank high and behave differently; the 14-day
  history requirement excludes the first two weeks, and listing age is a diagnostic.

## How it differs from closed lines

| Line                           | Its object                                                                                            | Difference here                                                                                                  |
| ------------------------------ | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| HYP-014, HYP-015 momentum-flow | Minute-level flow bursts from the watch, holds of 4 h and 12 h                                        | No trigger or flow; a daily cross-sectional rank of 7-day returns from public hourly bars, 24 h, hedged with BTC |
| early_momentum_v4 (falsified)  | Catching an early intraday move                                                                       | Not early: the leader is already a week ahead; the question is persistence, not detection                        |
| HYP-024 pump-short (stopped)   | Short after a pump                                                                                    | Long the leaders, relative to BTC, ranked over a week, not on a pump event                                       |
| HYP-033 squeeze continuation   | Long after a first hourly impulse with negative funding and rising OI                                 | No impulse, funding or open interest input; a daily rank                                                         |
| HYP-032 compressed spring      | Sell absorption in a compressed range; momentum relative to the market only as a baseline, never read | The relative momentum is the primary here, on its own protocol and window reading                                |

## Data boundary

- Only Bybit public V5 market data. No Schurfer capture, cohort or result is read.
- **Discovery: 2024-10-01T00:00Z to 2025-09-30T23:59Z**, first decision 2024-10-16T00:00Z
  (the 14-day histories), as HYP-032 to HYP-034.
  - **Reuse.** The window was used by HYP-032 to HYP-034 at the features level (1 h
    klines, open interest, funding, premium) and by other studies, so it is discovery
    only. No 7-day relative rank and no return of this setup or its controls has been
    read in it.
  - 2025-10-01 to 2026-09-28 stays HYP-031's reserved test year; the holdout 2023-02-01 to
    2024-09-15 is read only in the strategy's stage 2 batch.
- **Universe:** the instruments of HYP-032's features snapshot (`features-8522f0c`,
  SHA-256 `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`), live and
  delisted, BTCUSDT as the benchmark and never a unit.
- **No new fetch for counts and power:** the ranks, the controls and the pre-t scales
  come from the snapshot's hourly bars, verified by hash. The reading's minute bars are a
  later fetch under their own registry row.
- **Registry.** A row for the counts (features level) is added in the same change as the
  counting code, before it runs.

## Decisions and units (fixed now)

Hourly bars are [h, h + 1 h). **One decision time a day, t = 00:00 UTC**, from
2024-10-16 to 2025-09-29 (the last whose hold and exit end inside the window).

- **Liquid at t:** turnover over the 24 bars ending t at least **USD 2,000,000**.
- **Formation:** m(t) = ln(close(t) / close(t - 168 h)) - the same for BTCUSDT, where
  close(t) is the close of the bar [t - 1 h, t). Defined only when both closes exist.
  Subtracting BTC's return, the same for every instrument at t, does not change the
  ranks; the hedge matters for the trade's result, not for the selection.
- **Scale inputs:** the standard deviation of the instrument's own hourly returns over
  the 336 hours before t (the 80% rule), as HYP-033 and HYP-034, and its beta to BTC:
  the least-squares slope of its hourly returns on BTCUSDT's over the same hours.
- **Rank:** the mid-rank of m(t) among the liquid instruments at t with m and the
  scale defined (at least 100 of them, or the day is counted as `thin_cross_section` and
  dropped).
- **Unit (event):** a liquid instrument with rank at least **0.90** (the top decile).
  Every top-decile member is a unit, whether or not it was a member the day before: the
  question is whether current leaders keep leading, not what new leaders do. The same
  instrument on consecutive days gives consecutive, non-overlapping holds, which may
  still depend on each other (Step 2).
- **Determinability:** each item is `true`, `false` or `undefined` with a reason
  (`kline_gap`, `history_short`). Undefined instrument-days are counted, never treated
  as false.
- **The portfolio, a dollar-neutral daily book.** Each day: long every unit with equal
  notional, and short BTCUSDT for the sum of the long notionals, at the open of the
  1-minute bars starting t + 1 min (taker). **Both legs are closed every day** at the
  close of the 1-minute bars ending t + 1 min + 24 h, with no carry-over or rebalancing
  of members that stay in the decile, so every day pays entry and exit costs on both
  legs. **H = 24 h fixed.** Equal notional is dollar-neutral, not beta-neutral: leaders
  with a beta above 1 keep a residual long exposure to the market, which the result
  includes. The formation closes at t are hourly kline closes, final at t; the minute of
  margin is for the order, not for publication.
- **Units of account.** Every return, cost and threshold is per unit of **long-leg
  notional**. A unit's net = (its 24-hour return - BTC's over the same minutes) - 42 bps
  - funding (below). The day's net is the equal-weight mean over its units.
- **Funding:** every settlement inside the hold on both legs, at the HYP-033 rule (rate
  times mark over entry) with the settled rates: the long pays a positive alt rate, the
  BTC short receives a positive BTC rate. Net is reported with and without it; the
  reading uses net with it.
- **Weights: days, then units.** Every decision day weighs the same (a book of the same
  daily notional), and inside a day its units weigh the same. So a unit's weight in (a)
  is 1 / (D n_d), with D the days and n_d the day's units. The same weights are used in
  the power step and in the reading. The cluster is the decision day.
- **Completeness:** as HYP-033; incomplete units stay in the denominator of their day.

## Costs

Per unit of long-leg notional, each day. Alt leg taker both sides, **30 bps round trip**
primary, **50 bps** sensitivity, as HYP-031 to HYP-034. BTC leg taker both sides, **12
bps** round trip primary (2 x 5.5 bps fee and 1 bps slippage), **20 bps** sensitivity;
the BTC notional equals the long notional. Net at the primary costs therefore carries
**42 bps a day**, at the sensitivity 70 bps. The break-even alt-leg cost is reported, and
(a diagnostic) the cost of a book that trades only the day's changes in membership.

## The matched control (fixed now)

The competing explanations are volatility and the alt market, so a control is an
instrument of the same t whose rank is ordinary:

- the same decision time t, another instrument;
- rank between **0.30 and 0.70** (the middle of the cross-section);
- the same 24-hour turnover bin (USD 2 to 10 million, 10 to 50 million, at least 50
  million);
- ln(sd_c / sd_e) within **0.25**, where sd is the 336-hour scale input above.

Of those, the at most **5 nearest** by |ln sd ratio|, ties by turnover log ratio. A
control may serve several events of its day. The event's **excess** is its BTC-relative
24-hour price return minus the mean of its controls' (no funding, no costs: both pay the
same alt costs, and with the same entry and exit minutes BTC cancels), so (b) measures
the leaders against the middle of the same cross-section. The day's excess is the
equal-weight mean over its events with a control; days weigh the same; a control's
weight sums over its pairs.

**Balance gate (learned from HYP-034, fixed now):** on the pairs, the weighted
standardized mean difference of ln sd (weights as in (b), the spreads weighted like the
means) is at most **0.10** in absolute value. Reported beside it and deciding nothing:
the SMD of the beta to BTC (matching on volatility does not rule out that leaders react
more to the market), of the 24-hour BTC-relative return before t and of ln turnover. The
24-hour return is not balanced by design: momentum is the treatment.

## Step 1: counts (outcome-blind)

From the verified features snapshot, with no return after any t: the funnel of
instrument-days (liquid, scale and formation defined, thin days), cross-section size per
day (median, minimum), units per day (median, minimum), days, symbols, ISO weeks, the
top-5 symbol share of the (a) weight and the top-5 week share, the share of units that
were in the previous day's top decile, the share with a control, controls per event
(median, 10th percentile), the most events one control serves, and the frozen pairs
(`pairs.json`), hashed.

## Step 2: power (outcome-blind)

**What is measured is a daily portfolio**, so its spread is estimated as one: for each
leg the day's weighted portfolio, (a) the mean of its units' BTC-relative returns and
(b) the mean over its events of the event minus its controls' mean, with the day's frozen
members, pairs and weights. The signs of the weights, a control serving several events,
and the co-movement of the day's units are then inside the one quantity whose variance
is needed.

- **Pre-formation segments.** The spread of day d's portfolio is estimated on the seven
  non-overlapping 24-hour segments [t - 168 h - 24 h x j, t - 168 h - 24 h x (j - 1)),
  j = 1 to 7, that is the week before the formation window [t - 168 h, t). These hours
  did not select the members (the selection uses only m(t), the closes at t - 168 h and
  t). A member's segment return is undefined when a close is missing; a segment is
  defined when at least 80% of the day's members (for (b), events with all their
  controls' segment returns defined at 80%) are; sigma_d is the sample sd of the defined
  segment returns when at least 5 are, else the 90th percentile of the defined sigma_d.
- **Dependence across days.** Holds of different days do not overlap, but leaders
  persist and the momentum factor is serially correlated. The pooled lag-k
  autocorrelations rho_k (k = 1 to 6) of the segment series, around the grand mean of
  each leg, give the weekly factor F = max(1, 1 + 2 x sum((1 - k/7) x rho_k)).
- **MDE.** Var = sum over days of (1.5 x sigma_d)^2 / D^2, times the design effect
  **1.2**, times F; MDE = 2.487 x its square root.
- Overlapping holds on one instrument cannot occur at H = 24 h with daily decisions;
  HYP-034's overlap bound is used for V3.

Sensitivity, deciding nothing: inflation 1.0, 1.5, 2.0 by design effect 1.0, 1.2, 1.5 by
F at 1, the estimate and 2. The floor compares
unrounded fractions of integer counts.

**Expectation, stated before counting** (arithmetic, no market data): with about 35
units a day over about 350 days, unit sds near 8% and a within-day correlation of 0.1, a
day's portfolio sd is near 2.8% and MDE (a) near **62 bps** before F. A stop at step 2 is
likely. The counts need no fetch and take minutes, so this is the cheap way to find out.

## Floor and stop (fixed now)

At least **150 decision days, 40 symbols, 26 ISO weeks**; top-5 symbols at most **30%**
of the (a) weight; at least **80%** of events with a control; scale and formation
defined for at least **90%** of liquid instrument-days; the balance gate above; MDE at
most **50 bps** for (a) and (b).

**Stop:** any failure stops HYP-035 v1 at `insufficient_data`, `insufficient_balance` or
`insufficient_power`; the lookback, the decile, the calipers, the horizon and the floor
are not loosened, and the next family is chosen with the owner and the colleague.

## Reading inputs (fetched only after step 2 passes)

Under its own registry row: the 1-minute perp klines at each event's and control's
entry and exit minute, the same for BTCUSDT at each decision day, and the 1-minute mark
price at each settlement inside a hold (funding). About 2 x (units + controls + days)
requests, a few MB, hashed.

## Reading (fixed now, at H = 24 h)

HYP-033's five conditions with this protocol's weights (days equal, units equal inside
a day), seed 35: floor and at least 95% complete; the mean daily net of the
dollar-neutral book (funding included) at the primary costs at least **+30 bps** of
long-leg notional; the ISO-week block
bootstrap 5th percentile of the mean net above 0 (10,000 resamples); without the top 5
symbols (by (a) weight), and separately without the top 5 weeks, the mean net above 0, and both halves
(before 2025-04-01 and from it) positive; the mean excess at least **+30 bps** with its
bootstrap 5th percentile above 0 and at least 80% of complete events with a control. A
negative mean net is `fail`. A pass makes the primary a stage 1 candidate, never orders.

## Variants (declared now, each read once after the primary, never the primary)

- **V1 one day:** formation over 24 hours instead of 168.
- **V2 one month:** formation over 720 hours (first decision 2024-10-31).
- **V3 three days:** H = 72 h, daily decisions; holds of one instrument overlap and get
  HYP-034's overlap bound.
- **V4 unhedged:** the long alone, no BTC leg (the market-exposed version a small account
  could trade without a hedge).

Each enters the [discovery ledger](discovery-ledger.md) with the count: primary plus 4.

## Diagnostics (reported, decide nothing)

- Net by the 24-hour return before t (terciles inside the top decile): the reversal risk.
- Units that were in the previous day's decile against new members, and the cost of a
  book that trades only membership changes.
- The alt leg and the BTC leg separately; net without funding; funding as a share of net.
- By listing age, turnover bin, month, BTC's own 7-day return sign.
- Rank at least 0.97 (the top of the decile) against 0.90 to 0.97.
- A top-3-by-rank daily basket, the form a USD 50 account could trade (Executability).

## Executability at USD 50 (stated now, decides nothing)

A top decile is about 35 instruments a day; a USD 50 account cannot hold them all. The
primary measures the mean of a unit; a random subset has the same expected return with
more variance, a top-k subset does not (hence the diagnostic). The BTC hedge has a
minimum of 0.001 BTC, so even one hedged position needs leverage on the BTC leg. A pass
leads to a stage 1 design of the tradable form, not to orders.

## Runbook for steps 1 and 2

From a committed revision with a clean tree, after the review and the registry row:

```bash
uv run --package schurfer-analytics bybit-relative-strength counts \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz \
  --features-sha256 a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6 \
  --output-dir runtime/research/hyp035/counts-REV \
  --code-revision REV --no-working-tree-dirty
```

It writes `counts.json`, `counts.md` and `pairs.json` once each and prints the verdict.

## Steps 1 and 2 result (2026-11-08): stop

`bybit-relative-strength counts` from 5ec0de2 (this branch, clean tree, before merge),
2026-11-08, 39 s, on the verified features snapshot. `counts.json` SHA-256
`df83f9647c5e76dfe4c94dd0b900876ea820d24c1a0e9d27263d99e64b121d0a`, `pairs.json`
`9d514c8eacf1ad62fd9487184b60949961a3985d4cd81db52f8bf8aadb787d7b`. Nothing after any
decision time was read for that decision; the only returns in the outputs are pre-t
for their own decision (the 24 hours before t and the seven segments before each
formation window). Across decisions that is not true: a segment of day t can cover an
earlier day's hold of the same instrument (a leader 8 to 14 days before), and by the
labels in `pairs.json` 2,652 of the 9,706 events have such a possible overlap. So hourly
proxies of part of the holds entered the dispersion estimate, never a mean or a net.

**Counts.** 349 decision days, cross-section median 267 (minimum 174), formation and
scale defined for 97.8% of liquid instrument-days. 9,706 units on 592 symbols and 51
weeks, 27 a day (median, minimum 17); top-5 symbols hold 3.3% of the weight; 65.8% of
units were in the previous day's decile. 83.0% of events have a control (median 5, 10th
percentile 0).

**Balance** (weighted, deciding nothing except ln sd):

| Pre-t covariate           | Events | Controls | SMD    |
| ------------------------- | ------ | -------- | ------ |
| ln sd (the gate, <= 0.10) | -4.036 | -4.080   | 0.132  |
| Beta to BTC               | 1.54   | 1.78     | -0.354 |
| 24 h return before t      | +5.7%  | -0.5%    | 0.567  |
| ln turnover               | 16.82  | 16.69    | 0.090  |

**Power** (daily portfolios, inflation 1.5, design effect 1.2):

| Leg             | Days | Day sigma median | F     | MDE, bps | Without inflation, design effect and F |
| --------------- | ---- | ---------------- | ----- | -------- | -------------------------------------- |
| (a) net, hedged | 349  | 318 bps          | 1.163 | 88.7     | 50.0                                   |
| (b) excess      | 349  | 245 bps          | 1.0   | 66.7     | 40.6                                   |

**Floor:** every data item holds; the balance gate fails (0.132) and both legs are above
50 bps. Verdict **`insufficient_balance`**, and the power items fail on their own. HYP-035
v1 stops; nothing is loosened.

**What it shows (decides nothing).** The median spread of a day's book of about 27
leaders against BTC, estimated on the 24-hour segments before its formation window, is
about 3.2% a day, and of their excess over same-day middle-rank coins about 2.4%. Under
the protocol's assumptions (inflation 1.5, design effect 1.2, F) that puts the smallest
detectable effect at about 67 to 89 bps a day, against 42 bps of daily costs. Even at no inflation and no design effect, (a) sits at the 50 bps
limit. A daily relative-strength book is not testable on this window at this precision,
whatever the control. Leaders carry a lower beta to BTC than their controls and a large
last day (+5.7% against BTC), the reversal risk the diagnostics were to show.

**For any later design (none is proposed here).** No minute-level trade return or net of
this setup or its controls has been read; hourly proxies of part of the holds entered the
dispersion estimate only (registry level `outcomes (dispersion only)`), and no mean of
them was computed. A longer hold lowers daily costs but not
the power per trade; only a longer window would, and the window is fixed by the program.

## Effort

Code half a day (reusing HYP-033's instrument and HYP-034's matching, balance and power
parts); counts and power minutes; no fetch before step 2 passes.
