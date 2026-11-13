# B1 prior-only replacement: review response, 2026-11-13

Status: review changes implemented locally on `fix/b1-four-hour-prior-scope`,
based on `8d4ea95`. No supplemental market fetch or strategy-return read has run
within this protocol. The exploratory pre-settlement reads and post-hoc 4h choice
in #582 remain part of the selection history.

## P1-1: explicit four-hour report scope

The S1 pool is the 17,892 prior-only 4h candidates unioned with the original fixed
2,000 validation intervals: 19,845 after 47 overlaps. `report_scope` builds S3
from C-qualified 4h candidates only. It does not use the sample as an event source.
The 75 non-4h sample intervals that pass C remain S1-only. The 47 overlap intervals
qualify independently by C and are not randomly removed because they were sampled.

The root `s3[d]` is `not_fetched`; only its `by_interval["4h"]` holds actual S3
counts and a floor. Outside classes and pooled S3 are never partly evaluated and
never accumulate a misleading `premium_gap` denominator. Their verdicts are
`not_fetched`. Root `verdict` and `passing_decision_offsets` are explicitly 4h.

Incomplete active decision-prefix data retains the fixed denominator and cannot
pass just because the survivors meet the event floor. The original 95% data
coverage threshold applies; the reason is `event_data_incomplete`.

## P1-2: post-read gate removal is explicit

Amendment 3 of the [protocol](bybit-presettlement-feasibility-v1.md) states that the
registered 0.1% minute/hour S1 guard is removed **after seeing it fail** (about
59.6%, 59.5358% in the independently retained local result). It existed solely to
protect hourly-low selection, which the new candidate enumeration does not use.
`hourly_range_exceeded` remains a diagnostic. It cannot produce an inherited
`superset_rebuild_needed` in this report version. Other formula, coverage, sample,
minimum-count and floor thresholds remain unchanged.

The old results and input hashes are preserved; new output identifies itself as
`bybit_presettlement_feasibility_prior_only_4h_v2`.

## P2-3: exhaustive containment through the real classifier

The [retained result](evidence/b1-review-2026-11-13/classify-containment.json) was
computed on clean code revision `a3971eedf2e44d14d18a1cb74de401f77021b862`:

| Check                             |                   Result |
| --------------------------------- | -----------------------: |
| All forecast intervals            |                  855,829 |
| Real classify calls, three d each |            **2,567,487** |
| All-class superset                |                   49,286 |
| Admitted at d30 / d15 / d10       | 49,286 / 49,286 / 49,286 |
| Admitted outside the superset     |                    **0** |
| 4h candidates / S1 pool           |          17,892 / 19,845 |
| Runtime, three local workers      |           718.12 seconds |

Every queried premium minute is the synthetic constant -0.0045. The real
`classify`, `decision_rate`, rate formula and point-in-time filters run without
monkeypatching. No real premium or strategy outcome enters this proof. Unlike the
6,000 earlier spot comparisons, this covers every forecast and offset. The proof
checks all classes, while the economic report scope remains 4h-only.

The reusable `bybit_presettlement_scope_audit` command refuses a dirty checkout,
verifies the discovery-index digest and stores revision and classifier hash. Its
unit test deliberately drops one eligible interval from the superset and checks
that the audit reports all three missing admissions. All 49,286 candidates admit
at all offsets on this index; there is one superset, not three independent ones.

## P2-4: one implementation and conflict-safe merge

The original `premium_free_ok` and `event_superset` from `8d4ea95` are reused.
No unrelated #587 implementation is merged. `research/b1-verified-report` is now
an evidence archive; the canonical active revision is on
`fix/b1-four-hour-prior-scope`. The colleague's uncommitted changes in
`b1-superset` were not modified or deleted.

`merge_minutes` refuses conflicting closes in either input order. Loaded native
snapshots retain source paths; overlapping keys are checked against the preserved
native OHLC payloads too. A changed high with an identical close fails, including
conflicting duplicate rows within one snapshot. Equal rows fold, source paths
remain attached and input objects are not mutated. The loader retains OHLC only
for overlap checking, avoiding a full extra OHLC dictionary for every minute.

## Verification and remaining boundary

- 79 targeted B1 tests pass; Ruff and strict targeted mypy pass.
- Against exact `8d4ea95`, the new review test file has 11 failures and 3 passes.
  This includes off-scope sample pollution, last-file-wins conflicts and disabled
  unbudgeted fetch-path regressions. The test bootstrap preloads and checks the
  baseline module path so pytest cannot accidentally import the fixed source.
- Full classifier containment is pass, with source/data SHA-256 retained above.
- Full `make verify` passed: all-files lint, lock check, Python types/tests on an
  isolated PostgreSQL, Go test/vet, web tests/build and Compose validation. The first
  attempt found missing local dependencies in the new worktree; after
  `uv sync --frozen --all-packages --extra dev`, the unchanged source passed the
  complete command. No production database was used.

The old unbudgeted minute-fetch CLI is disabled **before loading inputs**. The
subsequent [bounded transport implementation](b1-bounded-fetch-runbook-v1.md) closes
the remaining tooling requirement, with 22 additional transport tests and a new
successful full `make verify`. Its actual pilot has not run. The fixed pilot and
request/byte budgets are unchanged; transport review and a new owner fetch go
remain required. No approval or download is implied by these review fixes.
