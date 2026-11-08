# HYP-035: relative strength, long against BTC (protocol v1, draft)

Status: **draft for the design touchpoint, 2026-11-08; nothing counted or read.** No
formation return, rank, control or return after any decision has been computed for this
question. It registers nothing until committed after review; any later change needs a
dated amendment before the step it changes runs. It is family 6 of the
[research strategy v2](research-strategy-v2.md) list ("relative strength"), next after
[HYP-033](bybit-squeeze-continuation-v1.md) and HYP-034 (perpetual premium, PR #564)
stopped at `insufficient_data`.

## Question

On Bybit USDT perpetuals, does an instrument in the top decile of the liquid cross-section
by its 7-day return relative to BTC keep outperforming over the next 24 hours: more than
BTC after costs and funding, and more than same-day instruments of similar volatility and
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
- **Scale inputs:** the standard deviation of the instrument's own hourly returns over
  the 336 hours before t (the 80% rule), as HYP-033 and HYP-034.
- **Rank:** the mid-rank of m(t) among the liquid instruments at t with m and the
  scale defined (at least 100 of them, or the day is counted as `thin_cross_section` and
  dropped).
- **Unit (event):** a liquid instrument with rank at least **0.90** (the top decile).
  Every top-decile member is a unit; the same instrument on consecutive days gives
  consecutive, non-overlapping holds.
- **Determinability:** each item is `true`, `false` or `undefined` with a reason
  (`kline_gap`, `history_short`). Undefined instrument-days are counted, never treated
  as false.
- **Entry:** long the instrument and short BTCUSDT for the same notional at the open of
  the 1-minute bars starting t + 1 min (taker). **Exit:** both at the close of the
  1-minute bars ending t + 1 min + 24 h. **H = 24 h fixed.** The formation closes at t
  are hourly kline closes, final at t; the minute of margin is for the order, not for
  publication.
- **Funding:** every settlement inside the hold on both legs, at the HYP-033 rule (rate
  times mark over entry) with the settled rates: the long pays a positive alt rate, the
  BTC short receives a positive BTC rate. Net is reported with and without it; the
  reading uses net with it.
- **Cluster:** a unit is one symbol-day, so each unit is its own cluster and every
  statistic weighs units equally. Units of one day share the market; that is handled in
  the power step and by the ISO-week block bootstrap in the reading.
- **Completeness:** as HYP-033; incomplete units stay in the denominator.

## Costs

Alt leg taker both sides, **30 bps round trip** primary, **50 bps** sensitivity, as
HYP-031 to HYP-034. BTC leg taker both sides, **12 bps** round trip primary (2 x 5.5 bps
fee and 1 bps slippage), **20 bps** sensitivity. Net at the primary costs therefore
carries 42 bps. The break-even alt-leg cost is reported.

## The matched control (fixed now)

The competing explanations are volatility and the alt market, so a control is a unit of
the same day whose rank is ordinary:

- the same decision time t, another instrument;
- rank between **0.30 and 0.70** (the middle of the cross-section);
- the same 24-hour turnover bin (USD 2 to 10 million, 10 to 50 million, at least 50
  million);
- ln(sd_c / sd_e) within **0.25**, where sd is the 336-hour scale input above.

Of those, the at most **5 nearest** by |ln sd ratio|, ties by turnover log ratio. A
control may serve several events of its day; its weight sums over its pairs. The
event's **excess** is its BTC-relative 24-hour price return minus the mean of its
controls' (no funding, no costs: both legs pay the same alt costs, and the BTC leg
cancels).

**Balance gate (learned from HYP-034, fixed now):** on the pairs, the weighted
standardized mean difference of ln sd (weights as in (b), the spreads weighted like the
means) is at most **0.10** in absolute value. Reported beside it and deciding nothing:
the SMD of the 24-hour BTC-relative return before t and of ln turnover. The 24-hour
return is not balanced by design: momentum is the treatment.

## Step 1: counts (outcome-blind)

From the verified features snapshot, with no return after any t: the funnel of
instrument-days (liquid, scale and formation defined, thin days), cross-section size per
day (median, minimum), units, symbols, ISO weeks, the top-5 symbol and week shares, the
share with a control, controls per event (median, 10th percentile), the most events one
control serves, and the frozen pairs (`pairs.json`), hashed.

## Step 2: power (outcome-blind)

HYP-034's method on the frozen pairs: unit weights 1 / n for events and the summed
-1 / (n m_e) for controls in (b); the scale of a unit is the sd of its 24-hour
BTC-relative returns ending at each hourly close of the 336 hours before t (one scale
for both legs: (a) is hedged), inflation **1.5**, design effect
**1.2**, MDE = 2.487 x the square root of the variance, an undefined scale takes the
90th percentile.

**The change: units of one day move together.** In HYP-033 and HYP-034 events were
spread over hours and instruments; here every unit of a day is held over the same 24
hours, and leaders tend to be one theme. So the variance groups units by decision day:

- day d contributes (1 - rho) x sum(w^2 x (1.5 sigma)^2) + rho x (sum(|w| x 1.5 sigma))^2;
- rho is estimated outcome-blind, separately per leg, as the one-way intraclass
  correlation by day of the same units' BTC-relative returns over the 24 hours ending t
  (for (b), of the event minus its controls' mean), floored at 0. These are the moves
  that put the units in the top decile, so rho is likely overstated: conservative;
- overlapping holds on one instrument cannot occur at H = 24 h with daily decisions;
  the overlap bound of HYP-034 is kept for V3.

Sensitivity, deciding nothing: rho at 0, 2 x rho-hat and 1 (each day one movement),
inflation 1.0, 1.5, 2.0, design effect 1.0, 1.2, 1.5. The floor compares unrounded
fractions of integer counts.

**Expectation, stated before counting.** With about 35 units a day over about 350 days,
24-hour sds near 8% and a rho near 0.1, MDE (a) is near 50 bps: a stop at step 2 is
likely. The counts need no fetch and take minutes, so this is the cheap way to find out.

## Floor and stop (fixed now)

At least **150 clusters, 40 symbols, 26 ISO weeks**; top-5 symbols at most **30%** of
units; at least **80%** of events with a control; scale and formation defined for at
least **90%** of liquid instrument-days; the balance gate above; MDE at most **50 bps**
for (a) and (b).

**Stop:** any failure stops HYP-035 v1 at `insufficient_data`, `insufficient_balance` or
`insufficient_power`; the lookback, the decile, the calipers, the horizon and the floor
are not loosened, and the next family is chosen with the owner and the colleague.

## Reading inputs (fetched only after step 2 passes)

Under its own registry row: the 1-minute perp klines at each event's and control's
entry and exit minute, the same for BTCUSDT at each decision day, and the 1-minute mark
price at each settlement inside a hold (funding). About 2 x (units + controls + days)
requests, a few MB, hashed.

## Reading (fixed now, at H = 24 h)

HYP-033's five conditions unchanged, seed 35: floor and at least 95% complete; mean net
(hedged, funding included) at the primary costs at least **+30 bps**; the ISO-week block
bootstrap 5th percentile of the mean net above 0 (10,000 resamples); without the top 5
symbols, and separately without the top 5 weeks, the mean net above 0, and both halves
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

## Effort

Code half a day (reusing HYP-033's instrument and HYP-034's matching, balance and power
parts); counts and power minutes; no fetch before step 2 passes.
