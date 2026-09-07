# HYP-021 — The exit-policy family has been measured against a policy that is not production

**Status: registered, not read.** No net return, exit-reason count or per-policy
statistic from the population below has been queried at the time of this commit.

Registered 2026-09-07.

## What is already established, and is not the hypothesis

Two facts about the code, both verifiable without reading any outcome:

1. `virtual_strategy.BASELINE_EXIT_POLICY` carries the version string
   `production_max_hold_v1`. It was introduced on 2026-07-28 (`dcc7507`), when it
   did describe production.
2. Production gained a no-progress exit on 2026-08-18 (`422f784`). Since then
   `exit_params` returns `no_progress_min: 60.0` in all three pump bands, and the
   exit engine closes a position at 60 minutes when trailing never activated.

So since 2026-08-18 the reference policy in the replay has held a stalled position
to `max_hold_min` — 180, 240 or 360 minutes depending on band — where production
would have closed it at 60. That is a fact about two commits, not a finding.

The family's `no_progress_60m` challenger does not close this gap. Its rule is a
rolling stall detector: exit when no new favourable extreme beyond
`minimum_progress_pct` has appeared in the last 60 minutes, with a bounded
extension past `max_hold_min`. Production's rule is a single absolute check at 60
minutes on whether trailing ever activated, with no extension and no progress
step. Same name, different rule. `ExitPolicy.__post_init__` currently rejects
production's shape outright: a `no_progress_minutes` policy is required to carry
both a positive `minimum_progress_pct` and a non-zero `max_extension_minutes`.

## Question

With the policy production actually runs included in the family as the reference,
does the ranking of the registered exit policies change, and does production's own
policy beat or lose to the baseline it was assumed to be?

This matters beyond bookkeeping. Production's rule closes a short that is up 7% at
the 60-minute mark, because 7% is below the 8% activation threshold for the
sub-50% band. Whether that is cheap insurance or a systematic donation of winners
is unknown, and is what this pass measures.

## Population

The existing replay cohort, unchanged: `ReplayFilters` defaults — strategy version
`pump_short_v1_market_quality`, resolver version pinned, `allow_fallback` false, so
proxy market paths never enter the metric. Episodes without a complete market path,
without a recorded `signal_position_usd`, or without decision-time bid/ask impact
are reported as coverage and excluded from every metric, as the family already does.

## Windows

- **Discovery (this pass):** `2026-07-17` through `2026-08-18`, half-open.
- **Held out, not read in this pass:** `2026-08-18` through `2026-09-07`.

The boundary is the date production's exit changed, which makes the split a real
regime boundary rather than an arbitrary cut. Note what the discovery window is and
is not: every policy here is simulated over recorded price paths, so the pre-change
window is a valid comparison of policies. It is not evidence about what production
did, because production did not run this policy then.

If the discovery window shows no separation, the held-out window is not read.

## The variant being added

One new policy, `production_no_progress_v2`: close at 60 minutes when trailing has
not activated by then, with no extension and no minimum-progress step. It mirrors
`evaluate_exit`'s pre-activation branch in the shared policy. Adding it requires
relaxing two `ExitPolicy` invariants that assume every no-progress policy is an
extension policy; that relaxation is code, and is reviewable independently of any
result here.

`BASELINE_EXIT_POLICY` keeps its key, version and semantics. Reports already
registered and read under `production_max_hold_v1` stay interpretable; this pass
adds a policy beside it rather than redefining it underneath them.

## Primary metric, declared before reading

**Median net return per completed virtual trade, per policy**, over the discovery
window, using the shared cost model in `schurfer_performance` as the family already
does. The comparison of interest is `production_no_progress_v2` against
`baseline`.

## Secondary, context only

Share of trades closed by each exit reason per policy, and median holding time.
These describe _how_ a policy got its number. They do not decide the verdict and
may not be promoted to the primary metric afterwards.

## Decision rule, declared before reading

- **Production's exit is not the problem** if `production_no_progress_v2`'s median
  net return is within ±0.25 percentage points of `baseline`. The no-progress exit
  is then close to free, the mislabelled reference changed nothing material, and
  the existing family comparisons stand as read.
- **Production's exit is costing money** if `production_no_progress_v2` is more
  than 0.25 points _below_ `baseline` on at least 200 completed trades. That does
  not authorize changing the exit: it earns a read of the held-out window under
  these same rules.
- **Production's exit is earning money** on the mirror-image result, with the same
  evidence floor.
- **Inconclusive** otherwise, including any separation on fewer than 200 completed
  trades.

## What this pass may not do

It may not tune `no_progress_min`, sweep the 60-minute mark, propose a new policy
after seeing which one wins, or re-run with a different cost assumption. Any of
those is a new hypothesis with a new id and an untouched window.

It also may not be read as a statement about live results. These are simulated
paths with no order book behind them, and no live position is being reconciled
against them here. Whether this replay reproduces what production actually did to
real positions since 2026-08-18 is a separate question and a separate hypothesis.

---

# Amendment, 2026-09-07, before any result was read

Two things in the registration above turned out to be unrunnable as written. Both
are corrected here, in a commit that precedes the run, rather than being quietly
reconciled afterwards.

**The discovery window.** The registration declared `2026-07-17` through
`2026-08-18`. The formal report refuses any cohort start other than
`EXIT_POLICY_COHORT_START = 2026-07-29` and raises rather than running. That date
is the family's own registered cohort start, and it predates this hypothesis, so
it supersedes the one invented here. The window is the family's registered window.

**The held-out split.** The `2026-08-18` holdout is dropped, not deferred. With a
cohort starting `2026-07-29` there are three weeks before that boundary, and
truncating the window there would cut the family's paired sample below its own
readiness gates, so the split would buy nothing and cost the whole read. What the
holdout was protecting against is already handled here by construction: this pass
adds exactly one policy, sweeps nothing, and the family applies a Holm correction
across the challengers. There is no parameter to overfit.

**The verdict is subordinate to the family's readiness ladder.** The family
reports `collecting`, `directional_only`, `insufficient_diversity`, or
`formal_sample_ready`. The decision rule registered above applies **only** at
`formal_sample_ready`. At `directional_only` the comparison may be described and
the verdict is `insufficient_data`. At `collecting` or `insufficient_diversity`
no per-policy number is read as evidence at all. This is not a weakening added
after seeing a disappointing sample: it is the gate the family already enforces,
written down here so the outcome cannot be reinterpreted later.

The evidence floor of 200 completed trades and the ±0.25 point decision rule are
unchanged.

---

# Result, 2026-09-07

Run on production at `4a1ef1f`, working tree clean.
Scope `2026-07-29` through `2026-09-07T18:28Z`.
Decision fingerprint `47933cf06de82ce0`, market-path fingerprint `e2d9fffe492af949`.

## Verdict: `insufficient_data`

The family reports readiness **`insufficient_resolution`** and withholds its formal
intervals: the locked first 100 eligible episodes contain only 69 that are
completely paired, against a tolerance of 1. By the amendment above the decision
rule applies only at `formal_sample_ready`, so **no formal claim is made here**,
in either direction.

Note that `insufficient_resolution` is a readiness state the amendment did not
enumerate. That does not create room for interpretation: the rule was that the
verdict is subordinate to `formal_sample_ready`, and this is not it.

## Coverage

| Metric                           |                      Value |
| -------------------------------- | -------------------------: |
| Dataset episodes                 |                      1,929 |
| Eligible                         |                      1,083 |
| Excluded                         |                        846 |
| Resolved per policy              |                        837 |
| Locked formal sample             | 100 episodes / 70 clusters |
| Completely paired in that sample |                         69 |

Exclusions are dominated by missing price paths, not by policy: 319
`market_path_unavailable`, 303 `missing_outcome`, 236
`complete_fallback_unsupported`. Nearly half the dataset never reaches the metric,
and that is a data-capture limit, not a property of any exit rule.

## Descriptive result, not a verdict

All 837 resolved episodes, paired per episode across policies.

| Policy                                       |   Mean net | Profit factor | Win rate | Median-ish duration | Closed by initial SL |
| -------------------------------------------- | ---------: | ------------: | -------: | ------------------: | -------------------: |
| recent_progress_extension                    |     -0.51% |          0.85 |   47.43% |              117.6m |               31.90% |
| baseline (`production_max_hold_v1`)          |     -0.54% |          0.85 |   47.43% |              114.7m |               31.90% |
| breakeven_after_activation                   |     -0.57% |          0.83 |   49.34% |              111.1m |               31.90% |
| **production (`production_no_progress_v2`)** | **-0.76%** |      **0.74** |   45.28% |           **62.9m** |               22.22% |
| no_progress_60m                              |     -0.76% |          0.76 |   42.41% |               80.4m |               24.97% |
| breakeven_no_progress_60m                    |     -0.77% |          0.75 |   44.44% |               77.1m |               24.97% |

Paired against the baseline, production's real policy is **-0.23 percentage points**
of mean net return, changing the exit on 423 of 837 episodes and cutting mean
holding time by 51.8 minutes. Per episode it is close to a coin flip: 210 improved,
213 worsened, 414 unchanged.

## What is worth saying without a verdict

**The finding that does not depend on the readiness gate is that every policy in
the family is unprofitable on this cohort.** Profit factor runs 0.74 to 0.85 and
mean net return -0.51% to -0.77% across all six. The exit rule is not the thing
standing between this strategy and money, and choosing among these six is
rearranging what a losing cohort loses.

The mechanism behind production's direction is visible in the exit-reason counts
and is worth recording because it is structural rather than statistical. The
60-minute cut fires on 423 episodes and replaces 81 stop-outs (`initial_sl` falls
from 267 to 186) with an earlier exit at market. It therefore truncates losses
whose downside was already bounded by the initial stop, while truncating gains
whose upside was not. Episode 11115 (FLORK) is the shape: the baseline trails to
+7.92%, production closes at minute 60 for -7.13%, because at that moment the
position was 3.90% in profit and the sub-50% band activates trailing only at 8%.

That is a hypothesis about the mechanism, generated after seeing the result. It is
recorded here as such and is **not** evidence for changing the 60-minute mark or
the activation threshold. Doing so would need its own id and an untouched window.

## What this does not say

- It does not say production's exit costs money. The `-0.23` point gap is under
  the registered `0.25` decision margin and, more decisively, the family withheld
  formal inference.
- It does not say the earlier family comparisons were wrong in their conclusions.
  It does say they were measured against a reference that has not described
  production since 2026-08-18, and that the reference flatters production by
  roughly this gap.
- It is not evidence about live results. No live position was reconciled against
  this replay.

## What this changes

Nothing in production. The immediate value is that the family now contains the
policy production actually runs, so the next read is against the real thing.

The binding constraint the run exposed is resolution, not policy: 31 of the 100
locked formal episodes are unpaired, and 846 of 1,929 episodes never enter the
metric at all, mostly for want of a price path. Until that improves, this family
cannot produce a formal verdict about any policy, including the one in production.
