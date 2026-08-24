# Exit-liquidity adjusted net economics

**Report:** `analysis/exit-liquidity-adjusted-net-economics-v1`
**Code:** `apps/analytics/schurfer_analytics/exit_liquidity_net_economics*.py`
**CLI:** `make exit-liquidity-adjusted-net-economics-report ARGS="--since <ISO8601> --until <ISO8601> --format markdown|json"`

Retrospective diagnostic. Answers one question: after replacing the
decision-time **modeled** exit cost with the **observed** executable
close-time quote, does `pump_short` v1 still have positive net economics?
Read-only against production Postgres; never writes, never executes a
trade, never touches the running strategy. A positive result authorizes a
new, untouched forward cohort under a registered contract -- **never**
live capital directly. A negative result is grounds to stop developing
this strategy version.

This is the second consumer of `research_dataset_artifact.py` (#288) and
the direct follow-up to `exit_liquidity_calibration_report.py`'s own
finding (2026-08-24): the decision-time model underestimates exit cost by
+80.90 bps on the 12/149 trades (8.05%) whose observed close-time spread
was `>= 50 bps`; on the remaining 137 the model is directionally fine, even
slightly conservative. This report turns that bps-level finding into a
dollar-level one.

## Frozen cohort contract

Fixed **before** the first production run, not retuned after seeing
results.

| Field                                                       | Value                                                                                                                                                                                                                                                          |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `report_version`                                            | `exit_liquidity_net_economics_v1`                                                                                                                                                                                                                              |
| `formula_version`                                           | `ask_vwap_primitives_v2`                                                                                                                                                                                                                                       |
| `strategy` (enforced in SQL, `ALLOWED_STRATEGY_IDENTITIES`) | `("pump_short", "1")` **only** -- the distinct registered variant `("pump_short", "1_market_quality")` (see `trader.py`'s own comment on `journal.strategy_identity` parsing) is deliberately excluded; broadening this needs its own explicit contract change |
| mode                                                        | paper only (`setup_context->>'paper' = 'true'`)                                                                                                                                                                                                                |
| side                                                        | short only                                                                                                                                                                                                                                                     |
| cohort start                                                | `EXIT_LIQUIDITY_COHORT_START` = `2026-07-29T15:45:34Z` (same as the calibration report -- exit-liquidity observations do not exist before this)                                                                                                                |
| accounting versions read                                    | `legacy_price_only_v1`, `paper_conservative_costs_v1` -- any other value is `unsupported_accounting_version`                                                                                                                                                   |
| max exit-quote skew                                         | `MAX_EXIT_QUOTE_SKEW_SECONDS` = 120s (same constant as the calibration report)                                                                                                                                                                                 |

## Why this does not reuse `ExitLiquidityRow`

`exit_liquidity_calibration_report.py`'s dataset exists to compare bps,
not dollars -- it deliberately drops fields (leverage, fees, funding,
accounting status) it does not need. Bolting those onto `ExitLiquidityRow`
would grow a bps-calibration row into a dollar-accounting row for a
different consumer, coupling two reports that should be able to change
independently. This report defines its own artifact,
`exit_liquidity_net_economics_v1`, one row per `trade_id`, and freezes it
through the same generic `research_dataset_artifact.py` mechanism.

The artifact keeps **every** row in the coverage cohort, including
unresolved/excluded ones -- not just the paired-comparable subset -- so the
exclusion funnel itself is reproducible from the frozen artifact, not only
computed live against the database.

## The adjusted-PnL formula, and why it is NOT a simple bps delta

`exit_liquidity_calibration_report.py`'s own delta
(`observed_exit_bps - modeled_exit_bps`) cannot be subtracted directly from
`Trade.net_pnl_usd` for every row, because `Trade.exit_price` does not mean
the same thing on every row:

- **Legacy-capture era** (`accounting_version = legacy_price_only_v1`,
  closed before `paper.py`'s exit-time VWAP capture existed):
  `Trade.exit_price` is the naive/trigger price. The exit cost lives
  entirely in `exit_slippage_bps` (equal to the entry-time
  `setup_context.market_quality.ask_impact_bps` snapshot -- see
  `exit_liquidity_calibration_repository.py`'s own docstring).
- **Fresh-capture era** (`accounting_version = paper_conservative_costs_v1`
  and a capture succeeded): `paper.py`'s `_capture_exit_liquidity` sets
  `Trade.exit_price` to the **captured VWAP price itself**
  (`exit_price_for_accounting = exit_vwap if exit_vwap is not None else
current_price`), and `close_trade()` deliberately zeroes
  `exit_slippage_bps` to avoid charging that cost a second time.

Naively recomputing "gross PnL from `Trade.entry_price`/`Trade.exit_price`,
then subtract the observed bps delta" would double-count the exit cost on
every fresh-capture row -- exactly the bug class fixed in #286, and exactly
what this report's own regression tests (below) exist to prevent.

**The formula instead uses only primitives that mean the same thing on
every row, regardless of era:**

```
adjusted_gross_pnl_usd =
    size_usd * (entry_price - observed_ask_vwap) / entry_price      [short]

entry_cost_usd = size_usd * entry_slippage_bps / 10_000

adjusted_net_pnl_usd =
    adjusted_gross_pnl_usd - entry_cost_usd
    - fees_usd - funding_usd
```

- `observed_ask_vwap` is `TradeExitLiquidityObservation.ask_vwap` directly
  -- the actual executable VWAP fill price for the requested notional at
  close time, never `Trade.exit_price` (so the formula never has to know
  or care which accounting era produced the row).
- **`formula_version=ask_vwap_primitives_v1` (2026-08-25) never shipped
  past colleague review.** It computed gross PnL from `observation.mid`
  instead, then charged `observation.ask_impact_bps` separately against
  `filled_notional_usd` as a flat notional-scaled cost -- reasoning that
  `mid + ask_impact_bps ≈ ask_vwap` "by construction" made the two
  equivalent. That reasoning was wrong: `ask_impact_bps` is measured
  relative to `mid` (`ask_vwap = mid * (1 + ask_impact_bps / 10_000)`), not
  relative to `entry_price`, so charging it flat against notional only
  matches the true `ask_vwap`-based cost when `mid == entry_price`. On a
  short where price moved against the position (mid far above entry --
  exactly the trades this report cares about most), v1 systematically
  UNDERSTATED exit cost and OVERSTATED PnL, worse the larger the adverse
  move (reproduced: $0.25 overstatement on a $50 position, 50% adverse
  move, 100bps impact). `ask_vwap_primitives_v2` reads `ask_vwap` directly
  and cannot make this error regardless of how far mid drifts from entry.
- `entry_slippage_bps` is read as-is from the `Trade` row (existing
  decision-time model, computed once at open by `accounting_contract()`
  from `setup_context.market_quality`) -- this report does not attempt an
  "observed" entry-side counterpart; only the exit side has a capture
  mechanism today.
- `fees_usd`/`funding_usd` are read as-is from the `Trade` row. **Caveat,
  shown in every rendered report:** `funding_usd` here is
  `calculate_performance()`'s fixed conservative rate model
  (`funding_cost_bps_per_8h * duration_minutes / 480`, always a cost, never
  signed) -- this codebase does not capture real per-trade signed exchange
  funding anywhere. Calling it "signed funding" would overclaim precision
  this dataset does not have; the field is carried through unchanged and
  documented as a model, not a measurement.

`recorded_net_pnl_usd` (the "before" side of the comparison) is
`Trade.net_pnl_usd` as persisted, used only when `accounting_status ==
'complete'` -- an `incomplete` row has no valid baseline to pair against
(see exclusions below), even though `adjusted_net_pnl_usd` above can still
be computed for it independently.

## Exclusion funnel

Independent boolean flags, plus one `primary_reason` (first flag in this
order that applies). Every field this exclusion funnel depends on is
checked for presence/validity explicitly and fails CLOSED (excluded) when
missing -- an earlier version of this function let `filled_notional_usd
is None` and `observed_at is None` pass through silently (one reached an
`AssertionError` deep in the formula, the other was treated as a valid
fresh quote); colleague review, 2026-08-25:

1. `missing_observation` -- no `TradeExitLiquidityObservation` row
2. `not_sampled` -- `observation.status != 'sampled'`
3. `observation_error` -- `observation.error is not None`
4. `identity_mismatch` -- `observation.exchange != trade.exchange` or
   `observation.symbol != trade.symbol`
5. `malformed_identity_post_263` -- **row-level integrity failure, not a
   normal exclusion**: `identity_mismatch` on a trade whose `entry_at` is
   at or after `29ccd71` (#263, `2026-08-20T17:31Z`, "enforce canonical
   instrument identity"). The 23-row 2026-08-20 audit (pre-#263, `15:49:52`
   -- `17:08:44Z`, all 63 that day) is the last time this pattern is
   expected; any occurrence on or after the fix is a regression and must
   surface loudly, not blend into the same bucket as the known legacy gap.
   (Takes priority over the plain `identity_mismatch` label above when
   both would apply.)
6. `missing_observed_at` -- `observation.observed_at is None` (cannot judge
   staleness at all, so this must never be silently treated as fresh)
7. `stale_quote` -- `abs(observation.observed_at - trade.exit_at) >
MAX_EXIT_QUOTE_SKEW_SECONDS`
8. `missing_or_invalid_notional_fields` -- `requested_notional_usd`
   missing/non-positive, or `filled_notional_usd` missing/negative
9. `requested_notional_mismatch` -- `abs(requested_notional_usd -
size_usd) > 0.01`
10. `insufficient_visible_depth` -- `filled_notional_usd < requested_notional_usd - 0.01`
11. `missing_or_invalid_quote_fields` -- `mid`/`ask_impact_bps`/`ask_vwap`
    missing or non-finite, or `Trade.entry_slippage_bps` missing or
    non-finite (the formula's entry-cost leg -- absent on the same rows
    that lack `setup_context.market_quality` entirely, i.e. largely the
    same rows already caught by `identity_mismatch`/
    `malformed_identity_post_263` above, but checked independently rather
    than assumed)
12. `unsupported_accounting_version` -- not one of the two frozen versions
13. `incomplete_accounting` -- `accounting_status != 'complete'` (no valid
    `recorded_net_pnl_usd` baseline)

Reasons 1-12 block computing `adjusted_net_pnl_usd` at all. Reason 13 alone
does not -- it only blocks the **paired** comparison against
`recorded_net_pnl_usd`; the row still counts in `coverage cohort` totals.
Historical double-`USDT` symbol rows (the known pre-#263 legacy gap) stay
in coverage, flagged `malformed_identity_post_263=False,
identity_mismatch=True`, excluded from paired economics exactly like every
other `identity_mismatch` row -- not specially resurrected.

## Output

Readiness funnel (closed trades -> accounting-complete -> quote-captured ->
comparable), original vs. adjusted total/mean/median net PnL, paired
per-trade difference (mean/median, descriptive), win rate, profit factor,
max drawdown, worst losing streak, distinct asset clusters / UTC weeks,
largest-cluster and busiest-week concentration, and a deterministic
cluster-bootstrap interval **on the mean adjusted net PnL itself**
(`clustered_inference.cluster_bootstrap_mean`, `DEFAULT_BOOTSTRAP_SEED`,
`DEFAULT_BOOTSTRAP_ITERATIONS`, symbol-base clusters) -- the quantity the
Verdict section actually gates on is "is adjusted economics robustly
positive", not "is the adjustment itself robustly nonzero" (the calibration
report already established that); the paired difference is reported as a
plain descriptive statistic, not separately bootstrapped.

Segments: strategy/version, exchange, normalized exit reason
(`initial_sl`/`max_hold`/`no_progress`/`trailing_stop`, parameters shown as
a separate field, not folded into the segment key), observed close spread
buckets (`<10`, `10-25`, `25-50`, `>=50 bps`), duration, leverage,
without-largest-asset-cluster (`leave_one_cluster_out_means`),
without-busiest-UTC-week.

No `$100`/`$250` depth projections -- every observation in this cohort was
requested at `<= $50`; this report never extrapolates past measured depth.

## Verdict

Asymmetric by design -- positive results need more evidence than negative
ones. A **hybrid** rule on the raw comparable-count floor specifically
(colleague review, 2026-08-25, reconciling the plan's own "negative EV
takes priority over insufficient diversity" against a second review's
"five random trades cannot support a confident terminal verdict either
way"): below 100 comparable trades, a negative point estimate is a
_diagnostic_, not a terminal `fail` -- at or above 100, a negative result
IS terminal regardless of asset/week diversity.

Evaluated in this order:

- **`insufficient_data` with `diagnostic=negative_point_estimate`**: fewer
  than 100 comparable trades (`DECISION_SAMPLE_SIZE`) AND adjusted mean net
  EV `<= 0` or profit factor `<= 1`. Not enough evidence for ANY confident
  verdict -- surfaced as a diagnostic so a reader isn't left wondering why
  an obviously-bad-looking number didn't fail outright.
- **`fail`**: `>= 100` comparable trades AND adjusted mean net EV `<= 0` or
  profit factor `<= 1` -- regardless of asset/week diversity from here on.
  A clearly negative result on a properly-sized sample needs no further
  diversity evidence to reject. `readiness.evidence_floor` still records
  the actual cluster/week counts either way, never hidden by this verdict.
- **`insufficient_data`** (no diagnostic): EV is positive but fewer than
  100 comparable trades, 30 distinct clusters, or 4 distinct UTC weeks
  (same floor as `exit_liquidity_calibration_report.py`'s
  `DECISION_SAMPLE_SIZE`) -- only a _positive_ result needs the full floor
  before it can be trusted.
- **`fragile_positive`**: floor cleared, adjusted mean net EV `> 0`, but
  either the bootstrap interval crosses zero, or the result flips sign
  with the largest asset cluster or the busiest UTC week removed
- **`historical_positive_requires_forward_confirmation`**: floor cleared,
  positive EV, bootstrap lower bound `> 0`, and robust to both
  leave-one-out checks

Required test matrix (all six cases, colleague review 2026-08-25):
`N=5` negative -> `insufficient_data` + diagnostic; `N=100` negative,
1 asset/1 week -> `fail` with diversity numbers still recorded; `N=100`
positive, 1 asset/1 week -> `insufficient_data`; mature diversified
negative -> `fail`; mature positive with unstable bootstrap/leave-one-out
-> `fragile_positive`; mature robust positive ->
`historical_positive_requires_forward_confirmation`.

Even `historical_positive_requires_forward_confirmation` authorizes only a
new prospective/forward-registered contract on an untouched cohort --
**never** a live-capital decision from this retrospective number alone.

## Delivery

```
make exit-liquidity-adjusted-net-economics-report
make prod-exit-liquidity-adjusted-net-economics-report
```

CLI modes: `--freeze-artifact`, `--from-artifact <fingerprint>`,
`--format json|markdown` (same interface `exit_liquidity_calibration_
report.py` shipped in #288).

Post-merge verification (manual, once):

1. Rebuild only the `analytics` image (`make prod-deploy`).
2. On a clean production `main`, freeze the cohort with a fixed `--until`.
3. Run the artifact validator (`make prod-research-dataset-artifact-validate`).
4. Re-render the report through `--from-artifact`.
5. Diff the economics payload of the live-DB run against the
   `--from-artifact` run -- must be identical.
6. Archive the JSON, the Markdown, and the artifact's own SHA-256 files
   under `backups/reports/`.

## Explicitly out of scope

Trading config, new thresholds, execution changes, live orders, and any
`early_momentum` refactor. The PR after this one is determined by this
report's own verdict: either the `pump_short` v1 short-exit contract is
retired, or a new, untouched forward cohort is registered under its own
contract.
