# HYP-036 bounded power audit: conditional discovery budget

Status: completed 2026-11-14, planning only. The original unadjusted MDE
reproduces; the pilot does not establish exam power or a profitable strategy.
HYP-036 v1 remains stopped. No strategy outcome, exam or new archive was read.

## Chronology and evidence

The owner accepted the limited audit after the inventory review with
"го го го". [Contract](hyp036-power-audit-v1.md), code and the registry row
were committed as `de499aa1f1f23c48939c28237276ceddbff611f2` at
2026-11-14 12:29:38 UTC. The audit ran from 12:30:02 to 12:30:22 UTC from
that clean revision, after checking the pinned input hashes. This registration
was local, not published in main before the calculation. Colleague review
precedes a PR; no new track or strategy registration is claimed.

The exact [output](evidence/hyp036-power-audit-v1/audit.json.gz), original
[counts](evidence/hyp036-power-audit-v1/counts.json.gz) and frozen
[event identities](evidence/hyp036-power-audit-v1/pairs.json.gz) are retained
as deterministic gzip files (mtime 0). Their compressed and uncompressed
hashes are in the [manifest](evidence/hyp036-power-audit-v1/manifest.json).
Original output SHA-256:
`5bf24449cb3c0455fae7dd329ea60a36c24cf8b710b66ed7e42ea2feef631f0f`.
Contract SHA-256:
`c18362b3700a25fd79e74e7b244ed0259a234902e22e40e5b244cb5c17711e49`.

## What was observed and what is projected

Observed: **784 events, 750 symbol-day clusters, 194 symbols, 28 decision
days and four ISO weeks**. January contributes 408 events and July 376.
The four weeks contain 230, 178, 171 and 205 events. At most seven events
occur at one decision hour. Counts do not establish independence across hours
or across weeks. The previously reported 9,312 events are a full-period
projection, not the observed sample.

Both columns below use the original model: inflation 1.5, design effect
1.2, planning constant 1.645 + 0.842. The last column projects variance
by observed decision days / 334 and assumes dependence per day transfers.
These are log-price dispersion proxies, not measured net returns.

| Price-dispersion proxy           | MDE at observed sample size, bps | Conditional full-discovery MDE, bps |
| -------------------------------- | -------------------------------: | ----------------------------------: |
| Raw short, combined              |                            68.65 |                               19.88 |
| Raw short, January only          |                           108.08 |                               22.13 |
| Raw short, July only             |                            81.52 |                               16.69 |
| BTC-adjusted short, combined     |                            50.48 |                               14.61 |
| BTC-adjusted short, January only |                            75.33 |                               15.42 |
| BTC-adjusted short, July only    |                            66.09 |                               13.53 |

The raw combined MDE is 19.877307091747415 bps and reproduces the old
unrounded `a_net` calculation to numerical tolerance. No scales are missing
or substituted. That old leg was already unadjusted: the inventory's earlier
claim that it subtracted BTC was wrong. BTC-relative event weights are new
diagnostics, and are distinct from the original matched `b_excess` leg.

For the short, the benchmark adjustment means short asset minus short BTC,
or minus the asset's log-price change plus BTC's log-price change. It does
not model orders in BTC. Neither leg here includes funding variance or
uncertain native execution costs. Removing a market factor statistically
does not demonstrate that an executable hedged strategy earns money.

## Dependence sensitivity fixed before the run

| Extra variance multiplier, on top of the old model | Raw MDE projection, bps | BTC-adjusted projection, bps |
| -------------------------------------------------- | ----------------------: | ---------------------------: |
| 1.0                                                |                   19.88 |                        14.61 |
| 1.5                                                |                   24.34 |                        17.90 |
| 2.0                                                |                   28.11 |                        20.67 |
| 4.0                                                |                   39.75 |                        29.23 |

The combined raw model reaches the 30 bps planning comparison after a
2.278-fold additional variance increase. January alone reaches it at
1.838-fold; its twofold scenario is 31.29 bps. These multipliers are stress
scenarios, not observed weekly correlations or newly estimated design effects.
Original rho-factor sensitivities 0.5/1/1.5 give raw projections
16.04/19.88/23.09 bps and BTC-adjusted 12.26/14.61/16.64 bps.

Four ISO weeks cannot calibrate dependence across a year. Accordingly,
`conditional_below_30_bps: true` is a statement within the old model, not
a confidence-coverage or promotion claim. The fixed output status is
`planning_only_requires_design_decision`. Exam power remains null and no
exam is assigned. No whole-window square-root extrapolation is called an
empirical holdout calculation.

## Practical decision

The corrected audit leaves a reason to discuss a bounded economic screen;
it does not justify spending 520 GB on the projected count alone. My proposed
next task is a **separate fixed design for one economic read on the existing
784-event pilot**, with the old trigger and four-hour horizon unchanged,
actual funding, explicit execution assumptions and all unresolved events.
Specify native price availability and the exact request budget before any
price fetch. This is a proposal, not an authorization to read outcomes.

That screen would test whether the economics warrant buying the rest of the
discovery tape. It cannot confirm a strategy: it has only four weeks and the
selection history includes the known abnormal-flow long loss. Before its
read, specify the spending decision for unfavorable, inconclusive and
favorable results, without pretending the 26-week confirmation floor passed.
No mirror, alternate horizon, filter or favorable subgroup is added afterward.
Any later full discovery and independent exam require their own recorded
decisions. The backward holdout remains unassigned and unread here.

## Verification

Executed: 60 focused audit, original HYP-036 and matching-check tests; Ruff
check/format; strict mypy on the new module; commit hooks. Tests exercise
the original power path on a synthetic 784-event panel, incorrect baseline
rejection, missing scales, duplicates, timestamp/scope boundaries, projection
arithmetic, and invariance to bars at/after each tested decision. No test uses
the market sample to choose a parameter. Full `make verify` and CI have not
run; they remain before any later push/PR. Production was not accessed.

Exact focused checks (from the branch worktree, using the existing root venv):

```sh
PYTHONPATH=apps/analytics /Users/mavlevich/Desktop/Projects/schurfer/.venv/bin/python -m pytest apps/analytics/tests/test_bybit_absorbed_buying_power_audit.py apps/analytics/tests/test_bybit_absorbed_buying.py apps/analytics/tests/test_bybit_absorbed_buying_match_check.py -q -rA
/Users/mavlevich/Desktop/Projects/schurfer/.venv/bin/ruff check apps/analytics/schurfer_analytics/bybit_absorbed_buying_power_audit.py apps/analytics/tests/test_bybit_absorbed_buying_power_audit.py
MYPYPATH=apps/analytics:packages/journal:packages/performance /Users/mavlevich/Desktop/Projects/schurfer/.venv/bin/mypy --follow-imports=silent apps/analytics/schurfer_analytics/bybit_absorbed_buying_power_audit.py
```

Audit invocation at the clean registered revision (a reproduction must use a
new output path, because publication cannot overwrite an earlier result):

```sh
PYTHONPATH=apps/analytics /Users/mavlevich/Desktop/Projects/schurfer/.venv/bin/python -m schurfer_analytics.bybit_absorbed_buying_power_audit \
  --features /Users/mavlevich/Desktop/Projects/schurfer/.claude/worktrees/hyp032-run/runtime/research/hyp032/features-8522f0c.jsonl.gz \
  --counts /Users/mavlevich/Desktop/Projects/schurfer/.claude/worktrees/power-budget/runtime/research/hyp036/counts-blocks-0564134/counts.json \
  --events /Users/mavlevich/Desktop/Projects/schurfer/.claude/worktrees/power-budget/runtime/research/hyp036/counts-blocks-0564134/pairs.json \
  --output /Users/mavlevich/Desktop/Projects/schurfer/runtime/research/hyp036-power-audit-v1/audit.json
```
