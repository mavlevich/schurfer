# Track B closure, 2026-11-14

**Decision: track B (HYP-031 family, long into settlement) is closed.** The fixed
taker policies failed in [B3a](b3a-discovery-result-2026-11-14.md). The frozen
maker version gets no further budget. The HYP-031 test year stays closed and
unspent. The owner decided on 2026-11-14 after the agent that ran B3a and the
reviewer both recommended closing.

## What closes

- **Taker v1, both exits: closed on a negative result.** Net at 30 bps is
  -26.03 bps (exit at t - 1 min) and -20.43 bps (exit at t + 1 min). Both
  weekly upper bounds are below zero. Count, coverage and diversity floors pass,
  so this is a failure, not insufficient data.
- **Maker version: not funded.** B3a's spending rule returned
  `maker_source_review_needed`. That verdict permits a review; it does not
  require the maker stage. This note is that review, and it ends in no budget.

## Maker review: why the frozen maker stage is not worth running

The frozen maker model enters with a resting limit order and exits at the same
fixed times as the taker policies, with a taker exit. Against B3a's central
estimates of price plus funding before costs:

| Item                                          | Before (t - 1 min) | Through (t + 1 min) |
| --------------------------------------------- | -----------------: | ------------------: |
| B3a price plus funding before costs, bps      |              +3.97 |               +9.57 |
| Maker entry fee 2 bps plus taker exit 5.5 bps |               -7.5 |                -7.5 |
| Best case before adverse selection, bps       |               -3.5 |                +2.1 |

The best case assumes that the B3a bar open is close to the mid price, so the
half spread earned on the maker entry is paid back on the taker exit. It also
assumes every signal fills and the exit meets no slippage beyond the top of the
book. None of these is shown; each one makes the result worse, not better.

- **Before settlement:** negative even under the best case. Dead in any fill model.
- **Through settlement:** at most about +2 bps before adverse selection. Fills
  of resting orders concentrate on the worse signals: the 2026-11-11 pump lab
  measured unfilled signals at +82.1 to +239.9 bps against filled ones at
  -151.0 to -8.4. The B2 pilot left real fill identification unresolved.
- **Power:** B3a's diagnostic MDE against zero is 23.5 / 22.7 bps at this
  sample size. A test-year sample of similar size would have a similar MDE, so
  an effect of a few bps could not be confirmed there even if it existed. The program's
  planning note already put a net below 50 bps out of reach of a clean test.
- B3a's upper diagnostics (21.68 / 26.08 bps) are 97.5th percentiles with the
  favorable funding valuation. They are tails, not central estimates, and they
  are why the rule did not pause the track automatically.

Running B3b would mean resolving trade classes, historical lot rules and exit
tape coverage, plus a large archive download, to estimate an effect that is
at most a few bps and cannot be confirmed. The budget goes elsewhere.

## What this does not show

- It does not prove that every maker policy on these signals loses. Other
  entries, exits or execution would be new designs on an exposed year.
- The window from t - 60 min to t - 30 min was not tested: B1 fixed d at 30, 15
  and 10 minutes. The earlier exploratory 4h figure (+55.3 bps gross from
  t - 1 h) collapsed to -2.0 bps net without its top five symbols. Testing that
  window now would be another exploratory variant on the discovery year, not a
  continuation of this track.
- The gap between the old +35.5 bps (t - 30 min to t, events selected by the
  rate settled at t) and B3a's +3.97 bps (t - 29 min to t - 1 min, decision-time
  signal) is consistent with selection by the settled rate. The windows differ
  by a minute at each end, so this is not a clean decomposition.

## What stays

- **Windows:** the HYP-031 test year (2025-10-01 to 2026-09-28) is unread and
  unassigned again. The backward holdout stays unassigned.
- **Data**, all outside Git and kept:
  - B1 snapshots under `runtime/research/b1/`, including the all-class
    supplement `minutes-extra-0abd726.jsonl.gz` (35,217 intervals, SHA-256
    `6fbc5a15...`) from branch `fix/b1-premium-free-superset`, kept without a
    report;
  - B2 pilot bodies under `runtime/research/b2/native-source-pilot-527b7446/`;
  - B3a receipts and the sealed snapshot under `runtime/research/b3a-v1/`.
- **Frozen records:** the maker model, events and exits stay as frozen in the
  [B3a contract](b3-economic-screen-proposal-v1.md), so a later return cannot
  quietly change them.
- **Selection history:** at least 124 exploratory configurations, the 4h choice
  after seeing returns, eight B2 configurations and the two B3a exits. Any
  future use of these signals counts all of them.

**Reopen only on new, independent evidence of an execution edge**, for example
measured maker fills from live trading on comparable instruments. A repeat of
this design on the same year does not count.

## Program consequence

With track A closed earlier (HYP-037 power stop, #584) and track B closed here,
the program has no active candidate. What remains scheduled:

- HYP-012 v2 checkpoint at 2026-11-30 00:00 UTC (the registered command on the
  owner's go);
- HYP-038 radar intake, only after v2 is terminal.

Under the [program plan](program-plan-2026-11.md) stop rule, without a candidate
past an untouched window by 2027-02-11 the program goes to maintenance until
the March read. Whether to start a new idea before then is the owner's decision.
