# early_momentum_v4 net-evidence report

**Report:** `analysis/early-momentum-net-evidence-v1`
**Code:** `apps/analytics/schurfer_analytics/early_momentum_net_evidence*.py`
**CLI:** `make early-momentum-net-evidence-report ARGS="--cohort-end <ISO8601> --format markdown|json"`

Answers, honestly and reproducibly: does `early_momentum_v4` have a positive
net edge after real costs, is it robust, and is there enough evidence to
move toward `LIVE_MICRO`? Read-only against production Postgres; never
writes, never executes a trade, never touches the running strategy.

## Frozen cohort contract

These values are fixed **before** the first report run and are not
retuned after seeing results. Changing the strategy after this analysis
means a new `early_momentum_v5` and a fresh, untouched cohort.

| Field                    | Value                                                                           |
| ------------------------ | ------------------------------------------------------------------------------- |
| `report_version`         | `early_momentum_net_evidence_v1`                                                |
| `strategy`               | `early_momentum` v4                                                             |
| formal cohort start      | `2026-08-23T14:53:57.243399Z` (`FORMAL_COHORT_START`)                           |
| expected contract SHA256 | `bdda6c6423b0cc69d8b6266269cda07c31e20f4d256b1793229ab47beb5cb1ac`              |
| accounting version       | `paper_conservative_costs_v1` (`schurfer_performance.PAPER_ACCOUNTING_VERSION`) |
| mode                     | paper only                                                                      |
| side                     | long only                                                                       |

Cohort membership is anchored on **`episode.armed_at`**, never
`trade.entry_at`. A trade can open after the formal cutoff while its
episode armed before it (armed just before a deploy, opened just after) --
that trade is not formal evidence. `app.early_momentum_episodes` is the
authoritative source; `trade.setup_context->>'strategy'` is only ever a
cross-check, used to catch a trade that claims to be v4 with no matching
episode at all (itself an integrity violation, never a second membership
path).

## Cohort maturity

`--cohort-end` must be at least `COHORT_MATURITY_BUFFER_SECONDS` (6 hours)
older than the database's own clock (`SELECT now()`, captured as the first
statement inside the report's own read-only transaction -- never the report
process's local clock). The buffer is:

```
episode TTL (<= 1h, ttl_seconds=3600 at arm time)
+ max hold (4h, _EXIT_PARAMS["max_hold_min"] = 240.0)
+ 1h operational buffer
= 6h
```

An immature `--cohort-end` raises `CohortNotMatureError` and produces no
report at all -- this is enforced in code (`generate_report`), not only
documented.

## Provenance: git revision and working-tree cleanliness

The analytics container does not carry `.git`/source, so the report
process cannot call `git` itself. `--code-revision` and
`--working-tree-dirty`/`--no-working-tree-dirty` are computed by the
Makefile (which does have `.git`, on the host or in prod) and passed in as
required CLI arguments -- the same pattern every other report in this
package already uses. `--code-revision` is validated as a non-empty,
SHA-like identifier.

A dirty working tree never silently changes the computed verdict (that
would conflate git hygiene with economics) -- but the report carries a
separate `formal_run: bool` field (`not working_tree_dirty`), and a
`pass_live_micro_candidate` verdict is only an actual authorization to
start the `LIVE_MICRO` implementation PR when `formal_run` is `True`. A
dirty-tree run still renders its full verdict, prominently marked
**NOT A FORMAL RUN** in the Markdown output.

## Evidence funnel (16 steps, nothing dropped silently)

Each step reports remaining/excluded counts and up to 5 example IDs for
every exclusion:

1. All formal v4 episodes (`armed_at` in `[cohort_start, cohort_end)`)
2. Correct strategy ID and contract hash
3. Valid canonical identity (route resolution succeeded at arm time)
4. Reached claim/open, or has an explained terminal reason -- a
   still-armed/claimed episode past its own maturity horizon is a
   row-level violation here, not a silent exclusion (see below)
5. Exactly one trade-leg per episode
6. Trade is genuinely paper
7. Side is long
8. Strategy metadata (trade's own FK) matches the episode
9. Route and strategy-label identity consistent:
   `trade.exchange == episode.exchange`,
   `trade.symbol == episode.execution_symbol`,
   `trade.setup_context.strategy == "early_momentum_v4"`, and
   `trade.entry_idempotency_key == f"{episode_id}:entry:base"`
10. Temporal sanity (see below)
11. Trade is closed and its outcome is mature
12. `accounting_version` matches the frozen contract
13. `accounting_status` is `complete`
14. Gross/net PnL, fees, funding, slippage are all populated
15. Accounting arithmetic actually reconciles (see below) -- step 14 only
    checks presence, this checks the numbers are mutually consistent
16. Final comparable set

## Integrity severity

**Cohort-level** (blocks the entire report -- a cohort-definition failure,
not a single bad row): unexpected contract hash, multiple contract hashes
observed inside the formal window.

**Row-level** (excludes just that episode/trade from the comparable set,
but blocks formal `PASS` until remediated -- see Verdict below):
`v4_trade_without_episode`, `episode_opened_without_trade`,
`multiple_trades_per_episode`, `not_paper`, `unexpected_side`,
`strategy_identity_mismatch`, `route_or_strategy_identity_mismatch`,
`temporal_inconsistency`, `episode_stuck_unresolved_past_maturity`,
`open_past_maturity_horizon`, `accounting_version_mismatch`,
`incomplete_accounting_on_closed_trade`, `missing_required_accounting_field`,
`pnl_present_despite_incomplete_accounting`,
`accounting_arithmetic_inconsistent`.

**Not a violation** (excluded from the funnel, no flag): a normal
rejected/expired/suppressed episode that never opened; an open trade still
within its own maturity horizon; an administratively cancelled trade; an
episode still armed/claimed that has _not yet_ reached its own maturity
horizon (a normal right-censored case -- the reaper simply hasn't had a
chance yet). Once an armed/claimed episode HAS passed its own maturity
horizon, it is instead a row-level violation
(`episode_stuck_unresolved_past_maturity`): a mature cohort run can only
happen once the whole cohort is already well past its own maturity buffer
(see Cohort maturity above), so a still-unresolved episode at that point
means the reaper genuinely never got to it -- a real lifecycle failure, and
a possible selection-bias risk if silently dropped (an unexecuted signal
disappearing from the funnel would otherwise let the report show `PASS`
while hiding exactly the failures that should count against it).

### Accounting arithmetic reconciliation

Step 14 only checks that PnL/cost fields are non-`None`; step 15 checks
they are mutually consistent (within DB-storage-rounding tolerance,
`rel_tol=1e-3`/`abs_tol=0.05`), so a corrupted or miscomputed row with
implausible numbers can never reach economics as "complete":

- `size_usd`, `leverage`, `entry_price`, `exit_price` are all finite and
  positive
- `gross_pnl_usd ≈ size_usd * gross_pnl_pct / 100`
- `net_pnl_usd ≈ gross_pnl_usd - fees_usd - funding_usd - slippage_usd`
- `net_pnl_pct ≈ net_pnl_usd / size_usd * 100`

**Descriptive economics is always computed on the clean comparable
subset, even when row-level violations exist elsewhere in the dataset** --
but the formal verdict is `invalid_integrity` whenever _any_ row-level
violation exists, not just cohort-level ones. A single unexplained anomaly
among hundreds of trades can be a symptom of a broader problem; silently
excluding it while still authorizing real money is the wrong default. The
report must be re-run after remediation, never auto-recovered by exclusion.

### Temporal sanity

- `trade.entry_at >= episode.armed_at - 5s` (a small NTP/clock-skew
  tolerance across processes/hosts, not a strict `>=`)
- `episode.expires_at > episode.armed_at`
- `episode.claimed_at >= episode.armed_at - 5s` (when present)
- `trade.exit_at >= trade.entry_at` (when present)
- an `opened`/`closed` trade's episode has `claim_attempts >= 1`

## Economics, concurrency, robustness, capacity

Computed on the final comparable set: win rate, gross/net mean/median
return **both on notional and on margin** (`size_usd / leverage` --
leverage is fixed at 5x, so these two bases diverge materially and both
are reported, since return-on-margin is what actually matters for a
`LIVE_MICRO` capital-sizing decision), p05/p25/p50/p75/p95 net return,
total gross/net PnL, fees/funding/slippage separately, profit factor,
worst trade, worst losing streak, an equity curve and max realized
drawdown ordered by `exit_at`, and cuts by canonical cluster (never raw
per-exchange symbol -- one asset can trade on two venues under two
different native symbols), UTC day, UTC week, source exchange, and parsed
exit reason.

Exit reason is parsed from `Trade.notes`'s leading token
(`take_profit`/`max_hold`/`no_progress`/`initial_sl`/`trailing_stop`,
matching `exit.py`'s five `return f"..."` strings exactly); an
unrecognized token stays `unknown` rather than becoming an invented new
category.

Concurrency (max/time-weighted-mean/p95 concurrent positions, deployed
notional, required margin) uses a sweep-line over entry/exit instants,
processing a close before an open at an identical timestamp so an
instantaneous exit+entry never double-counts. Entry waves group
consecutive entries with no gap larger than `ENTRY_WAVE_GAP_SECONDS` (60
minutes) -- pre-registered, never retuned after seeing results.

Capacity evidence uses only liquidity actually recorded at the traded
size -- `setup_context.entry_vwap_impact_bps` for the long entry (a
top-level field, the real reading at the actual traded notional;
_not_ `setup_context.market_quality.ask_impact_bps`, which is measured at
the market-quality gate's larger safety-margin depth target and would
otherwise misrepresent the real entry cost) and
`TradeExitLiquidityObservation.bid_impact_bps` for the long exit -- never
extrapolated to a larger notional the order book was not measured at.

## Robustness

- Leave-best-asset-out: exclude the single canonical cluster with the
  largest total net PnL contribution, recompute the mean.
- Leave-one-week-out: `clustered_inference.leave_one_cluster_out_means`
  over UTC-week clusters.
- Mean net return excluding the single best UTC day.
- Deterministic block bootstrap over UTC-day clusters
  (`clustered_inference.cluster_bootstrap_mean`, fixed seed
  `DEFAULT_BOOTSTRAP_SEED`, `DEFAULT_BOOTSTRAP_ITERATIONS` draws), reporting
  a **90%** confidence interval on the mean net return -- see below for why
  90% and not the more conventional 95%.

**Caveat, always shown in the report:** at exactly the minimum evidence
floor (100 closed / 30 clusters / 4 UTC weeks), block-bootstrap-by-day and
leave-one-week-out draw from very few blocks (as few as ~28 days / 4
weeks) -- confidence intervals and leave-one-out results are
correspondingly wide/weak right at the floor. A narrow `PASS` at exactly
the floor is provisional; more weeks of evidence meaningfully tighten
these numbers.

## Why 90%, not 95%

The 90% block-bootstrap lower-bound gate is used **only** to authorize the
minimal, hard-capital-limited `LIVE_MICRO` step -- it is calibrated to the
same 0.10 significance level already used in this repo's prior validation
protocol (`liquidation_cascade_validation_report.py`'s
`SHUFFLED_LABEL_SIGNIFICANCE_THRESHOLD`). Raising position size beyond
`LIVE_MICRO` requires a longer evidence window **and** the tighter 95%
confidence gate -- a deliberate, documented tiering (looser bar for a
small, reversible first step; tighter bar before committing more capital),
not an arbitrary or accidental threshold.

## Verdict

Machine-readable states: `invalid_integrity`, `insufficient_data`, `fail`,
`pass_live_micro_candidate`.

**Minimum formal evidence floor:** `>= 100` closed, accounting-complete
trades; `>= 30` distinct canonical clusters; `>= 4` distinct UTC weeks. At
`>= 50` closed trades an **interim checkpoint** may be published
(`is_interim_checkpoint = True`), but the verdict itself stays
`insufficient_data` until the floor is cleared -- an interim number is
never a formal `PASS`, however good it looks.

**`PASS` requires, once the floor is cleared:**

- mean net return (on notional) `> 0`
- total net PnL `> 0`
- profit factor `>= 1.20`
- 90% block-bootstrap lower bound `> 0`
- leave-best-asset-out mean net return `> 0`
- every leave-one-week-out result `> 0`
- zero cohort-level **and** zero row-level integrity violations

Win rate is explicitly **not** a promotion gate -- net EV, profit factor,
and robustness are.

`pass_live_micro_candidate` does not trade anything automatically; it is
the machine-readable signal that the `LIVE_MICRO` implementation PR may
begin, **and only when `formal_run` is also `True`**.

## Legacy versions (v1/v2/v3) -- context only, never mixed into v4

Shown as a strictly separate descriptive table
(`fetch_legacy_context`/`LegacyContextRow`), scoped by
`setup_context->>'strategy'`, PnL summed only over `accounting_status =
'complete'` closed rows. Never joined with, or added to, the v4 formal
number. v1 (pre-episode-lifecycle) rows include the legacy-orphan/cancelled
rows reconciled in `fix/legacy-paper-orphan-reconciliation-v1` -- an
integrity appendix, not economics. Momentum Flow comparison and any
ensemble work are explicitly out of scope for this report; MFE/MAE and
alternative-exit analysis are a separate exit-optimization study.

The orphan cross-check window (trades with no `episode_id` claiming to be
v4) extends past `cohort_end` by `COHORT_MATURITY_BUFFER_SECONDS`: an
episode armed just before `cohort_end` can still open its trade after
`cohort_end` (episode TTL/trigger delay), so a naive `entry_at <
cohort_end` bound would miss exactly that orphan if its `episode_id` link
were ever lost.

## Reproducibility fingerprint

`dataset_fingerprint` hashes **every field of every dataclass** in the raw
dataset -- every formal episode, every linked/orphan trade row, every
exit-liquidity observation -- generically over each dataclass's own field
list (`dataclasses.fields`), not a hand-picked subset. Two runs that differ
in any economically or integrity-relevant way (prices, size/leverage, PnL
percentages, identity fields, `execution_symbol`, `claim_expires_at`,
exit-liquidity spread/impact/latency/error, or anything else on any of the
three row types) always produce different fingerprints; a changed exclusion
between two runs against the same nominal cohort window is always
detectable. The report also always prints: `report_version`,
`code_revision`, `working_tree_dirty` / `formal_run`, `db_snapshot_at`,
`cohort_start`/`cohort_end`, and the expected vs. observed contract
hash(es).

## First run

The formal cohort started `2026-08-23T14:53:57.243399Z`; with the 6-hour
maturity buffer, no `--cohort-end` is valid (`CohortNotMatureError`) until
`2026-08-23T20:53:57Z` at the earliest, and a meaningful verdict requires
real trading volume well beyond that single instant. A mechanical
end-to-end smoke run (`--cohort-end` before the cohort even starts, so
zero episodes are in scope) against the local dev database confirmed the
full pipeline -- connection, the single REPEATABLE READ read-only
transaction, funnel, economics, concurrency, robustness, capacity, verdict,
Markdown and JSON rendering -- runs cleanly end to end and correctly
reports `insufficient_data` on an empty dataset rather than a false `PASS`
or a crash. The first real interim checkpoint (`>= 50` closed trades) will
follow naturally once the strategy has accumulated enough paper volume;
none of the gates above are retuned to get there sooner.

## First formal read (2026-09-09)

First run over real matured volume. Provenance:
`code_revision=ae4a89a570b9f0263395a0fdae2c3b3797f23be4`,
`working_tree_dirty=False`, `formal_run=True`,
`cohort_end=2026-09-09T06:00:00Z`, `db_snapshot_at=2026-09-09T12:23:13Z`,
observed contract hash equals the pinned expected hash, zero cohort-level
and zero row-level integrity violations.

**Verdict: `fail`.** The evidence floor is cleared (438 comparable trades
across 4 UTC weeks, well past the 100-trade/30-cluster/4-week floor), and
every `PASS` gate fails:
`mean_net_return_not_positive`, `total_net_pnl_not_positive`,
`profit_factor_below_threshold`, `bootstrap_lower_bound_not_positive`,
`leave_best_asset_out_not_positive`, `leave_one_week_out_not_all_positive`.

Headline numbers:

- total net PnL `-117.60 USD`; total gross PnL `-20.01 USD`; fees `87.60`,
  funding `9.99`, slippage `0.00`.
- mean net return `-0.27%` / trade on notional (median `-0.45%`); profit
  factor `0.77`; win rate `40.6%` (178/260).
- block-bootstrap 90% CI on mean net return `[-0.55%, -0.02%]` -- the whole
  interval is below zero; leave-best-asset-out `-0.30%`; every
  leave-one-week-out value negative.

The decisive structural fact is that **gross PnL is already negative
(`-20.01 USD`) before any cost.** The signal shows no positive forward edge
on this cohort; fees and funding (`-97.59 USD` together) deepen a
flat-to-negative gross rather than eroding a real one. Round-trip cost is
~22 bps/trade (Bybit taker plus ~5.7/5.5 bps entry/exit impact at the only
measured size, `100 USD` notional) against a gross return of ~`-4.6`
bps/trade. Exit mix: `take_profit` 71 trades `+281.67` (`+3.97%` mean);
`max_hold` 363 trades `-348.49` (`-0.96%` mean, 83% of the set);
`initial_sl` 4 trades `-50.79` (`-12.70%` mean).

Honest scope limit: this is ~17 days and one market regime, and the report
itself flags results as provisional at the floor (weekly swings are large,
W36 was near flat at `-0.03%` over 218 trades). The defensible reading is
"no edge demonstrated, gross ~0," not "proven unprofitable forever."

Implication for delivery order: `early_momentum_v4` as frozen here has no
demonstrated executable edge, so building entry-filter or exit-tuning
superstructure on top of this exact signal is not justified by this
evidence. A filter (e.g. the `moderate_15m_taker_imbalance_filter_v1`
discovery candidate) is a subset of a gross-negative population and would
have to clear the full ~22 bps cost on its retained slice to matter; run it,
if at all, as a cheap SHADOW that cleanly parks the candidate, not as the
primary path to positive economics. Owner economics
([ECONOMICS.md](../../ECONOMICS.md)) remain unfilled, and capacity is
measured only at `100 USD` notional, so even a rescued variant would need a
materiality decision before live work.

Independent cross-check (prospective cohort, same `db_snapshot`). The
`early_momentum_prospective_cohort_report` over the isolated forward window
(`cohort_start 2026-08-25T06:25:00.970709Z`, deliberately excluding the
historical v1-v4 data this report already covers) is still `collecting`
(`distinct_utc_weeks_3_below_4`, so its own formal verdict is
`insufficient_data` until W38), but its 399-trade economics match this read
almost exactly: gross `-8.40 USD`, net `-97.28`, mean net `-0.24%`/trade,
profit factor `0.79`, leave-best-asset-out `-0.27%`, every leave-one-week-out
value negative (`-0.20% / -0.50% / -0.14%`). Because that window is untouched
by any candidate selection, it independently rules out the "negative only
because the signal was fit on this same data" explanation: a fresh forward
cohort shows the same no-edge, cost-dominated result. (Its bootstrap CI upper
bound sits marginally above zero at `+0.02%` purely from having 3 weeks
rather than 4; the point estimate and every other cut stay negative.)
