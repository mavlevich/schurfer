# Medium-term BTC trend, long or cash: can it be tested? (planning note v1)

Status: **planning note, 2026-11-09; decides nothing and registers nothing.** The
colleague proposed it as the strategy's fallback after family 2 failed its comparison
three ways on Bybit ([liquid top-10 pilot](bybit-liquid-top10-pilot-v1.md)): does one
daily long/cash rule on BTC, fixed in advance, add value after costs against a constant
BTC share and simple volatility targeting? Whole strategies on the same dates, so no
pairing. This note answers the colleague's first step: the clean windows, the
independent weeks, the turnover and costs, and the effect that can be told apart. It
reads no strategy return: the only market inputs are BTC's weekly and daily spread and
BTC's funding over 2024-10-01 to 2025-09-30, both already seen as features (HYP-032 to
HYP-036 used BTC as their benchmark).

## Clean windows for BTC

From the [registry](seen-windows-registry.md) and [strategy v2](research-strategy-v2.md):

| Window                   | Status for this study                                           | Weeks          |
| ------------------------ | --------------------------------------------------------------- | -------------- |
| 2024-10-01 to 2025-09-30 | Discovery (BTC spread and funding seen, no trend return)        | 52             |
| 2023-02-01 to 2024-09-15 | Backward holdout: one read, for the whole frozen batch          | 85             |
| 2025-10-01 to 2026-09-28 | HYP-031's reserved test year                                    | 0              |
| From 2026-09-29          | Forward, usable only after v2 is terminal                       | about 6 so far |
| 2022                     | Left unread by strategy v2                                      | 0              |
| 2020-03 to 2021-12       | Outside the program's data plan; BTC's path is public knowledge | 0              |

So at most **52 weeks** for discovery and **85** for the one confirmation read; 137 in
all. Splitting them by market regime would need the price path, and there are too few
weeks for regime cells to mean anything either way.

## The spread of the comparison

On 2024-10 to 2025-09, BTC's weekly log-return spread is **5.53%** (40% a year; daily
2.29%). A long/cash rule in the market a share p of the time against a constant BTC share
of p (same average exposure) differs each week by (I - p) r; if weekly returns are
nearly independent, its spread is the root of p(1 - p) times BTC's, **2.77% a week** at
p = 0.5. Serial dependence (which is what a trend rule exploits) changes this somewhat;
it does not change the order of magnitude. Volatility targeting as the baseline gives a
similar spread.

## Detectable effect

MDE of the mean weekly excess, one-sided 5%, power 80% (2.487 standard errors), raw and
under the program's floor (inflation 1.5, design effect 1.2):

| Weeks          | MDE per week | Per year, raw | Per year, floor |
| -------------- | ------------ | ------------- | --------------- |
| 52 (discovery) | 0.95%        | **49.6%**     | 81.5%           |
| 85 (holdout)   | 0.75%        | 38.8%         | 63.7%           |
| 137 (both)     | 0.59%        | 30.6%         | 50.2%           |

A plausible edge of a BTC trend rule over a matched static share is a few to about 15%
a year, mostly from avoiding large drawdowns. To tell **10% a year** apart takes about
**1,280 weeks raw (25 years), 3,450 under the floor**; 5% a year takes four times that.
In Sharpe terms (correlation with the static share about 0.71 at p = 0.5), the MDE of
the Sharpe difference is 1.9 on the discovery year and 1.2 on all 137 weeks, against a
plausible 0.3 to 0.5.

## Turnover and costs

- A medium-term rule (signals of 50 to 200 days) switches roughly 5 to 15 times a year.
- **Spot:** taker fee about 0.1% per side; at 10 round trips about 2% a year. No funding.
- **Perpetual:** taker fee about 0.055% per side, plus funding while long: BTC's funding
  summed to **7.7% a year** over the discovery year (87% of settlements positive), so
  about 3.9% a year at p = 0.5. That alone is the size of a plausible edge.
- **USD 50:** the BTCUSDT perpetual's minimum order is 0.001 BTC (about USD 100 of
  notional at today's prices), so volatility targeting cannot be done in steps finer than
  the whole position; spot's minimum is about 1 USDT. Both to be checked against the
  venue's instrument data before any protocol.

## Reading

**The comparison is clean but cannot be powered on the program's clean data.** The MDE
is three to ten times any plausible effect on every window the program may read, and
the costs on the perpetual (funding) are as large as the effect. Spot is the only
sensible venue for it at this size.

## Position and alternatives

My position: do not write a protocol for a BTC trend test. It would read the discovery
year and then spend the holdout on a test that cannot pass at any realistic effect. The
team should decide the fallback's direction instead:

1. **Use it without a test, as risk control.** A pre-declared long/cash rule on spot,
   adopted for drawdown control rather than as an edge, sized by the owner's choice; no
   claim of added return. Cheap, honest, and outside the research gate.
2. **Widen the data for trend only:** amend strategy v2 to admit 2020-03 to 2022 for
   this one study. It roughly triples the weeks (still short of 1,280) and those years'
   path is public knowledge, so it would add little evidence.
3. **Trend across many liquid instruments:** more units per week, but the top names move
   together (monthly rho 0.52 to 0.72 among the liquid top 10), so the effective number
   per week is about 1.5 to 2: it does not close a 25-year gap.
4. **Another kind of edge altogether**, still to be named, under the same three-item
   gate before any code.
