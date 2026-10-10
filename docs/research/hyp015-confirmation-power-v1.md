# HYP-015 confirmation: power note (v1)

Status: planning note for the [confirmation design](hyp015-confirmation-stage-v1.md),
approved with it on 2026-10-10.
Offline arithmetic on a declared grid. It reads no HYP-015 return, no paper row and no
data dated on or after 2026-09-29.

## Why not the #483 calculator

`research_cost_power_planning` resamples the dispersion of HYP-012b, HYP-012c and
HYP-029: source-lead and early-trigger trades held 30 to 60 minutes. A 12-hour long hold
has a different dispersion, so those scenarios would be borrowed, not declared. This note
uses the calculator's own independent-sample formula with a design effect, on a grid
fixed before any HYP-015 return is seen.

## Inputs

- **Events.** The registration's outcome-blind 48 h checkpoint counted 345 eligible
  WATCH, about 172 a day, so about 4,830 in four weeks. 10% is assumed lost to stale
  entries, unresolved positions, accounting and refused orders, which gives
  **n = 4,347 executable pairs**. The rate depends on the market, so half and a quarter
  of it are also shown.
- **Which sample.** Gate D and gate E's paired difference are computed on every
  executable pair, regardless of free slots; gate E's portfolio checks on the slot
  portfolio built from them (design, question 2). This note's `n` is the pairs.
- **Spread of one trade's 12 h net return:** 300, 600, 1,000 and 1,500 bps. A declared
  grid, not an estimate.
- **Dependence.** WATCH decisions on one asset cluster in time, and the registered
  inference resamples asset clusters. This is folded into a design effect `deff` of 1
  (independent), 3, 10 and 30. With `m` decisions per asset and intraclass correlation
  `rho`, `deff = 1 + (m - 1) * rho`; for example 30 decisions per asset at `rho = 0.1`
  give about 4.
- **Rule.** 80% power for a 95% two-sided interval whose lower bound is above zero
  (gate D), so `MDE = (z_0.975 + z_0.80) * SD * sqrt(deff / n)`, with
  `z_0.975 + z_0.80 = 2.80`.

## Gate D: the standalone 720-minute mean over every executable pair

Minimum detectable mean net effect per trade, in bps, for `deff` = 1 / 3 / 10 / 30:

| n (rate)     | SD 300             | SD 600              | SD 1,000             | SD 1,500              |
| ------------ | ------------------ | ------------------- | -------------------- | --------------------- |
| 4,347 (100%) | 13 / 22 / 40 / 70  | 25 / 44 / 81 / 140  | 42 / 74 / 134 / 233  | 64 / 110 / 202 / 349  |
| 2,174 (50%)  | 18 / 31 / 57 / 99  | 36 / 62 / 114 / 197 | 60 / 104 / 190 / 329 | 90 / 156 / 285 / 494  |
| 1,087 (25%)  | 25 / 44 / 81 / 140 | 51 / 88 / 161 / 279 | 85 / 147 / 269 / 465 | 127 / 221 / 403 / 698 |

## Gate E: the paired difference and the portfolio

Gate E has two parts.

- **The paired difference** `d(p)` (720 minus 240 minutes within one probe) needs a 95%
  lower bound above zero over every executable pair. The gate D table applies to it with
  the spread of the difference in place of the spread of the return. That spread is not
  known before a read.
- **The fixed-bank portfolio** checks are point checks without an interval: the 720m
  window PnL must be positive and beat the 240m one by USD 15. How many trades it takes
  depends on how long positions run and on the signal flow. Six slots each holding the
  full 12 h, never empty, give 6 x 2 x 28 = 336 trades in four weeks. That is a
  scenario, not a bound: stops free slots sooner, empty slots give fewer. The noise of
  these checks is of the order of their dollar thresholds. Example, in that 336-trade
  scenario under a normal model with independent trades inflated by `deff` 3: a true
  effect of 44 bps (the gate D threshold at SD 600) is USD 0.22 a trade, so about
  USD 74 in the window, against a standard deviation of about USD 95. The 720m window
  PnL alone is then positive with probability about 78%. That is one condition of
  gate E, not its pass probability: the paired difference and beating the 240m result
  by USD 15 also have to hold, and their joint chance needs assumptions about how the
  three results move together.

## Against the economic threshold

The owner's proposed floor, USD 10 a month at a USD 300 bank, is per taken trade
(USD 50 each):

| Taken trades a month | Floor per trade |
| -------------------: | --------------: |
|     360 (full slots) |         5.6 bps |
|           180 (half) |        11.1 bps |
|         90 (quarter) |        22.2 bps |

## What this means

- **In these scenarios, four weeks detect only a large edge.** At the full rate, with
  moderate dependence (`deff` 3 to 10) and a 600 to 1,000 bps spread, gate D needs a
  true mean of about 45 to 135 bps per trade. These are declared scenarios, not
  HYP-015's measured spread or dependence. The economic floor is 6 to 22 bps. An edge that would pay at
  this scale but sits below about 50 to 100 bps will most likely end
  `insufficient_evidence`. That is a lack of power, not evidence against the edge.
- **Gate E adds its own risk.** In the example its first portfolio condition alone
  fails a real effect at the gate D level about one time in five; the whole gate fails
  it at least that often.
- **Doubling the window** to eight weeks divides these by about 1.4 only if the design
  effect stays the same. If repeats on the same assets accumulate, it grows and the
  gain is smaller. It does not reach the floor either way.
- **The same size governs the 2026-11-04 read.** Nothing changes for it. But a
  `candidate` there means the observed effect was large, and a confirmation on a new
  sample should expect a smaller one (regression to the mean).
- The true spread and dependence are visible only at a read. This note does not
  estimate them and does not pick a cell.

## Decision (2026-10-10, preliminary)

The confirmation keeps four weeks as a bounded budget, and its registration states this
power plainly. It confirms only a large edge; with `insufficient_evidence` nothing goes
to live, which means the confirmation was not obtained, not that the edge is absent.
The 2026-11-04 read is unchanged. The 12 h return net of the market's move is reported
as a diagnostic only: it measures a different edge and does not by itself show that our
trade pays; a real hedge would be a separate strategy with its own costs and capital.
