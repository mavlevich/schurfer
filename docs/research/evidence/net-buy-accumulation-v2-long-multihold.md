# net-buy accumulation v2: LONG multi-hold return distribution (EXPLORATORY)

Companion to `net-buy-accumulation-v2-long-multihold.json` (fingerprint `39aecf30...`,
source parquet sha256 `cbb72f01edcf...`). Tests whether the "accumulation-long is dead"
verdict was an artifact of the 240m hold, by reading the full LONG return distribution
at holds from 4h to 3d. READS RETURNS; exploratory, not a verdict.

Motivation: the LSK example (accumulation on Sep 9-10 in our data -> ~20x on Sep 12-13,
OUTSIDE our window) suggests the payoff horizon is DAYS and the edge, if any, is a fat
right tail (let winners run), not a mean at a 4h hold.

## Result: P-SHAPE, net of 22 bps (the executable primary)

| hold       | mean  | median | win | max   | frac > +10% | frac > +25% | n   |
| ---------- | ----- | ------ | --- | ----- | ----------- | ----------- | --- |
| 240m (4h)  | -0.5% | -0.4%  | 48% | +23%  | 1.6%        | 0%          | 126 |
| 720m (12h) | -0.8% | -2.1%  | 36% | +76%  | 7.1%        | 1.6%        | 126 |
| 1440m (1d) | -2.4% | -4.0%  | 28% | +42%  | 7.9%        | 3.2%        | 126 |
| 2880m (2d) | -1.0% | -3.4%  | 33% | +112% | 9.7%        | 6.5%        | 93  |
| 4320m (3d) | +0.9% | -2.5%  | 39% | +153% | 14.0%       | 5.4%        | 93  |

(P-MAG stays negative at every hold; see JSON. n shrinks past 1d because data ends
~2026-09-11 and late fires cannot resolve a multi-day exit.)

## Reading

This is a positive-SKEW "lottery ticket" signature, not a mean-reversion edge:

- The MEDIAN is negative at every hold and the win rate is 28-48%: most fires drift
  down or nowhere. A naive equal-weight long loses on the typical trade.
- The right TAIL grows with hold length: max return 23% (4h) -> 153% (3d), fraction
  above +10% grows 1.6% -> 14%, above +25% up to ~6%. By the 3-day hold the tail is
  large enough to pull the MEAN net positive (+0.9%) despite 61% of trades losing.
- The true tail is UNDERSTATED here: data ends ~2026-09-11, so the biggest moonshots
  (LSK-style 20x on Sep 12-13) are truncated. The realized winners would be larger with
  data covering the actual pumps.

So "accumulation-long is dead" was indeed largely an artifact of the 240m hold. The
signal is not a steady edge; it is a small-size, multi-day, diversified, let-winners-run
bet whose profitability lives entirely in a fat right tail. That is a fundamentally
different strategy from the 240m mean-reversion framing, and it is fragile (a couple of
winners drive the mean; costs and the median loss must be survived across many bets).

## Implications

- Evaluating a fat-tail multi-day strategy REQUIRES data that CONTAINS the tail events.
  Our 24-day window that ends before LSK's pump systematically truncates exactly the
  moves that make or break it. This is the strongest argument for the data-strategy
  rethink (longer retention, coverage through real pump events, richer features).
- It also reframes the pump-SHORT: shorting these names is shorting lottery tickets, so
  the moonshot (LSK) is a catastrophic short loss (liquidation). Consistent with the
  wall of short liquidations in the LSK report.

## Caveats

Exploratory: one truncated window, no out-of-sample, no uncertainty bands,
equal-weight, pre-liquidity-filter, no funding. Entry `close(t)`, long, gross/net at
each hold. Directional signal only.
