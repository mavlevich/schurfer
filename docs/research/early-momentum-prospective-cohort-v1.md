# early_momentum prospective cohort

**PR:** `feat/early-momentum-prospective-cohort-v1`
**Code:** `apps/execution/schurfer_execution/early_momentum.py` (contract guard),
`apps/analytics/schurfer_analytics/early_momentum_prospective_cohort_report.py`
**CLI:** `make early-momentum-prospective-cohort-report ARGS="--cohort-end <ISO8601> --format markdown|json"`

The most direct step toward real money in the current plan. Read-only,
retrospective-on-a-rolling-basis status for a genuinely fresh
`early_momentum_v4` cohort, isolated from the historical v1-v4 data the
existing net-evidence report already covers. **Never places an order.**
`eligible_for_live_probe_review` is a signal for a human to START the
separate live-probe safety PR (budgets, circuit breaker, kill switch,
reconciliation) -- not an automatic authorization for a single live order.

## What this PR reuses, and does not rebuild

`apps/analytics/schurfer_analytics/early_momentum_net_evidence*.py`
(`analysis/early-momentum-net-evidence-v1`) already implements, and already
tests, the entire economics engine this status needs: the 16-step
resolved/unresolved funnel with cohort- and row-level integrity violations
(including a mismatched/unexpected `contract_sha256` already excluded
fail-closed), accounting-complete counts, executable net EV/profit
factor/median/drawdown/losing-streak, distinct assets/weeks, concurrency
and capital occupancy, and bootstrap/leave-one-out robustness. This PR
does not reimplement any of it -- see
`early_momentum_prospective_cohort_report.py`'s own module docstring for
why duplicating it would risk the two silently drifting apart on what
"accounting complete" or "profit factor" means.

Two things are genuinely new:

1. A separate, later cohort boundary, so historical episodes are excluded
   by the query itself, not filtered out afterward.
2. A narrower promotion-state vocabulary purpose-built for a live-probe
   go/no-go read, mapped from the existing, already-tested 4-state
   `Verdict`.

## Contract immutability

`early_momentum.py` already computed `CONTRACT_SHA256` (scanner quality
policy + signal thresholds + exit params + size + leverage) and wrote it to
every armed episode. What it did not have: anything stopping a silent edit
to any of those inputs from quietly minting a new hash that the next armed
episode starts using, under the same nominal `_STRATEGY_VERSION`, with no
human ever deciding "this is now a different contract."

Fixed by pinning a checked literal
(`_EXPECTED_CONTRACT_SHA256_HEX = "bdda6c6423b0cc69d8b6266269cda07c31e20f4d256b1793229ab47beb5cb1ac"`,
the same value `early-momentum-net-evidence-v1.md`'s own frozen contract
table already recorded), verified at import time via
`_verify_contract_hash_pinned`, raising `RuntimeError` on any mismatch --
the exact pattern `momentum_flow_paper_contract.py` already established for
`momentum_flow_paper_v1` and its siblings. A deliberate contract change
bumps `_STRATEGY_VERSION` and updates this literal in the same commit, with
the new hash visible in the diff.

`schurfer-analytics` does not depend on `schurfer-execution` (no shared
package), so `early_momentum_net_evidence.py`'s own
`EXPECTED_CONTRACT_SHA256_HEX` is a **duplicated** literal, kept in sync by
convention (the same "must track apps/execution/..." pattern
`liquidation_cascade_repository.py` already uses) -- guarded by
`test_expected_contract_sha256_matches_the_pinned_execution_side_literal`
as the tripwire against the two drifting apart.

Immutability of an already-armed episode's own `contract_sha256` is
structural, not merely conventional: the column is written exactly once,
at arm time (`INSERT`), and no `UPDATE` statement anywhere in `episodes.py`
or `journal.py` ever touches it -- verified by a static source scan
(`test_no_update_statement_anywhere_ever_touches_contract_sha256`), which
catches any _future_ UPDATE that mentions the column, not just the
specific code paths one behavioral test happens to exercise.

## The prospective cohort boundary

Migration 0036 creates `app.research_cohort_registrations`. On the first
startup of the new execution image, before scanner/trigger tasks are
created, execution inserts `early_momentum_v4_prospective_v1` using
`clock_timestamp()` from PostgreSQL. First writer wins; every restart must
match the registered strategy, contract hash, and frozen runtime-policy
hash or startup fails. The report reads this durable registration and
refuses to run before it exists. There is no manually chosen timestamp and
no post-deploy follow-up commit.

Once registered, the boundary is passed as the shared evidence builder's
`cohort_start` parameter (added to that function as an optional,
backward-compatible keyword defaulting to `FORMAL_COHORT_START` -- every
existing caller is unaffected) instead of the formal v4 cohort start.
Membership is `episode.armed_at`, exactly like the formal report --
verified end to end against real Postgres
(`test_historical_episode_before_the_prospective_boundary_is_excluded`).

## Verdict mapping

| Underlying `Verdict`        | Prospective state                |
| --------------------------- | -------------------------------- |
| `invalid_integrity`         | `blocked_integrity`              |
| `insufficient_data`         | `collecting`                     |
| `fail`                      | `fail`                           |
| `pass_live_micro_candidate` | `eligible_for_live_probe_review` |

`invalid_integrity` deliberately maps to `blocked_integrity`, not an
economic `fail` and not ordinary `collecting`. A broken pipeline must be
repaired rather than mistaken for a need to wait for more rows. Verified against real
Postgres with a deliberately wrong `contract_sha256` inside the prospective
window
(`test_contract_hash_mismatch_inside_the_window_maps_to_blocked_not_eligible`).

A pass is never exposed as `eligible_for_live_probe_review` from a dirty
working tree or a mutable DB-only run. The final decision run must use
`--freeze-artifact` or replay the same validated fingerprint with
`--from-artifact`; the artifact freezes episodes, trades, exit-liquidity
observations, legacy context, DB snapshot time, and cohort registration.

## Duplicate/restart safety (already existing, not new)

Episode-arm uniqueness (`ux_early_momentum_episodes_live_instrument`,
partial unique index on `(exchange, native_market_id)` while
`status IN ('armed','claimed')`) and idempotent trade-open
(`entry_idempotency_key` + `ON CONFLICT ... DO NOTHING` in
`journal.open_trade_for_episode`) both already exist and are already
covered by `test_live_instrument_partial_index_rejects_a_second_armed_
episode` (`test_episodes_integration.py`) and
`test_open_trade_conflict_returns_existing_row_when_it_matches`
(`test_journal.py`). This PR does not add new production code for this --
only verified the existing coverage is real before relying on it.

## Explicitly out of scope

No orders, no `execution_intent`/`orders`/broker calls anywhere in this
PR's own code. Budgets, circuit breaker, kill switch, and reconciliation
are their own later PR, started only once a cohort is actually accumulating
toward `eligible_for_live_probe_review`. `liquidation_cascade` observability
is a separate, smaller PR after that -- the strategy is not disabled and
keeps collecting paper data as-is.

## Delivery

```
make early-momentum-prospective-cohort-report ARGS="--cohort-end <UTC> --freeze-artifact"
make early-momentum-prospective-cohort-report ARGS="--cohort-end <UTC> --from-artifact <fingerprint>"
make prod-early-momentum-prospective-cohort-report ARGS="--cohort-end <UTC> --freeze-artifact"
```

Same `--cohort-end`/`--code-revision`/`--working-tree-dirty`/`--format`
CLI shape as the formal net-evidence report. Read-only; the production
analytics service does not need to be restarted to run it, but rebuilding
the `analytics` image (`make prod-deploy`) is required before either the
contract guard or this report exist on the server at all.

**After this PR deploys:** execution registers the boundary automatically
before its workers start. No follow-up timestamp commit is required.
