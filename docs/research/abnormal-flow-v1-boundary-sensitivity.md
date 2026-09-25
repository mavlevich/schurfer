# Abnormal-flow v1: entry/exit-bars path sensitivity (post-hoc, registered before the read)

Status: registered 2026-09-25, before the new read. Classification:
`post_hoc_burned_window_diagnostic_not_formal_evidence`. This does not change the frozen v1
contract or its burned report.

## Question

The v1 outcome reader resolves an episode only when every one of the 721 one-minute bars of
its priced-proxy path is present and `price_complete`. The price itself comes from two bars
only: the entry bar's open and the horizon bar's close. How does the burned-window portfolio
diagnostic change if only those two bars must be present and complete?

## Why this rule, and what was known before this read

The rule was chosen from the outcome-blind unresolved-reason taxonomy (#443, bundle
`abnormal-flow-v1-unresolved-taxonomy-20260925-r2`, report
`sha256:cd4982011efd23c363b54a4d823d799e4a17bac04b5518876207c5bda5c41728`), which reads bar
presence and `price_complete` only:

- primary: 127 unresolved = 102 `price_incomplete_inside` + 25 `internal_gap`; all 127 have
  complete entry and exit bars (`entry_exit_bars_complete`);
- controls: 591 of 592 unresolved have complete entry and exit bars.

So the expected primary coverage under this rule is 252/252, provided both prices are
non-empty. Returns of this burned window were already read in #439, so the choice is made
before THIS read, not before any read.

## Fixed before the read

- Same snapshot (`a4d68c98...`), same 252 primary episodes, same frozen contract, same
  horizon (720m), same entry/exit reference, same conservative costs and funding model.
- Same portfolio policy as the baseline bundle `abnormal-flow-v1-portfolio-scenarios-20260925`
  (report `sha256:2a7e585a2b9185d993f7615ad35cac027c9557e1e83b8cb598f0be04d6658f7c`, run
  `82954a5`, clean tree): $300 bank, fixed sizing, one position per asset, all K = 1..20, all
  declared unresolved scenarios (hold_to_end, planned-exit worst/zero/mean), plus the
  take-every-signal policy.
- Published: every K and scenario, and per K and scenario the change in coverage, trades
  and net PnL against the baseline. The comparison re-simulates this run with the
  baseline's numeric unresolved assumptions (worst/zero/mean resolved gross), so the
  delta isolates the path rule; each scenario's own recomputed assumption is shown
  separately. It refuses a baseline that is not the registered report above, differs in
  scenarios, K values, policy, snapshot or contract, or whose resolved positions changed.

## What the result may support

- Negative total net PnL for every K and scenario may lower the priority of an
  abnormal-flow v2.
- Positive for every K and scenario is grounds to design a prospective v2, not a
  confirmation of its economics.
- A sign that depends on K, scenario or the remaining unresolved rows leaves the question
  open.
- No K is selected from this result.

## What it cannot show

The two-bar rule recovers a final priced-proxy return for a fixed-horizon exit. It does not
recover the path inside the position, so it says nothing about drawdown during the hold,
stop/take-profit/trailing behaviour, or whether the position was executable during the
incomplete minutes.
