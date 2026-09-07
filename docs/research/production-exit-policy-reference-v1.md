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
