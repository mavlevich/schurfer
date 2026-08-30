# Source-lead forward cohort v1

## Purpose

This contract answers, on identity- and route-verified captures never touched
by any earlier analysis, whether an immediate long entry on the qualified
target exchange -- the moment Gate shows a leading source-lead capture --
holds a real, after-cost edge over the following half hour.

This is a distinct question from the older `source_lead_report.py` discovery
screen (`SOURCE_LEAD_COHORT_START`, 2026-07-24): that report reads
`pump_events`/`pump_event_sources` directly, across every exchange that ever
first-saw a pump, with no identity registry and no executable-liquidity
check. This contract instead reads the live, forward-only
`source_lead_prospective_capture_v1` capture pipeline
(`source_lead_capture.py`, `source_lead_qualification.py`) -- exact Gate
identity, exact registered target identity, real captured order-book
liquidity, and (as of research/gate-source-lead-registry-activation-v3)
independently verified derivative-market route evidence, not a provisional
symbol guess. It is prospective, research-only measurement. It does not
modify `orders.py`, `trader.py`, position size, leverage, or
`DRY_RUN`/`AUTO_TRADE`.

**Scope today: Gate -> Binance only.** Registry v3 carries 14 canonical
assets with 28 links -- 14 Gate and 14 Binance, zero Bybit. Any future
mention of "identity- and route-verified assets" in this contract means
exactly that Gate/Binance set, not a Binance/Bybit choice; there is nothing
to select between yet.

## Relationship to HYP-012 -- narrower estimand, not a claimed replication

`docs/research/discovery-ledger.md` HYP-012's frozen primary metric is a
**paired delta** (early-entry net return minus confirmation-entry net
return), cluster-bootstrapped and **Holm-corrected across 4 source/execution
routes** (gate/mexc -> binance/bybit). Its own "Next" instruction calls for
re-running that exact paired, 4-route, Holm-corrected family on an untouched,
identity-verified forward cohort before any promotion decision.

The identity- and route-verified registry (v3) covers exactly **one** of
those four routes -- gate -> binance. Replicating the full 4-route Holm
family is not possible on this registry; claiming this cohort confirms
HYP-012 in that original sense would misrepresent what a
single-route-verified registry can actually test.

This contract is registered as its own estimand instead
(`ESTIMAND_VERSION = "standalone_early_entry_net_return_v1"` in
`source_lead_forward_cohort.py`): **standalone after-cost net return** on
the qualified gate -> binance route alone, not a comparison against waiting
for confirmation. That is honestly a different -- arguably more directly
money-relevant -- question than HYP-012's original "does early beat
waiting" claim, and it needs no multiple-testing correction: there is
exactly one primary test, not four.

Where a confirming second-exchange source does appear within the hour (the
same `pump_event_sources` signal `SourceLeadProgress.confirmed_within_hour`
already surfaces), the original HYP-012 paired early-vs-confirmed delta is
still computed and reported as a **secondary diagnostic**
(`SECONDARY_DIAGNOSTIC_VERSION`) -- kept for continuity with HYP-012's
design and useful for telling "informational lead" apart from "pumps go up
regardless of when you enter" -- but it never gates the verdict below.

## Why now, and what is frozen before vs. after the cohort starts

`ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED` flipped to `True` in
research/gate-source-lead-registry-activation-v3 (PR 3 of 3):
`qualify_source_lead` can, for the first time, actually return
`status='qualified'`. Registering now, before any qualified capture exists,
is what makes the read honest: the contract is fixed before the data that
will be judged by it.

Everything that determines a single episode's resolved outcome is frozen in
code now, not deferred: entry/exit price sources, the exit-bar alignment and
gap policy, the fixed conservative exit-slippage haircut, unresolved
reasons, and the verdict rule itself are all implemented as **pure
functions** (`resolve_episode`, `formal_verdict` in
`source_lead_forward_cohort.py`) with synthetic-input unit tests --
deferring these to whenever the evaluator gets written would let that later
code choose resolution mechanics with the real outcome already in view,
reversing this codebase's own prospective-research discipline. Only the
DB-fetching, CLI, and Markdown-rendering plumbing around those two pure
functions is deferred -- there is nothing prospective-research-sensitive
about that plumbing, and the earliest point it could produce a first read is
~2026-10-01 (cohort start plus the 4-week floor) regardless of when it is
written.

## Frozen cohort and selection

All values live in `source_lead_forward_cohort.py`
(`CONTRACT_VERSION = "source_lead_forward_cohort_v1"`):

- **Cohort start**: `SOURCE_LEAD_FORWARD_COHORT_START`, aliased to (not
  copied from) `IDENTITY_REGISTRY_V3_START` -- `2026-09-03T00:00:00Z`.
- **Candidate set**: every `app.source_lead_qualifications` row with
  `status='qualified'` and `qualification_version='source_lead_qualified_
capture_v3'` whose capture's `source_first_observed_at` is at or after
  the cohort start.
- **Entry**: frozen at `0m`, no artificial delay, no re-fetch.
  `entry_price` = the qualification result's own selected
  `TargetObservation.liquidity["ask_vwap"]` -- the exact executable VWAP
  `qualify_source_lead` already proved fillable, on the fixed
  `SOURCE_LEAD_NOTIONAL_USD` ($50) quote.
- **Exit / outcome**: `+30m` primary horizon. Nothing in the live capture
  pipeline fetches a fresh executable quote 30 minutes after entry -- a new
  live capture worker for that (mirroring
  `trade_exit_liquidity_observations`) is a bigger lift than registering a
  cohort should require. `exit_price` is instead the first fully-closed 1m
  OHLCV bar at or after `entry_at + 30m` (`ceil`, never `floor`), explicitly
  labeled a **proxy** (`EXIT_PRICE_SOURCE_VERSION = "ohlcv_close_proxy_v1"`),
  never claimed to be an executable quote -- with a fixed, conservative
  `EXIT_SLIPPAGE_BPS_ASSUMED = 15.0` bps haircut charged against it so an
  unrealistically clean proxy fill can never manufacture an edge that would
  not survive a real one. An episode is `unresolved` (not a synthetic
  worst-case fill) if the nearest usable bar is more than
  `MAX_EXIT_BAR_GAP_MINUTES = 2.0` from the ideal boundary.
- **Costs**: this codebase's shared conservative model
  (`packages/performance/schurfer_performance.DEFAULT_COSTS` --
  `taker_fee_bps_per_side=10.0`, charged on both entry and exit; no
  funding, since a 30-minute hold never crosses an 8h settlement).
- **Episode definition**: one `app.source_lead_qualifications` row is one
  episode -- no additional cooldown/dedup logic, since each Gate pump event
  is already a discrete, upstream-deduplicated unit (`app.pump_events`).
- **Checkpoint / stopping rule**: evaluate once, at the earliest point
  where both the episode and week floors below are met (the same
  "earliest maturity prefix" convention this codebase's other registered
  contracts use) -- never re-peeked at incrementally. An early look that
  fails the floor is `insufficient_data`, not a result, and is not logged
  as one.

## Evidence floor -- registered as a small-universe study

`EVIDENCE_FLOOR`: 100 resolved episodes, 7 distinct asset clusters, 4
distinct UTC weeks. `min_resolved_episodes`/`min_distinct_utc_weeks` match
this codebase's other registered contracts. `min_distinct_asset_clusters`
is deliberately **not** the usual 30: the entire identity- and
route-verified candidate universe is 14 canonical assets, so a 30-cluster
floor is unreachable here by construction, forever -- registering it
anyway would mean this cohort could never produce anything but
`insufficient_data`.

Because a low cluster floor alone cannot stop one asset from dominating the
verdict (seven distinct assets contributing episodes can still mean one of
them supplies the overwhelming majority), two explicit concentration caps
apply in addition to the floor, unconditionally:

- `MAX_SINGLE_ASSET_EPISODE_SHARE = 0.35` -- no one asset may supply more
  than 35% of resolved episodes.
- `MAX_SINGLE_WEEK_EPISODE_SHARE = 0.45` -- no one UTC week may supply more
  than 45%.

**A verdict reached under this small-universe floor does not by itself
authorize paper or live execution.** `SMALL_UNIVERSE_PROMOTION_NOTE`: a
`candidate` result here authorizes only registering a broader confirmatory
cohort once the identity registry covers more assets and/or exchanges --
the same layered-gate pattern this codebase already uses elsewhere (a
`Confirmed` maker-entry-prospective result likewise only authorizes
registering a real shadow contract, not live trading directly).

## Verdict rule

`formal_verdict` (pure function, `source_lead_forward_cohort.py`), given
already-aggregated statistics:

- **`insufficient_data`** if the episode/cluster/week floor is not met, if
  either concentration cap is exceeded, or if a cluster-bootstrap CI could
  not be computed.
- **`fail`** if the floor and concentration caps are met and the primary
  estimand's 95% cluster-bootstrap CI lower bound is not strictly positive.
- **`candidate`** only if the floor and concentration caps are met AND the
  CI lower bound is strictly positive -- subject to
  `SMALL_UNIVERSE_PROMOTION_NOTE` above.

## Required output (once the report exists)

Coverage funnel (captured / qualified / resolved / unresolved and why, by
`UNRESOLVED_REASONS`), per-asset and per-week concentration against the
caps above, the primary standalone-return sensitivity with its
cluster-bootstrap CI, the HYP-012-continuity secondary diagnostic
(paired early-vs-confirmed delta, where a confirmation exists, reported but
never gating), and per-episode results -- same shape as this codebase's
other registered forward contracts, reading `source_lead_forward_cohort.py`'s
frozen constants and pure functions directly rather than redefining any of
them.
