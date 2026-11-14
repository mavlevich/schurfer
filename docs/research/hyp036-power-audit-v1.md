# HYP-036 pilot power audit, fixed 2026-11-14

Status: bounded planning calculation, not a strategy registration. The owner
accepted the review's limited next step with "го го го" on 2026-11-14. This
authorizes this offline audit of existing discovery inputs only. HYP-036 v1
remains stopped. No strategy outcome, exam, new download or production action
is authorized. Local contract and code are committed before this calculation;
colleague review still precedes any PR. This does not claim publication in main
before the audit.

## Fixed inputs and scope

- Features `features-8522f0c.jsonl.gz`, SHA-256
  `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`.
- Pilot `counts.json`, SHA-256
  `db24fde0daa8c148cf64a2e7597bca5e7cce5c414e58e4a6b9632c83734ba86b`.
- Frozen `pairs.json` event identities, SHA-256
  `338b6e3811bbeb95312e4bb17dd0ff5b1f6b8acfbd3dff8cd7a090fe07a5cba4`.
- Decisions 2025-01-13 through 2025-01-26 and 2025-07-07 through
  2025-07-20, inclusive: 784 events, 750 symbol-days, 28 decision days,
  four ISO weeks. Controls in the frozen file do not select or weight events.
- No tape part is read. No trigger, holding period, event or control changes.
  No holdout or HYP-031 test-year input is accepted. The verified features
  container covers discovery only; it is loaded using the existing parser.

## Fixed calculations

Reuse HYP-036's `weights_net`, `scale`, `month_rho`, `block_variance` and `mde`.
The event mean gives each symbol-day equal weight, then each event within it.
Each four-hour log-price scale uses 336 hourly-ended historical returns
ending at or before that event's t. Pool and correlation use the 30 days
before the relevant month. Those historical values may be after an earlier
event; no return over an event's own holding period is computed or printed.
Only scales, correlations and planning statistics are disclosed.

1. Reproduce original unadjusted `a_net` MDE to numerical tolerance. Failure
   stops the audit; it is not permission to change the event list or model.
2. Compute a separate BTC-relative event-price proxy using the same events
   and weights. This is not old matched `b_excess`. For a short benchmark,
   adjusted price return is short asset plus the underlying BTC return;
   variance equals that of asset log change minus BTC log change. This
   diagnostic is not the net of an executed hedge.
3. Report both the actual pilot-size MDE and its conditional discovery
   projection, variance multiplied by 28/334. Repeat separately for each
   fixed 14-day block, projecting by 14/334. The annual event count is
   a projection, not an observed sample size.
4. Keep original inflation 1.5, design effect 1.2 and one-sided planning
   constant 1.645 + 0.842. Report original rho sensitivities 0.5, 1, 1.5.
   Add fixed extra variance multipliers 1, 1.5, 2, 4; these multiply MDE by
   their square roots on top of the original model. They are scenarios,
   not estimates of weekly dependence. Report the multiplier that reaches
   30 bps. No proxy or multiplier is selected after seeing results.
5. Report counts by ISO week and same-hour concentration. Do not attempt
   a reliable weekly design-effect estimate from only four observed weeks.

No price, individual return, mean return, funding outcome, PnL or trade
verdict is output. The historical scale is a price-dispersion proxy; variable
funding and native execution uncertainty are not included in its variance.
The BTC-adjusted event proxy is an additional descriptive exposure; the
registry records it before this run. No synthetic funding zeros are used.

## Decision and reproducibility

The 30 bps comparison applies only to the conditional discovery planning
estimate, with the old one-sided alpha. It is not an exam power gate, a
positive lower bound, or a newly chosen confirmation alpha. Extra-variance
scenarios and separate-block projections diagnose fragility; they do not
automatically pass or kill a strategy.

Regardless of the numbers the only allowed final status is
`planning_only_requires_design_decision`. Input mismatch, changed sample,
missing scale or failure to reproduce the original result is an audit error.
No trial rerun with changed parameters is allowed. An unfavorable estimate
is reported without loosening the calculation. A favorable estimate permits
discussion of a separately registered economic pilot, not a 520 GB fetch.

The module refuses a dirty source tree and wrong input hashes, records
revision, porcelain, source and contract hashes, and publishes output once.
The result and source chronology are retained for local colleague review.
The old HYP-036 reading text's BTC adjustment and its unadjusted `a_net`
planning proxy disagree; this audit records the difference and does not
rewrite the stopped study. A successor must specify its estimand afresh.
