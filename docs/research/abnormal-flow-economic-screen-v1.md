# Abnormal-flow economic screen v1

## Decision this study may support

Test whether an **early, same-venue OI-and-taker-flow imbalance with limited
contemporaneous price movement** predicts a tradeable long move over 12 hours,
above a matched price/liquidity baseline. A historical pass is discovery-only:
it can nominate at most one separately registered forward cohort, not PAPER or
live trading. A historical failure closes only this declared bar-level
Bybit/Binance mechanism. It says nothing about uncollected venues or L2 state.

The primary candidate is frozen: 60-minute feature lookback,
long-only, 720-minute outcome horizon. The numerical OI, buy-pressure, and
price-containment thresholds, entry cost, missingness controls, and portfolio
policy are registered and immutable for the evaluation window.

## Population and information boundary

- Scan every captured `(exchange, native instrument, market_type, minute)` in
  the declared historical window. Neither pump events, Telegram alerts,
  current catalog membership, nor subsequently known winners select rows.
  Bybit and Binance remain separate exact-native paths; do not join on ticker
  or pool their raw OI units.
- Primary input is `timeseries.bybit_momentum_bars_1m`, `market_type=linear`,
  `capture_version=v1`. Pin the input window, universe/capture versions,
  per-day Parquet manifests and file hashes before the first outcome read.
  A present bar is not necessarily usable: require the relevant
  `price_complete`, `trades_complete`, and `open_interest_complete` flags.
- The decision clock is **after** finalization of the last feature bar.
  The replay must check `created_at <= decision_at`, trade receive time and
  OI observation time `<= decision_at`; it must never use the deciding
  minute's final bar before it was available. Choose and freeze the scan lag
  from outcome-blind finalization-lag data. If a required timestamp or bar is
  absent, classify the row as unavailable; never replace it with a later
  observation.
- Use a continuous 60-minute history on the exact route and source version.
  OI change is a _within-instrument percentage_ of native OI amount, not a
  cross-venue comparison of OI levels. Binance's USD OI-value field is not
  available in the sampled data, so a shared signal must not silently treat
  it as zero or substitute an unversioned mark-price conversion. Buy pressure
  uses actual accepted taker-buy and taker-sell notional; zero-flow windows
  are explicitly unavailable for the ratio.
- Eligibility floor (frozen with the thresholds, `min_oi_notional_usd` +
  participation). Native OI is converted to USD by ONE registered per-venue rule
  (`bybit_native_value_binance_amount_x_decision_price_v1`), verified against the
  collector: Bybit publishes both a native OI amount and a native USD
  open-interest value, so its USD OI is that native value; Binance publishes only
  the base-asset OI amount and no USD value, so its USD OI is that amount times the
  decision-time price. No venue is treated as zero or given an unversioned
  substitute. Participation divides the fixed position by PRE-DECISION turnover
  accumulated over a short frozen window ending at the decision
  (`entry_execution_window_minutes`, strictly shorter than the 60m lookback), a
  realistic fill period -- never the whole hour and never the future entry minute.
  A venue/day below the floor is ineligible, so the signal is not a small-cap noise
  detector and is at least nominally tradeable.
- The formal run is pinned to versioned executable rules, not free-text labels:
  the calibration, OI-USD conversion, entry, exit, matching, and funding fields
  must each name a rule implemented and reviewed in the contract module, and the
  run also pins literal tz-aware UTC window boundaries and an input fingerprint
  (the audit's aggregate hash) it must reproduce. A run over any other window,
  dataset, or unregistered rule refuses, so neither the scored window nor the
  operationalization can be chosen from results.
- Buy/sell USD semantics differ by venue and must be verified against the writer
  and real rows before use: Bybit sums individual trades, Binance sums aggTrades;
  both flow through price x size but are not identical. Describe and test this; if
  the notional is not comparably available on a venue, narrow scope rather than
  compare unlike quantities.
- Canonical asset identity is resolved as of the decision time for clustering
  and duplicate control. An unresolved identity is counted in the funnel and
  cannot be silently treated as a distinct ticker asset.

## Frozen signal and control

The **one primary** screen requires (a) positive within-instrument OI growth
over the prior hour (percent of native OI amount), (b) aggressive taker-buy
notional dominating sell notional (> 0.5 of buy+sell USD) over that same hour,
and (c) restrained price movement before the decision, measured as the MAXIMUM
deviation of the prior hour's 1m bar highs and lows from the window's opening
price (`price_containment_max_bar_dev`) -- bar extremes, not closes, so an
intraminute wick that closes back near the open (e.g. SAYLORMOON) is NOT counted
as restrained. This tests a pre-breakout accumulation mechanism, not the
already-tested near-trigger one-minute imbalance and not HYP-012's cross-venue
lead. The OI ABLATION runs the same primary cell over the SAME eligible set with
only the OI-growth threshold removed, so it isolates the OI effect and never
changes the token composition; no incremental OI benefit closes the mechanism.

Before reading returns, freeze either fixed thresholds or one deterministic
_outcome-blind_ threshold-calibration rule, its calibration window, and a
single selected cell. No post-result sweep over lookbacks, directions,
horizons, price-containment cutoffs, or venues may nominate a winner. Shape,
buy-burst, liquidation, and short-side variants are diagnostics or future
separate hypotheses, not hidden primary cells.

Form episodes before evaluation: one first qualifying decision per
`(exchange, canonical asset)` followed by a frozen cooldown at least as long
as the 720-minute horizon. A cross-venue same-asset overlap is reported for
portfolio concentration; it is not silently multiplied into independent
asset evidence. Controls are selected point-in-time from the same venue,
calendar regime, liquidity band, and pre-decision price-movement band, using
a frozen deterministic matching rule. Report both standalone strategy net
economics and excess over that matched control; a profitable market regime
alone is not alpha.

## Outcome and executable-price limitation

The historical minute bars contain OHLC and a last bid/ask, but **not the
timestamp of that last quote**. A quote stored in the minute bar is not proof
it was available or fillable at a chosen intraminute entry. Therefore the
historical replay must label its next-bar entry and exit as a **priced proxy**,
charge a pre-registered conservative spread/slippage/fee/funding model, and
show a break-even-cost sensitivity. It must not claim measured executable
fills or capacity from these bars alone. Same-venue gaps or missing quotes
are unresolved, never filled from another venue or later hindsight.

If the bar-level economics and matched excess both survive, the next gate is
a bounded point-in-time quote/depth/latency shadow on the candidate fires.
Only that gate can support a claim about executable size. A failing bar-level
screen does not justify building the shadow or adding exchanges.

## Required replay artifact and ordered decision

The implementation PR may contain the contract and tested replay code, but
**no result or economic conclusion**. Before running it, pin the exact input
window, data/code fingerprints, one primary threshold rule, entry/exit and
cost model, missingness ceilings, evidence floor, selection/cooldown,
portfolio bank and slot policy, baseline matching, and the one-shot verdict
rule. At minimum the output includes:

1. full scanned/eligible/fire/episode/resolved/unresolved funnel by venue and
   rejection reason, including zero-flow, stale OI, unavailable price,
   incomplete lookback, and missing outcome;
2. standalone net return and matched excess with cluster- and week-aware
   uncertainty, asset/week concentration, and leave-one-out checks;
3. signals per week, notional/participation proxy, concurrency, capital
   occupancy, drawdown and losing streak for the frozen fixed-bank portfolio;
4. dollar PnL for the actual historical window at the $300 research bank,
   conservative cost sensitivity, and an explicitly labelled capacity
   _unknown_ rather than an invented scaling claim.

A mature negative standalone net result is a stop before diversity commentary.
Positive but underpowered evidence is insufficient, not promotion. A candidate
requires positive standalone after-cost economics **and** matched excess,
acceptable portfolio risk, and a material dollar path; historical discovery
alone never authorizes live trading.

## Outcome-blind capability check (2026-09-19; not an edge result)

One day of production bars, `[2026-09-18, 2026-09-19) UTC`, was aggregated
without reading forward returns. At production revision `72ff35e`, both
venues had price, trade, and OI-amount observations and positive bid/ask
fields. The row counts were Bybit 741,600 and Binance 750,405. Bybit had
735,676 `price_complete` and `open_interest_complete` bars and 737,280
`trades_complete` bars; Binance had 750,405 for all three. OI-value was
nonnull on all Bybit rows and **zero Binance rows**; native OI amount was
nonnull on every row of both venues. This is a one-day operational preflight,
not a fingerprinted research dataset or a guarantee of historical continuity.

The query was a bounded same-day aggregate on the exact bar table, with no
outcome join. Before implementation, run a repeatable outcome-blind coverage
audit over the proposed research window and archive its query, revision,
row counts, coverage reasons, and output hash. If the input cannot support
the declared signal on both venues, narrow scope **before** any outcome read;
do not silently loosen quality gates after seeing returns.

The offline `abnormal-flow-input-audit` command implements the first part of
that gate over frozen daily Parquet files. It requires every UTC day and checks
each manifest's file hash, row count, bounds and whole-row source-fidelity
fingerprint before aggregating only input availability. Its JSON output lists
each input file/hash and per-venue/day quality counts. It does not calculate a
signal, join outcomes or authorize a formal run. Run it in an isolated research
environment, for example:

```text
abnormal-flow-input-audit --cold-bars-dir <restored-cold-bars-dir> \
  --start-day <YYYY-MM-DD> --end-day <YYYY-MM-DD-exclusive>
```

## Implemented outcome-blind scanner (counts-only; returns reading disabled)

This module ships as a CALIBRATION / COUNTS-ONLY scanner. `abnormal_flow_replay`
holds it. `load_verified_minute_bars` verifies each day's cold-bar manifest (bytes,
sha256, identity/bounds, proven source fidelity) before reading any outcome-blind
row; `assemble_all` groups bars by native route AND `capture_version` (so a feature
window never spans a capture regime change) and builds per-instrument decisions with
the registered scan lag, execution window, and per-venue OI freshness. Canonical
identity is resolved point-in-time by an injected resolver (the caller supplies the
existing one); an unresolved identity is counted in the funnel, never treated as its
own ticker. A healthy no-trade minute (NULL trade-receive time on a trade-complete,
finalized bar) stays available; only a trade received after the decision is late. The
scanner output is the funnel of counts (scanned / eligible / primary and ablation
fires / episodes) by rejection reason.

Reading forward returns is HARD-DISABLED in this release: `FORMAL_RETURNS_RUN_ENABLED`
is `False` and `FormalReplay.run` raises `ReturnsRunDisabledError` unconditionally,
even for a fully frozen contract, before any freeze check or outcome read (covered by
test). The returns-path logic it guards (freeze fingerprint/window binding,
route-keyed priced-proxy outcomes, matched excess, week-clustered uncertainty,
leave-one-out, fixed-bank portfolio, and the one-shot verdict) is present and unit
tested but inert. Enabling it, a full registered input fingerprint, the OI ablation
economics, the portfolio, and the verdict are the NEXT PR, still before any returns
are read. The canonical resolver here is a documented heuristic fallback
(quote-suffix stripping) for tests; production passes the point-in-time resolver.

## Reproducible calibration scan (`abnormal-flow-scan`)

`abnormal_flow_scan` is the reproducible, outcome-blind counts/calibration command
(no scratch script). It inventories each day from a fixed start (2026-08-14, the first
fidelity-provable day; earlier days are `unverifiable_legacy`), verifying the manifest,
proven source fidelity, the specific Borg archive, and that the receipt is offsite, and
selects ONE continuous window from the start to the last FULLY VERIFIED day. It never
compares windows by signal count; a missing day, or a verified day after an unverified
one, records a gap and stops rather than silently narrowing. It then streams bars one
instrument at a time (bounded memory), resolves identity point-in-time from a supplied
snapshot, and writes an artifact under `docs/research/evidence/abnormal-flow-v1/<run-id>/`:
a README (exact command, revision, bounds), a manifest (per-day archive/hash/fidelity/
receipt + identity-snapshot hash), and `scan.json` (coverage by day/venue, rejection
reasons, feature distributions of OI growth / buy pressure / containment / participation
/ OI-notional, and counts of fires, independent episodes, assets and weeks). It reads no
forward price, PnL, or verdict. Raw Parquet is never committed.

```bash
abnormal-flow-scan --cold-bars-dir <restored> --provenance-dir <prov> \
  --borg-repo <borg-repo> --identity-snapshot <point-in-time-identity.json> \
  --contract-json <provisional-contract.json> \
  --start-day 2026-08-14 --end-day <last-fully-verified-day>
```

### Resource estimate and environment (run off live prod)

`timeseries.bybit_momentum_bars_1m` is a historical name for the SHARED bars table; it
holds BOTH venues, distinguished by the `exchange` column (the 2026-09-18 audit shows
Bybit and Binance), so this is a two-venue scan, not Bybit-only. Per-day/per-venue
counts come from the scan's own coverage (`per_venue_available_decisions` plus the
per-venue coverage rows); the estimate below is refined by that first run. Order of
magnitude: roughly 1.5M rows/day across both venues (the earlier one-day preflight saw
Bybit ~741,600 and Binance ~750,405), so 2026-08-14 .. 09-18 is on the order of 50M+
rows. The naive loader that builds one Python list of every bar would need tens of GB
of RAM and must not be run blind; the scan avoids this by streaming one instrument at a
time (each instrument-month is tens of MB), so peak RAM is a single instrument plus
DuckDB's out-of-core ordered scan. Recommended isolated environment: an EXISTING
non-prod host with the restored cold-bars if one is suitable (no new paid environment
yet), DuckDB local, on the order of 8 GB RAM and ~20 GB free disk for sort spill,
minutes to low tens of minutes of CPU. Do the first real run as a one-day slice to
measure actual RAM/disk/time and the real per-venue counts before the full window. The
unit test drives the whole path on a synthetic two-day dataset as a correctness proof.

## Frozen deterministic thresholds

Registered rule: `fixed_percentiles_on_prestart_window_v1`. Split the one
verified window into a CALIBRATION slice (the first 14 days) and a disjoint EVALUATION
remainder (2026-08-30T00:00Z to 2026-09-18T11:58Z). On the calibration slice only, over the ELIGIBLE decisions, freeze the three
thresholds at fixed pre-declared percentiles of the scan distributions: OI growth at the
P97.5, buy pressure at the P90, and containment at the P25 (a cap, so lower
is more restrained). Freeze the eligibility floor `min_oi_notional_usd` at the P25
percentile of calibration-slice OI-notional.

The inference rule is frozen as `student_t_df_weeks_minus_one_v1` to correct for the small number of weekly clusters.
The portfolio simulator enforces fixed-bank rules: sequential capital updates, exit-timing capacity limits, and $300 maximum allocation per slot. If any selected signal is unresolved, the verdict fail-closes.

The OI-ablation metric (same buy/price thresholds on same assets, just without the OI-growth requirement) acts as an outcome-blind funnel diagnostic. It does not gate the formal PASS/FAIL verdict, but records whether the OI filter actually isolated different flows.

The frozen artifact `contract.json` embeds its own hash and references the full `evaluation_manifest.json` fingerprint covering the candidate tables, identity snapshots, and raw funding records used.
The formal evaluation only supports
a single registered deterministic pass with strict portfolio gates and the registered verdict.

## Point-in-time identity schema (pinned; export + scan consume it)

Identity has TWO SEPARATE layers so a cross-venue guess can never inflate independent
evidence:

1. Per-route identity (authoritative). For each `(exchange, market_type,
native_market_id)` and each snapshot interval, an `identity_key` taken from THAT
   venue's `momentum_universe_instruments` snapshot. EVERY instrument gets one,
   including single-venue instruments. It is a per-route key, never a cross-venue
   cluster. Each record carries `valid_from` = snapshot `captured_at`, `valid_to` =
   the next snapshot's `captured_at` (or null), and `snapshot_captured_at` so the
   snapshot age at a decision is known. `valid_to` is an interval boundary only: it
   does NOT assert the universe was provably complete across a long gap between
   snapshots.
2. Cross-venue correspondence (separate, advisory). A separate table maps identity_keys
   across venues with the classifier status: `matched` / `candidate` / `conflict` /
   `unmatched`. Only `matched` is treated as the same underlying asset. `candidate`,
   `conflict`, and unknown are NEVER silently merged.

Export rules: emit only snapshots whose `captured_at <= decision time` (the resolver
picks the nearest snapshot at or before the decision; no look-ahead). The artifact
records per day/venue coverage and the snapshot age used. Between snapshots the gap can
be large; membership is "as of the last snapshot", and a decision far past its snapshot
is flagged by its snapshot age, not assumed complete.

Scanner counting when the cross-venue link is unknown:

- Episodes are formed per `(exchange, identity_key)` (per route), so Bybit ABC and
  Binance ABC are two separate episodes regardless of cross-venue status; the cooldown
  dedups only within a route.
- The independent asset count collapses ONLY `matched` cross-venue groups into one
  asset; every `candidate`/`conflict`/`unmatched` identity_key counts separately. The
  artifact reports `cross_venue_matched_groups`, `cross_venue_candidate`,
  `cross_venue_conflict`, and `cross_venue_unmatched` so the reader sees how much
  identity is uncertain. Cross-venue confirmed overlap is reported for portfolio
  concentration, never multiplied into independent evidence.

The export runs read-only ON PROD (localhost DB, not a PG tunnel) reusing the existing
`momentum_universe_identity_repository` (`window_coverage` / `instruments_as_of`) and
`momentum_universe_identity_classifier`; the small JSON is fetched over SSH. It is
unit-tested locally against a fake repository before the prod run.

## Formal returns run (v1)

**Verdict:** `INSUFFICIENT_EVIDENCE`

### Interpretation

The prospective formal run (2026-08-30 to 2026-09-18) failed to meet the rigorous promotion gates required for live trading, concluding with `INSUFFICIENT_EVIDENCE`.

*   The `mean_net_return` was weakly positive (+0.6%), but the `lower_95ci_net_return` was -1.7%, demonstrating a negative bound.
*   The `mean_excess_over_control` was +0.44%, but its lower 95% CI bound was -0.94%, falling significantly short of the >0% minimum.
*   The strategy took only 34 trades out of 252 primary episodes, with a total simulated portfolio PnL of $4.25 and a max drawdown of $17.14.

### Roadmap

Since the v1 formal evaluation did not pass, the hypothesis does not graduate to live execution. The required next steps are:
1. Preserve the artifact and the `INSUFFICIENT_EVIDENCE` outcome. No further adjustments to the v1 parameters or thresholds will be tested on this dataset (to avoid dataset burn).
2. Archive v1 and pivot back to the discovery phase for v2.
3. Next iteration (v2) must rely on a newly gathered validation cohort.
