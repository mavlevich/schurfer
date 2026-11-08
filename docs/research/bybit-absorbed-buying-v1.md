# HYP-036: absorbed taker buying, short (protocol v1, draft)

Status: **draft for the design touchpoint, 2026-11-08,** revised the same day after the
first review (variance by hourly blocks, the fetch admitted by fixed calendar blocks with
the full pool, one more warm-up day in the scope) and after the second (rho of a block
known at the earliest decision covering it, step 1 recomputed from raw files, the two
scales stated exactly, seeding of the full fetch from the pilot parts). Nothing has been counted or read
for this question: no flow value has been computed for it, and no return of its events
or controls has been seen. It registers nothing until committed after review; any later
change needs a dated amendment before the step it changes runs. It is family 2 of the
[research strategy v2](research-strategy-v2.md) list ("pressure against price
response"), chosen in the [discovery power budget](discovery-power-budget-v1.md) because
its treatment is the flow, not the price, so controls can share the event's price path.

## Question

On liquid Bybit USDT perpetuals, when an hour of unusually strong net taker buying left
the price inside an ordinary hourly move, does the instrument underperform, over the
next 4 hours, instruments at the same time with the same price path and ordinary flow,
and is a short positive after costs?

This is one test of what the flow adds at a comparable price path. The price path is
held equal by the control, so the excess measures the flow, not the move.

## Assumed mechanism, stated before any outcome

Taker buying that does not lift the price was met by passive selling of the same size:
someone with a large inventory is selling into the bids of aggressive buyers. When the
buying stops, the seller is still there, and the price drifts down. Strategy v2 states
this reading ("buy absorption points to a large seller") and the family's question
("flow that fails to move price predicts a reversal").

This is an assumption the data cannot establish. The tape shows the taker side, not who
was passive, and a flat price under buying can also mean many small buyers against an
ordinary book. The test asks only whether the flow predicts returns beyond the price
path.

Competing explanations, which the control separates:

- **Price path.** Hours with a small move may revert or continue whatever the flow: the
  control has the same last hour and last 4 hours, in units of the instrument's own
  volatility.
- **Volatility.** Calm instruments show small moves under any flow: the control's hourly
  volatility is within a fixed ratio.
- **Market state.** Buying surges cluster in market-wide moves: controls are at the same
  decision hour.
- **Liquidity and activity.** The control is in the same pool tercile and also had at
  least median taker volume for itself.

## How it differs from closed lines

| Closed line            | Its object                                                                                                                        | Difference here                                                                                                                                                                                                                            |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Abnormal-flow v1       | Long after 60 minutes of open interest growth, buy share above 0.5 and contained price, 720 minutes, Schurfer capture 2026-07..09 | Short, the opposite reading of buying without a move; buying is extreme against the instrument's own history, not a share above half; no open interest; 4 h; a same-time control with the same price path; public history 2024-11..2025-09 |
| HYP-032                | Long after masked taker **selling** absorbed inside a compressed day range, open interest growing, funding at or below zero       | The buy side, short; no range, open interest or funding condition; one hour of flow, not six                                                                                                                                               |
| HYP-024 and pump-short | Short after a pump decision                                                                                                       | No pump: the price path is an ordinary hour by construction                                                                                                                                                                                |
| HYP-030                | Long after a 1-minute burst                                                                                                       | No burst; the trigger is flow without a move                                                                                                                                                                                               |
| HYP-033 to HYP-035     | Impulses, premium extremes, relative-strength leaders                                                                             | The treatment is not a price state, so it does not empty the control pool                                                                                                                                                                  |

**Known before this draft.** Abnormal-flow v1's long on buy dominance with contained
price was net negative at every K on its own window (2026-07 to 2026-09, Schurfer
capture). This draft's short side follows the strategy's mechanism, but the team knew
that result, so a pass here would partly mirror a known loss on another window. The
matched excess, not the net, is the test of the flow for that reason.

The mirror question (absorbed selling, long) is a separate family and close to HYP-032;
it is not computed here, not even as a diagnostic.

## Data boundary

- Only Bybit public data: V5 market data and the trade archive on
  `public.bybit.com/trading`. No Schurfer capture, cohort or result is read.
- **Discovery: 2024-10-01T00:00Z to 2025-09-30T23:59Z.** The first decision hour is
  **2024-11-01T00:00Z** (the pool needs 30 days of turnover before its month); the last
  is the latest decision whose exit bar ends by 2025-09-30T23:59Z.
  - **Reuse.** HYP-032 to HYP-035 used this window (features, tape and the dispersion of
    random-hour returns), so it is discovery only for this question. No return of this
    setup or its controls has been read.
  - 2025-10-01 to 2026-09-28 stays HYP-031's reserved test year; the holdout 2023-02-01
    to 2024-09-15 is read only in the strategy's stage 2 batch.
- **HYP-031 tape reservation.** The fixed mask of HYP-032 applies unchanged: no tape
  minute from :55 to :04 around any hour boundary enters a feature, and around HYP-031
  event hours those minutes are reserved (counted only). The flow of the hour before t
  is therefore the 50 kept minutes [t - 55 min, t - 5 min): the feature lags the decision
  by 5 minutes.
- **The pool, fixed now.** For each calendar month M, the 150 USDT-settled
  `LinearPerpetual` instruments (live and delisted, BTCUSDT excluded: it is the
  benchmark) with the largest sum of 1-hour kline turnover over [M - 30 d, M), from
  HYP-032's features snapshot (`features-8522f0c.jsonl.gz`, SHA-256
  `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`), ties by symbol.
  Signals and controls come only from the pool of t's month. Membership uses turnover
  known before the month, so no later winner selects it.
- **Tape scope.** Derived from the first decision of each month, not set by hand. A
  decision at t reads flow in the hour before t; its 336-hour rank history reaches the
  flow of hour t - 337 h, and "first in its episode" needs the ranks of the 8 hours before
  t, so the earliest flow minute a month's decisions need lies in the hour starting
  M - 345 h, on the day M - 15 d. The scope is therefore each member's archive days from
  M - 15 d to the end of M, with a file in the listing. Estimated with HYP-032's size rule
  and record (`archive-sizes-1fedaff`, no new request): **384 symbols, 56,971
  symbol-days, about 526 GB compressed**, for 1,189,464 pool member-hours. Kept
  per-minute parts are about 0.4% of that (HYP-032 kept 614 MB from 168 GB). The fetch
  reuses HYP-032's fetcher and rules (`tape-v3`: per-file checks, reconciliation against
  klines, restartable parts, ledgered byte cap) with this scope.
- **Registry.** Before each step runs: a row for the feature check on HYP-032's tape
  (features), a row for the pilot blocks' tape and counts (features), and a row for the
  full tape and counts (features).

## Event definition (fixed now)

Decision hours t are UTC hour starts. For a pool member at t, from data before t only:

- **B**, the taker imbalance: (taker buy USD - taker sell USD) / (taker buy USD + taker
  sell USD) over the kept minutes [t - 55 min, t - 5 min). Undefined (`no_flow`) when
  the sum is zero, and (`tape_missing`, `tape_mismatch`) as in HYP-032.
- **V**, the activity: taker buy USD + taker sell USD over the same minutes.
- **qB and qV:** the mid-rank of B and of V among the same instrument's values at the
  336 decision hours before t (below plus half of the equal, over the defined ones);
  at least 269 of 336 defined (HYP-032's 80% rule).
- **sigma1:** the standard deviation of the instrument's 336 1-hour log returns whose
  bars end at t - 1 h, ..., t - 336 h, from 1-hour kline closes (80% rule). The bar
  ending at t is left out, so r1 is not in its own scale.
- **z1** = ln(close(t) / close(t - 1 h)) / sigma1 and **z4** = ln(close(t) / close(t -
  4 h)) / (2 sigma1), from 1-hour kline closes (klines are not masked).

**Event:** qB >= **0.95** and B > 0, qV >= **0.50**, and |z1| <= **1.0**: an hour of net
buying in the top 5% of the instrument's own two weeks, on at least its median activity,
whose price moved no more than an ordinary hour.

**First in its episode:** no event-qualifying hour on the same instrument in the 8 hours
before t. This is the independence rule: a hold ends at t + 4 h + 1 min, so holds of one
instrument never overlap. "First" is undefined when any of those 8 hours is undefined.

**Determinability.** Each item is `true`, `false` or `undefined` with a reason.
Undefined decisions are counted, never treated as false.

**Entry:** short at the open of the 1-minute bar starting t + 1 min (taker). **Exit:** at
the close of the 1-minute bar ending t + 1 min + 4 h. **H = 4 h is fixed**, as in
HYP-033.

**Cluster:** symbol-day of t; every statistic weighs clusters equally.

**Completeness:** an event without its entry bar, exit bar, a BTCUSDT bar it needs, or
a funding mark inside its hold is `incomplete`, counted by reason and kept in the
denominator.

**Funding during the hold:** for each settlement s with entry < s < exit, the short's
funding return is +r_s x mark(s) / entry price (HYP-032's rule with the sign of a short).
Net is reported with and without it; the reading uses net with it.

## Costs

Taker entry and exit, **30 bps round trip** primary, **50 bps** sensitivity, as
HYP-031 to HYP-033. The break-even round-trip cost is reported too.

## The matched control (fixed now)

For each event, candidates are pool members at the **same t** that:

- are another instrument and not an event at t;
- have qB in **[0.25, 0.75]** (ordinary flow), qV >= 0.50 and |z1| <= 1.0;
- are in the same pool tercile of the month (turnover ranks 1 to 50, 51 to 100, 101 to
  150);
- have |ln(sigma1 control / sigma1 event)| <= **0.25**;
- have z1 and z4 each within **0.5** of the event's.

Of those, the at most **5 nearest** by sqrt((z1 difference)^2 + (z4 difference)^2), ties
by the absolute log sigma1 ratio, then by symbol. A control may serve several events; its
total weight is accounted for in the power step.

Each leg is measured on its own hold minus BTCUSDT's return over the same interval. The
event's **excess** is the mean BTC-relative price return of its controls minus its own
(the short's gain over the controls; no funding, both legs pay the same costs). The
controls share the event's time, last hour, last 4 hours, volatility and liquidity
tercile, and lack the flow.

**Balance gate (fixed now, from HYP-034's lesson):** the weighted standardized mean
difference between events and their controls (HYP-034 Amendment 1's formula: weighted
variances, the pooled standard deviation as the root of their mean, the weights of leg
(b)) is at most **0.10** in absolute value for z1, z4 and ln sigma1. qV and ln V are
reported.

## Step 1: feature check on HYP-032's tape (outcome-blind, about 0.2 GB of traffic)

HYP-032's tape (`tape-1327ea1/manifest.json`, SHA-256
`21bdf61989a9aac4ebf76e70d260e1aa3541cb66151c362f3189537389d26d50`, rules `tape-v3`,
20,661 symbol-days on 370 symbols) already holds the per-minute taker buy and sell USD
under the same mask. Its days were chosen by price and funding states for HYP-032, and on
it the set of instruments present at one hour is not the pool, so signal frequency,
control coverage and the overlap structure there can differ from the year in either
direction. It is used only to check the feature, never to admit a fetch:

- B and V computed from the parts agree with an independent recomputation on a fixed
  sample of 20 symbol-days (lowest SHA-256 of
  `bybit_absorbed_buying_v1|check|SYMBOL|YYYY-MM-DD`): their raw trade files are fetched
  again (about 0.2 GB, under step 1's registry row and on the owner's go), and B and V are
  summed straight from the trades under the same mask, by a separate code path. The
  kline reconciliation of `tape-v3` checks total volume only, so a swapped buy and sell
  side would pass it and flip the sign of B; this check is for the aggregation;
- the ranks qB and qV agree with a plain recomputation over the frozen 336 hours of the
  same sample (the 20 files alone do not hold that history);
- the mask: no minute from :55 to :04 enters B or V, and the masked minutes kept for
  HYP-032's sensitivity are never read;
- the undefined reasons (`no_flow`, `tape_missing`, `tape_mismatch`,
  `rank_history_short`) are counted.

No event rate, coverage or MDE from this tape is reported.

## Step 2: pilot on fixed calendar blocks (outcome-blind)

Two blocks of 14 decision days, fixed now by the calendar alone, one in each half of the
reading's split: decisions **2025-01-13 to 2025-01-26** and **2025-07-07 to
2025-07-20** (UTC days). Each block uses the whole pool of its month for signals and
controls, and its tape scope by the rule above (from 15 days before its first decision
day): **211 symbols, 8,652 symbol-days, about 82 GB**, for 100,128 pool member-hours (of
the full 1,189,464). The blocks are a subset of the full scope, so a later full fetch
can reuse their verified parts by the seeding rule of step 3.

Only on the owner's go, under its registry row: fetch the blocks with a ledgered byte cap
of **100 GB**, then report:

- determinability of every item over **all planned pool member-hours** in the blocks,
  including hours whose tape is missing;
- the funnel, events and clusters per block, before and after the independence rule, and
  the projection to the full scope (block events times 1,189,464 / 100,128);
- the share of events with a control, the balance table, and the MDE of step 4 on the
  blocks' pairs, scaled to the full window by decision days (the variance times 28 / 334,
  which assumes the blocks' dependence per day holds for the year).

**Pilot gate:** the full fetch is proposed only if determinability is at least 90%, the
projected count after the independence rule is at least **1,500** events, coverage is at
least **85%**, the balance passes, and the scaled MDE is at most **45 bps** for both legs.
The margins over the floor are for the sampling noise of two blocks, not for bias: the
blocks have the full pool. Both blocks are reported separately. A failure stops v1
before the full fetch.

## Step 3: full fetch and counts (outcome-blind)

Only after the pilot gate passes and on the owner's go: recompute the scope's size by
HEAD sampling under HYP-032's rule; stop and ask if it exceeds **650 GB**; then fetch
with a ledgered byte cap of 650 GB (about 13 to 15 hours at HYP-032's 11 MB/s, kept awake
as HYP-032's run had to be). The full fetch is a new run bound to the full scope's SHA-256;
it is seeded with the pilot's parts by importing only the `ok` parts whose symbol-day is
in the full scope, each checked for its SHA-256, its symbol-day, the same features and
HYP-031 index snapshots, the same mask version and the same rules (`tape-v3`); a part
failing any check is fetched again, and an incompatible input refuses the seeding as a
whole. The new manifest keeps each part's origin and reports imported bytes apart from
the full fetch's own traffic. Then on the full tape report:

- the funnel, events by ISO week and month;
- for the events: clusters, symbols, weeks, the top-5 symbol share, the top-5 week share,
  and the share with a control, with controls per event (median, 10th percentile) and the
  largest number of events one control serves;
- the balance table and the MDE of step 4;
- the frozen pairs (`pairs.json`), hashed, the list the reading uses.

## Step 4: power (outcome-blind)

On the frozen pairs, with data before each t only:

- **Units and weights** as in HYP-033: every event hold and every distinct control hold is
  a unit u; with K event clusters, an event in cluster k has weight 1 / (K n_k) in (a);
  in (b) its m_e controls get -1 / (K n_k m_e) each, summed over the pairs a control is in.
- **Scales:** sigma_a(u) is the standard deviation of the instrument's own 4-hour log
  returns ending at t - k x 1 h for k = 0, ..., 335 (80% rule; the last ends at t, before
  the entry at t + 1 min); sigma_b(u) the same on BTC-relative 4-hour returns. This is a
  separate quantity from sigma1. A unit's hourly scale is s(u) = sigma(u) / 2.
- **Hourly blocks.** Every hold starts at t + 1 min, so holds split into four hourly
  blocks on one grid; a unit covers the blocks of its hold. Each block's return of an
  instrument is a common part (share rho of its variance) plus its own part (share
  1 - rho), independent across blocks. rho_a and rho_b of a block are the mean pairwise
  correlation of 1-hour log returns (raw, and BTC-relative) among a month's pool over the
  30 days before that month, taken for the month of the earliest decision whose hold can
  cover the block (t = h - 3 h for block h). That estimate is known at every decision
  covering the block, so a hold across a month boundary never uses prices after its own
  decision, and all units in one block share one rho.
- **Variance:** 1.2 x 1.5^2 x the sum over blocks h of [rho_h x (sum over units covering h
  of w(u) s(u))^2 + (1 - rho_h) x sum over instruments i of (sum over units of i covering
  h of w(u) s(u))^2]. A single unit gets sigma(u)^2; two holds of one instrument that
  share three blocks get covariance 0.75 x sigma x sigma, and of two instruments 0.75 x
  rho x sigma x sigma; the signs of the weights in (b) are kept. The inflation 1.5 and the
  design effect 1.2 stay as planning assumptions (for higher volatility after t and for
  dependence the block model misses, such as autocorrelation of hourly returns). MDE =
  (1.645 + 0.842) x its square root, for (a) and (b).
- An undefined scale takes the leg's 90th percentile, counted.
- Sensitivity, reported, deciding nothing: inflation 1.0 and 2.0, design effect 1.0 and
  1.5, rho halved and times 1.5.

## Floor and stop (fixed now)

The study goes on to the reading only if all hold, checked in this order (data, balance,
power):

1. at least **150 event clusters, 40 symbols and 26 ISO weeks**; the top-5 symbols hold at
   most **30%** of clusters; at least **80%** of events have a control; every item
   determinable for at least **90%** of pool member-hours;
2. the balance gate;
3. MDE at most **50 bps** for (a) and (b).

**Stop:** if any item fails, v1 stops at `insufficient_data`, `insufficient_balance` or
`insufficient_power`; the event, the control, the horizon and the thresholds are not
loosened. Counts are features-level exposure only.

## Reading inputs and reading (after the floor, on the owner's go)

Inputs, under their own registry row: 1-minute perp klines for each event and control
hold, 1-minute BTCUSDT klines for the same intervals, the 1-minute mark-price kline at
each settlement inside a hold; every response hashed.

Discovery passes only if all hold:

1. the floor, and at least 95% of events complete;
2. **size:** cluster mean net of the short at 30 bps, funding included, is at least
   **+30 bps**;
3. **uncertainty:** the 5th percentile of the ISO-week block bootstrap of the mean net is
   above 0 (10,000 resamples, seed 36, whole ISO weeks; pairs resample together);
4. **not one corner:** without the 5 symbols with the largest total net, and without the
   5 ISO weeks with the largest total net, the mean net stays above 0; each half (t
   before 2025-05-01, and from it) has a positive mean net;
5. **more than the price path:** the cluster mean excess is at least **+30 bps**, with its
   week-block bootstrap 5th percentile above 0.

A negative mean net at 30 bps is `fail` whatever else holds. A pass makes the primary a
stage 1 candidate; never orders.

## Variants (declared now, each read once after the primary, never the primary)

- **V1 four-hour flow:** B, V and their ranks over the kept minutes of the 4 hours before t,
  the event's price condition |z4| <= 1.0, controls on z4 and z1 as above.
- **V2 wider flow:** qB >= 0.90.
- **V3 tighter price:** |z1| <= 0.5 for events and controls.

Each enters the [discovery ledger](discovery-ledger.md) with the count: primary plus 3,
each with its own control pool under its own rule.

## Diagnostics (reported, decide nothing)

- Net and excess by qB band (0.95 to 0.98, above 0.98) and by pool tercile.
- Net without funding, funding as a share of net, excess without the BTC adjustment.
- By month, by BTC 24 h return sign, by hour of day of t.
- Maximum adverse excursion in the hold (99th percentile).

## Effort

Feature check: one module reusing HYP-032's tape reader, about half a day. Pilot blocks:
the matching, power and floor code (reusing HYP-033 to HYP-035's), about a day with
tests, plus a few hours of fetch. Full fetch: unattended, about 13 to 15 hours. Counts on
the full tape: hours. Reading: about a day. Stop at any failed gate.
