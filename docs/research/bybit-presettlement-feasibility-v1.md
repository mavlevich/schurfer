# HYP-031 family, entry before settlement: can the signal be rebuilt at decision time? (B1, feasibility design v1)

Status: **design, 2026-11-11, revised the same day after the first review (no event
selection by the settled rate; caps, schedule and liquidity at decision time; S1 kept
apart from the signal) and after the code review (forecasts superseded by settlements
known at decision time; a recent-extreme filter instead of a cap proof; S1 minimum
counts and coverage), fixed before any fetch; registers no hypothesis and reads no
return within this protocol.** Step B1 of track B in the [program plan](program-plan-2026-11.md). Nothing
here runs until the owner's go for the fetch below.

## Question

A long opened before a funding settlement must be chosen with the information available
at the decision time t - d, not with the rate settled at t. Can that rate be rebuilt from
Bybit's public history, closely enough to build the event list without look-ahead? The
minute discovery's +35.5 bps pre-settlement diagnostic selected its events by the
settled rate, so it is not reused as a result. If B1 passes, the event list is rebuilt
from scratch on the decision-time rate.

## What Bybit documents

- **Formula.** Funding rate F = average premium index P + clamp(interest rate I - P,
  0.05%, -0.05%). The premium index is [max(0, impact bid - index) - max(0, index -
  impact ask)] / index. I and P are calculated **every minute**, and P is a time-weighted
  average over the interval with **linearly increasing weights**: minute i of N weighs i,
  so later minutes count more ([Bybit help center](https://www.bybit.com/en/help-center/article/What-is-Funding-Rate-and-Predicted-Rate)).
- **Caps.** Upper limit = min((initial margin rate - maintenance margin rate) x 0.75,
  maintenance margin rate), lower limit symmetric; Bybit may adjust them temporarily in
  volatile markets ([announcement page](https://www.bybit.tr/en-TR/announcement-info/fund-rate)).
- **Data.** The [premium index kline](https://bybit-exchange.github.io/docs/v5/market/premium-index-kline)
  endpoint returns OHLC at 1 minute and longer, up to 1,000 bars per request. The
  [funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)
  returns the settled rate and its timestamp only. The
  [instruments endpoint](https://bybit-exchange.github.io/docs/v5/market/instrument)
  gives the **current** interval and caps only; no history of either is published there.

## The decision time matters more than expected

With linear weights, the share of the interval's total premium weight already observed
at t - d is ((N - d)(N - d + 1)) / (N(N + 1)), with N the interval in minutes. It is a
share of weight, not an accuracy of the forecast, especially after the clamp and the
caps:

| Interval N | d = 30 min | d = 15 min | d = 10 min | d = 5 min |
| ---------- | ---------: | ---------: | ---------: | --------: |
| 1 h        |      25.4% |      56.6% |      69.7% |     84.2% |
| 2 h        |      56.4% |      76.7% |      84.1% |     91.9% |
| 4 h        |      76.6% |      87.9% |      91.9% |     95.9% |
| 8 h        |      87.9% |      93.9% |      95.9% |     97.9% |

So 30 minutes before a 1-hour settlement, three quarters of the premium weight is still
unobserved.
In the minute discovery's events, 1-hour contracts were 1,049 of 4,928 (21%), 2-hour 459,
4-hour 1,955 and 8-hour 1,459. The decision time is therefore a design choice, not a
detail: d = 30, 15 and 10 minutes are each computed below, and each counts as a variant
in the [discovery ledger](discovery-ledger.md). A shorter d sees more of the rate and
less of any drift.

## Inputs

- **Settlements and settled rates:** the pinned minute discovery index
  (`index-d87b3a2.jsonl.gz`, SHA-256 `f48fe09f...`), settlements 2024-10-01 to
  2025-09-30. Settled rates are used only by S1 (validation) and as diagnostics; they
  never select an interval for the event list.
- **Hourly liquidity:** the hourly perpetual turnover already in the same index (its
  `kline_60` records), only bars that ended by t - d.
- **New fetch 1, hourly premium index klines** for every instrument of the universe over
  the discovery year (about 9 requests per instrument). HYP-034 read the same series at
  the features level for most of this window.
- **New fetch 2, 1-minute premium index klines** for every settlement interval in the
  **superset** below, plus a validation sample (S1). Every request ends no later than
  2025-09-30T23:59:59.999Z, live and delisted instruments. One request per interval (at
  most 480 bars). A dry run prints the counts before the first request.
- **Not fetched:** no perpetual price, kline, trade or order book. No return is computed.

**The superset, chosen without the settled rate.** Whatever the interest rate, the clamp
keeps F at or above P - 0.05%, and a cap only moves F toward zero. So F_hat <= -0.30% at
any decision time needs P_hat <= -0.25%, and P_hat, a weighted average of minute values,
is at or above the lowest of them, which is at or above the lowest hourly low covering
the interval. Every interval whose lowest hourly premium low over its hours is at most
**-0.20%** (a margin of 0.05 points) enters the superset, so no interval that could
trigger at any d is missed, and none is chosen by its final rate. S1 checks on the
fetched minutes that each minute close lies within its hour's low and high; a violation
is reported and, above 0.1% of minutes, the superset is rebuilt with a wider margin
before S3.

**Amendment 2, the event superset (2026-11-12, after the first report).** The first
report (`87fabe3`, result `4cc65107`) ended `superset_rebuild_needed`: 59.6% of minute
closes lay outside their hour's low and high, far above the 0.1% limit. A scratch check
on 33,929 full hours found that Bybit's hourly premium index open and close equal the
first minute's open and the last minute's close in every hour, but the hourly low and
high are not the minute extremes: some minute low lies below the hourly low in 89% of
hours. So the bound above does not hold for this series, and no margin on the hourly
low is a proven bound. The fix drops the premium bound for S3 instead of widening it:

- **S3 runs on the event superset:** every interval that passes, at some d, all S2
  conditions that do not depend on the premium. These are: the forecast is not
  superseded, the schedule is certain, the liquidity floor holds, and the recent-extreme
  filter holds. An admitted event must pass all of them, so the event superset contains
  every possible event by construction, and none is chosen by its premium or its final
  rate.
- **A third fetch** gets the minute premium of the event-superset intervals that the
  second fetch did not request, into a separate pinned snapshot. The report reads both.
- **S1 is unchanged.** Its pool stays the hourly-low superset plus the validation
  sample, as registered, and its thresholds do not move. S1 already ran on that pool;
  its figures are not re-tuned.
- **The minute-against-hour check becomes a diagnostic.** It no longer gates S3,
  because S3 no longer relies on the hourly low. `superset_rebuild_needed` now means
  only that some event-superset interval has no minute data and no recorded fetch
  error, so the event list could be incomplete.

Nothing else changes: the trigger, the filters, the decision offsets, the floors and the
per-class gate stay as registered. The first report's S3 counts were seen; they are on
the old superset and are superseded by the rerun.

**Amendment 3, four-hour prior-only run (2026-11-13, after review and before any
supplemental fetch).** This section overrides the old hourly selection, S1 pool,
pooled S3 and inherited hourly-gate wording below. The original failed reports remain
unchanged. No strategy return has been read **within this protocol**; the exploratory
pre-settlement returns and post-hoc choice of 4h in #582 remain part of the selection
history and later confirmation correction.

- **Remove the failed 0.1% minute/hour gate explicitly.** It was a registered S1
  guard and it failed after the first read (about 59.6%; the independently retained
  local result is 59.5358%). We are changing that guard after observing its failure.
  Its sole purpose was to protect the hourly-low selection proof. The new selection
  does not use hourly premium, so that proof and guard are retired; the measured
  discrepancy stays in S1 as `hourly_range_exceeded`, a diagnostic only. No formula,
  sensitivity, specificity, coverage, minimum-count or event-floor threshold changes.
- **S1 pool:** the 4h prior-only candidates plus the original unchanged 2,000-interval
  validation sample across all classes, a union of 19,845 intervals. A 4h class pass
  still requires its own S1 and S3 floor and the pooled S1 formula gate. The historical
  hourly-selected pool is not reused for the new acceptance decision.
- **S3 and the reported primary verdict are only 4h.** Pooled S3 and 1h/2h/8h/other
  S3 are `not_fetched`, never a floor or a verdict computed from the sample's partial
  population. No outside class may become `pass` or silently contribute `premium_gap`
  rows to the active denominator. Top-level `verdict` is explicitly the 4h verdict;
  `pooled_s3_verdict` is `not_fetched`.
- **Validation is S1-only.** Sample membership never creates an event. The 75
  non-4h sample intervals that independently pass C remain S1-only. Of the 17,892
  actual 4h candidates, 47 also belong to the sample: they remain S3 candidates by C,
  not by their sample membership; excluding those would arbitrarily delete signals.
- **One superset across d on this index.** All 49,286 all-class candidates pass C at
  d30/d15/d10 together because the funding timestamps and liquidity buckets lie on
  hourly grids. There are not three independently selected supersets. The three
  decision-time F_hat values are still calculated separately.
- **Missingness:** decision-prefix coverage keeps the full fixed candidate denominator
  and must be at least the existing 95% coverage threshold at a passing d. Sparse
  survivors cannot produce a pass. An incomplete active event path is
  `event_data_incomplete`; classes outside the run remain `not_fetched`.
- **Immutable inputs and conflicts:** reuse the original pinned minute snapshot,
  write a supplemental snapshot, and refuse conflicting duplicate closes or native
  OHLC values. Equal observations fold, never last-file-wins. The report has a new
  version `bybit_presettlement_feasibility_prior_only_4h_v2`; raw snapshot format v1
  remains readable and old reports are not rewritten.
- **Bounded run:** the 64 fixed pilot IDs, 11,660 supplemental requests, unchanged
  financial thresholds and budgets are specified in the
  [scope-repair proposal](b1-minute-scope-repair-proposal-v1.md). The old unbudgeted
  minute-fetch CLI is disabled. No new fetch is authorized by this revision; the
  persisted-budget/pilot runner must be implemented and reviewed before the owner's
  separate fetch go is used. This commit executes only synthetic tests and a
  classify-based containment audit on the pinned index.

**Exposure.** The premium index inside [t - d, t] overlaps the would-be hold, and premium
moves with the perpetual's price. This is laboratory exposure on the discovery year,
which the program plan allows; it gets its row in the
[seen-windows registry](seen-windows-registry.md) before the fetch. The test year is not
touched.

## Steps and gates (fixed now)

**S1, the method reproduces the settled rate (validation only).** On the superset
intervals and on a validation sample drawn independently of rates (2,000 intervals,
uniformly over instruments and settlement times, seed 31), rebuild F from the full
interval's minute closes: linear weights, I assumed at 0.01% per 8 hours scaled to the
interval (Bybit's usual default, an assumption S1 tests), the ±0.05% clamp. Here, and
only here, the settled rate may be used: caps inferred after the fact from settled rates
pinned at a repeated extreme value are a diagnostic of S1, and intervals at such a value
are reported apart.

**Coverage and counts come first.** The S1 pool is the superset plus the validation
sample. Among its intervals whose schedule held (the settlement came at the predicted
time), at least **95%** must have complete minute data; a fetch error or a gap stays in
that denominator. Premium OHLC values must be finite: snapshot loaders reject `NaN`
and positive or negative infinity. A non-finite observation passed directly to the
calculation makes that interval undefined, so it cannot count as complete or produce
an admitted signal. A non-finite formula result is rejected.
At least **300** settlements at most -0.10% must be scored, with at
least **50** on each side of -0.30%. Settlements at a possible cap are excluded from the
score and reported with their own count and share. If any of these fails, S1 is
`insufficient_data` and B1 ends `insufficient_validation`: never a pass on what is left.

Gate: among the scored settlements, the rebuilt rate is within **0.02 percentage points
(2 bps)** for at least **95%**, and the -0.30% classification holds **on each side of the
trigger separately**: at least **95%** of settlements at or below -0.30% are rebuilt at or
below it (sensitivity), at least **95%** of settlements between -0.30% and -0.10% are
rebuilt above it (specificity), and at least **98%** agree overall. A large ordinary group
can no longer hide a failure on the extreme side: 50 extreme settlements all missed among
3,000 scored still give 98.3% overall agreement, and that case is a regression test.
The share within 0.005 points is
reported as a diagnostic. 2 bps, not 0.5: a 1-minute close may differ from what Bybit
averages inside the minute, and a gate that tight could end B1 `not_reconstructible` on
rounding alone, while 2 bps is small against a 30 bps trigger. If the gate fails, B1
ends `not_reconstructible`, and the entry before settlement stops. A pass shows the
formula and the data reproduce the end of the interval; it does not show that the
mid-interval value matches anything Bybit displayed (S2), nor the quality on the
excluded possible caps.

**S2, the decision-time signal.** Everything in S2 uses only what was available at t - d:

- **Premium:** the minutes closed by t - d - 1 min; P_hat = their weighted average with
  their own weights, renormalized; F_hat = P_hat + clamp(I - P_hat, ±0.05%).
- **Schedule, rebuilt at each decision time:** a forecast opened by settlement s1
  predicts the next one at s1 plus the last observed interval. If any settlement after
  s1 is already known at t - d, the forecast is `superseded`: the averaging window
  restarted there, and that settlement opens its own forecast (whose premium window
  starts at it). A settlement after t - d never cancels a forecast. If the last two
  intervals before s1 differ, the event is `schedule_uncertain`. Whether the actual
  settlement came at the predicted time is a diagnostic, never an input.
- **Caps are not rebuilt.** No historical cap is published, Bybit may change caps at
  any time, and a past settlement does not prove the cap in force at t - d. So F_hat is
  the uncapped rate implied by the premium, and the signal adds a filter of its own
  instead: **recent extreme funding**, a settlement at or below -0.30% on the same
  instrument within the 7 days before t - d. Without it the event is
  `no_recent_extreme`, counted and not admitted. The strategy studied is therefore
  "premium-implied rate extreme now, and funding extreme in the past week"; nothing here
  claims to reproduce the capped rate in force.
- **Liquidity:** perpetual turnover over the 24 hourly bars closed at or before t - d is
  at least USD 2,000,000 (the minute protocol's level, moved to bars available at the
  decision); universe membership from listing and delivery dates known at t - d.

**What F_hat is called.** No history of the rate Bybit displayed before settlement is
public. F_hat is therefore a **computed signal from the premium available at decision
time**, not "the rate traders saw". The comparison with the displayed rate is a live check
for the shadow stage (B5), after v2's terminal state; until then no claim about the
interface is made.

**S3, the rebuilt event list.** For each d, events are admitted intervals of the
superset with the S2 trigger, clustered by symbol-day as in the minute protocol (a
cluster's events are averaged first). Everything below is reported for the pooled list
**and separately for each settlement interval** (1 h, 2 h, 4 h, 8 h, other), because the
observed weight and the behaviour before settlement differ by interval; B3 keeps the
same split for every result:

- counts: events, clusters, symbols, ISO weeks, top-5 symbol share, and the floor;
- `superseded`, `no_recent_extreme`, `schedule_uncertain` and illiquid shares;
- agreement with the settled trigger: precision and recall of F_hat <= -0.30% against
  the settled rate <= -0.30%. A diagnostic only: the event set is meant to differ from
  the minute discovery's.

Gate: at least one d meets the minute protocol's floor (**150 clusters, 40 symbols, 26
weeks, top-5 symbols at most 30%**) on the pooled admitted events. The values of d that
pass go to B2 (the maker fill model) and B3 (the laboratory read), and nothing else does;
an interval class that misses the floor on its own is read in B3 as a diagnostic only. If none passes, B1 ends
`insufficient_events`.

**Gate for each interval class (amendment, 2026-11-12, before any fetch).** The program
plan carries 4-hour contracts forward, and that choice was made after seeing exploratory
returns. A pooled `pass` therefore does not open B2 for a class. Each class (1 h, 2 h,
4 h, 8 h) gets its own verdict in the report's `by_interval`, with the same names:

- S1 run on that class's settlements in the pool alone, with the same thresholds and
  minimum counts (coverage 95%, 300 scored, 50 on each side, 2 bps for 95%, sensitivity
  and specificity 95%, agreement 98%). Too few is `insufficient_validation` for the class.
- The class's own S3 floor at some d. Without it the class is `insufficient_events`.
- A class never passes where the pooled formula check does not: a pooled
  `insufficient_validation`, `not_reconstructible` or `superset_rebuild_needed` is
  inherited.

B2 for 4-hour contracts starts only on `by_interval["4h"]` = `pass`, at the values of d
in that class's own `passing_decision_offsets`. The other classes are fetched and
reported as diagnostics.

## What B1 does not decide

It reads no perpetual price and no return, so it says nothing about an edge. It fixes
only whether, and at which decision times, the pre-settlement event can be defined
honestly. The exit, the fill model, costs and the exam on the test year belong to B2 to
B4.

## Effort and approvals

- Code: reuse the minute protocol's fetcher, snapshot hashing and index reader; a
  rebuild module with tests on synthetic intervals (known weights, clamp and caps). About
  one day.
- **Owner go** for the fetch, after the dry-run count. The registry row lands before the
  first request.
- Colleague: this design before the code, one review before merge.
