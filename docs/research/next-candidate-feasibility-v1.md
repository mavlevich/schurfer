# Next candidate after HYP-036: family 5 against majors (planning note v1)

Status: **planning note, 2026-11-09; decides nothing and registers nothing.** Asked by
the colleague and the owner after the [HYP-036 matching check](bybit-absorbed-buying-v2-matching-check.md)
failed: compare a majors-only study with family 5 on three items before any return,
starting with what data already held can check. Both use pre-t features only; no
request, no trade return. The definitions below exist for this estimate; neither is a
protocol.

Inputs: the features snapshot (`a3b54d6c...`), HYP-036's sealed pilot tape (manifest
`3885a196...`, scope `315a343b...`) and HYP-032's archive-size record (`22e7c119...`).
Code `bybit-next-candidate-feasibility` from ed0b184 (clean tree): [`result.json`](evidence/next-candidate-feasibility-v1/result.json),
SHA-256 `76ba70cde9123fec882e53dd202ea154bc8f8d50633aad11e9b97cb28bfbfe9c`.

## The two candidates as estimated

**Family 5, post-pump short on an open-interest turn**, on HYP-036's monthly top-150
pool over the whole window (2024-11-01 to 2025-09-30):

- Pump: the first hour whose 24 h close-to-close return is at least P (one per instrument
  per 72 h).
- Event: the first hour k = 1 to 24 after it where open interest is at least D below its
  peak since 24 h before the pump, while at least half of the pump's rise is still held.
- Control: other pumps not turned by the same k (a risk set), within 0.10 of the held
  share, a pump size within 0.25 in log, and the v1 sigma1 ratio; up to 5 nearest.
- Primary P = 20%, D = 5%; a grid of P 15/20/30% and D 3/5/10% for the counts.

**Majors (family 2)**: HYP-036's event (qB >= 0.95, B > 0, qV >= 0.5, abs z1 <= 1, first
in 8 h) on the monthly **top 10** of the same pool (BTC excluded: it is the benchmark and
not on the tape). Only the pilot tape's two blocks hold the flow, so counts are scaled
from 28 days. Two comparisons: the same hour among the top 10 (the matching check's
rule), or the **same instrument within 7 days** (outside the event's 8-hour episode,
same calipers).

## Results

| Item                     | Family 5 (P 20%, D 5%)                                             | Majors, same instrument within 7 days                                                     |
| ------------------------ | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| Independent observations | **1,247** events, 304 symbols, top 5 8.0%                          | **52** in the blocks (50 clusters, 13 symbols), about 620 a year                          |
| Control found            | 98.2%                                                              | 84.6% (44 of 52); same hour only 25.0%                                                    |
| Balance, abs SMD         | held share 0.01, ln sigma1 0.04, turnover 0.08, **pump size 0.17** | z1 0.07, z4 0.02, ln sigma1 0.03 (turnover 0 by design); **qV 0.80**, ln V 0.30           |
| MDE (a) net, (b) excess  | **110.8, 75.3 bps**                                                | **36.4, 34.0 bps** (scaled to the full window)                                            |
| Data                     | held (features snapshot)                                           | held for the two blocks only; a full year of the top 10 is 24 symbols, about **120.5 GB** |

Family 5's grid: events from 378 (P 30%, D 10%) to 2,426 (P 15%, D 3%), control coverage
94.8% to 99.0% everywhere; at the largest count the MDE is still **79.1 and 53.9 bps**,
with pump size SMD 0.16. 77% of 20% pumps turn by 5% while held, 54% of them within 2
hours.

## Reading

**Family 5 fails on paper.** Events and controls are plentiful, but a pump coin's 4-hour
spread is so large that even 2,400 events leave both legs above 50 bps. The contrast is
also weak: most pumps "turn" within an hour or two, so with-turn against not-yet-turned
separates little, and the turned pumps are larger (size SMD about 0.16). Under the
[power budget](discovery-power-budget-v1.md) rule, a candidate that fails any of the
three items on paper is not built.

**Majors pass on power, but on thin, seen evidence.** The top 10's lower spread puts both
legs near 35 bps with about 620 events a year. Against it:

- 52 events from 28 days: the yearly rate is uncertain by about a quarter either way.
- Same-hour controls do not exist among ten names (25%); only the same instrument a few
  days apart works, which asks a different question (an hour against its own ordinary
  hours, not against other coins at that hour). Coverage 84.6% sits at the edge.
- Events are busier than their controls (qV SMD 0.80): a protocol would have to match or
  stratify on activity, which may cost coverage.
- Monthly rho among the top 10 is 0.52 to 0.72: little cross-sectional diversification,
  already inside the MDE.
- These blocks' features are seen (HYP-036), so any rule must be checked elsewhere; v1's
  floor of 40 symbols cannot hold with 24 names a year and would need a stated
  replacement.

## Position

Neither goes to a protocol yet. Family 5 stops here on paper. For majors, the cheapest
next step that answers the open doubts is **counts on two new fixed calendar blocks** of
the top 10 only (no return): about 15 to 20 GB of tape, to be sized by the scope rule
before asking for the fetch. It checks the event rate, same-instrument coverage and the
activity imbalance outside the seen blocks. If they hold, a majors protocol follows with
its own floor; if not, the program takes the strategy's fallback and changes the kind of
edge.

Alternatives: write the majors protocol now on these numbers (faster, but rests on 52
events in seen blocks), or skip majors and take the fallback now (gives up the only
candidate that passes power on paper).
