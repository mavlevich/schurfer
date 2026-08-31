# Serial pump regimes v1

Status: implemented and unit-tested (77 pure-function/report-layer tests,
including monkeypatched orchestration tests for `run()`'s own error
handling), verified end-to-end against real local dev data via `make
serial-pump-regimes-report`. Discovery-only, no verdict. Two colleague-
review rounds (2026-09-01) found five P1s each, all fixed below. ROADMAP
item 8.

## What item 8 asked for, and what already existed for it

Item 8's text: "What to do after a first pump on a given asset -- hold or
sell -- using every radar episode (not only ones that went on to "win"),
recurrence count and inter-episode intervals, venue expansion, BTC/market-
adjusted return, `15m`/`1h`/`4h`/`1d`/`7d`/`30d` horizons, MFE/MAE/time-to-
peak/retrace/delisting. The historical window stays discovery-only; any
confirmation runs on a new, untouched forward cutoff, never on the window
already viewed here."

Before writing any code, a read of `pump_recurrence_integrity_report.py`/
`_repository.py` found that "every radar episode, recurrence count and
inter-episode intervals" -- the harder, already-review-hardened half of
item 8 -- was already built and merged: its own `Episode`/`Regime`/
`merge_episodes_into_regimes` collapse detector-flapping reopens into one
independent price regime per real event, using a 24h cooldown that had
already been through colleague review for exactly the overlapping/nested-
episode edge cases this item would otherwise have had to re-litigate. This
PR reuses that mechanism directly (`PumpRecurrenceIntegrityRepository.
load()` in one `REPEATABLE READ` transaction) rather than reimplementing
episode-to-regime merging a second time.

What did not exist yet: a forward-outcome read from each regime's own
decision point (BTC-adjusted return, MFE/MAE/time-to-peak/retrace, across
six horizons) and venue-expansion evidence per regime. Those are this PR's
actual new code -- and where the first colleague-review round found real
problems (see below).

## What this is and is not

**Is** a pure discovery report over every independent pump regime ever
detected: what actually happened next, across the full historical
population, not filtered to regimes that later "won". No formal
checkpoint, evidence floor, or promotion rule -- unlike this codebase's
registered prospective contracts (e.g. `source_lead_forward_cohort.py`).
This was an explicit choice, asked and confirmed directly rather than
defaulted. Any future confirmation step runs on a new, untouched forward
cutoff -- never on the historical window this report reads.

**Is not** a live-decision-path component, and adds no new capture or
schema. Read-only against `app.pump_events`/`app.pump_event_sources` (via
the reused repository) and `app.momentum_universe_snapshots` (via
`instruments_as_of`, for venue expansion); OHLCV is fetched live per run
via `ohlcv.fetch_symbol_candles(..., use_cache=True)`, backed by the
existing `market_path_cache.py` -- not a new frozen Parquet dataset.

## Round 1 (colleague review, 2026-09-01): five P1s

### 1. Decision instant anchored to the LAST episode, not the first

`decision_boundary_ms` originally used `regime.last_seen_at`. A regime's
own `last_seen_at` is a running maximum: `merge_episodes_into_regimes`
merges any future episode starting within the cooldown of it into the
SAME regime, extending `last_seen_at` forward. So the decision instant
was only knowable once the cooldown had fully elapsed with no further
episode -- exactly the "known only in hindsight" look-ahead item 8's own
"never on the window already viewed here" line warns against, and it
answered "what to do after the LAST episode of a fully-formed regime",
not item 8's own "after a FIRST pump". An open regime's own decision
point (and every forward-outcome number derived from it) could also
silently shift between two runs if one more episode merged in between
them.

Fixed: `decision_boundary_ms` now anchors to `regime.first_seen_at`, which
`merge_episodes_into_regimes` sets once from the regime's own first
episode and never revises on a later merge (`first` is only reassigned
when a NEW regime starts, never inside the "merge into this one" branch)
-- knowable the instant the first episode is detected, with zero
dependency on what happens afterward. The cooldown itself is unchanged,
still used purely for `merge_episodes_into_regimes`'s own deduplication
(collapsing detector-flapping reopens into one regime identity) -- never
as part of the decision instant. `RegimeRow.regime_mature` (true once
`evaluation_at` is at least one cooldown past `regime.last_seen_at`)
surfaces whether a regime could still gain another merged episode on a
later run -- its own `episode_ids`/`last_seen_at`/`max_peak_pct`/
recurrence numbers could still change, but never its `decision_at` or
forward-outcome numbers, since neither depends on `last_seen_at`.
Verified directly: `test_decision_boundary_ignores_last_seen_at_entirely`
constructs two regimes sharing `first_seen_at` but wildly different
`last_seen_at` and asserts an identical boundary.

### 2. Entry price read the boundary candle's own CLOSE -- a look-ahead

`_decision_price` (now `_entry_price`) took the CLOSE of the first candle
at/after the decision boundary. That close is only known once the candle
itself finishes -- one full timeframe (5 minutes) after the boundary --
so the report was using a price not yet known at the moment it claimed to
be entering. Concretely: a nominal 15-minute horizon only had 10 minutes
of genuinely forward-looking coverage once the entry price itself is
knowable.

Fixed: `_entry_price` now reads that candle's own OPEN, set at the
candle's start instant and therefore known immediately at the boundary,
with no delay. Because entry now happens at the open of that same candle,
its own high/low legitimately belong to the trade's exposure from that
instant forward, so it correctly stays inside the MFE/MAE scan (unlike
before, where a close-based entry meant that candle's own intra-bar
excursion technically preceded the "entry"). Verified directly:
`test_resolve_horizon_outcome_entry_uses_open_not_close` sets a boundary
candle with `open=1.0, close=1.20` and confirms the forward return is
computed from 1.0, not 1.20.

### 3. Canonical identity replaced by a bare ticker

Three compounding issues: regimes were grouped by bare `base` (inherited,
accepted behavior of the reused `merge_episodes_into_regimes` -- not
itself the bug); the exchange pick (`_pick_exchange`) collected
`SourceIdentityObservation.exchange` values into a set without checking
`identity_conflict` or agreement across a regime's own episodes; and
`_process_regime` fetched OHLCV via `ohlcv.fetch_candles(client, base,
...)`, which reconstructs `f"{base.upper()}/USDT:USDT"` from the bare
ticker. This is exactly the class of bug this codebase's identity
foundation/resolution PRs and the recurrence-integrity audit exist to
prevent: a relisted or ticker-colliding instrument could silently have
its own OHLCV path built from a different contract than the one that was
actually observed.

Fixed with `_resolve_regime_identities`: for a regime's own `episode_ids`,
every `SourceIdentityObservation` on a given exchange must agree on one
`identity_key` and one `unified_symbol`, with no `identity_conflict` flag
set anywhere in that group -- otherwise that exchange is treated as
having no usable identity for this regime at all (`ambiguous_identity`),
never a guess at which one is right. `_pick_ohlcv_identity` only ever
hands `_process_regime` an already-disambiguated `ResolvedIdentity`, whose
own `unified_symbol` goes straight into `ohlcv.fetch_symbol_candles` (the
identity-safe sibling of `fetch_candles`, already built for exactly this
purpose and used elsewhere in this codebase, e.g.
`derivatives_context.py`) -- no ticker reconstruction anywhere in this
module. Venue expansion (`_venue_expansion`) uses the same resolved
`identity_key`, matched against `AsOfCoverage.identity_keys`, not a
reconstructed `base.upper() + "USDT"` against `native_market_ids`.
`EXCHANGE_OHLCV_PRIORITY` is now explicitly disclosed as a simplification,
not a literal mirror of `apps/api-gateway/internal/pumps/handler.go`'s own
`ohlcvPriority`: the Go side sorts primarily by each exchange's own live
`volume_24h_usd`, using its own priority list only as a tie-breaker;
fetching a fresh volume ranking per regime here would mean an extra live
API call per candidate exchange per regime, out of proportion for a
discovery report -- this module uses the static priority list alone.
Verified directly: `_resolve_regime_identities`'s own tests cover a
conflicting `identity_key` across two episodes of the same merged regime,
an `identity_conflict` flag, and a missing `unified_symbol`, all
correctly falling back to `None`; `test_run_uses_canonical_unified_symbol_
not_a_reconstructed_ticker` runs the full `run()` orchestration with a
base whose own recorded `unified_symbol` deliberately differs from what a
ticker reconstruction would produce, and asserts the fetch used the
recorded symbol.

### 4. A leading or internal candle gap still resolved

The horizon-resolution check only verified the LAST candle in the window
reached far enough (`window[-1].ts_ms + timeframe_ms >= horizon_end_ms`).
`ohlcv.fetch_symbol_candles` can return a partial result -- a leading gap
(the series starts after the requested boundary) or a bar silently
missing from the middle -- without raising (its own docstring says so
explicitly); the tail-only check could not see either, so a return/MFE/
MAE built on an incomplete path could silently resolve as if the whole
path were real.

Fixed: `resolve_horizon_outcome` now calls `ohlcv.covers_window_without_
gaps` (made public -- it already existed as `fetch_symbol_candles`'s own
private cache-eligibility check, with exactly this "is this the exact,
gapless bar sequence" semantics; reused rather than reimplementing a
second, independently-written gap check that could silently drift from
it) against both the target and BTC windows, with the leading case
(`window[0].ts_ms != boundary_ms`) checked separately from the general
gaplessness scan so each gets a distinct, honestly-named reason
(`leading_candle_gap`/`internal_candle_gap`, and the BTC-prefixed
equivalents) rather than one merged one. Verified directly with four new
tests, each constructing a specific gap shape (an internal hole, a late-
starting series, and the BTC-side equivalents of both) and asserting the
correct distinct reason.

### 5. Venue expansion could read the future as already known

`_venue_expansion` computed `after_at = boundary + 30 days` and called
`instruments_as_of(exchange, after_at)` unconditionally. `instruments_as_
of`'s own semantics are "the nearest snapshot at or before `as_of`" -- for
a regime less than 30 days old, `after_at` is itself still in the future,
so this call would return TODAY's current snapshot and, since `is_usable`
only checks `(as_of - snapshot_captured_at) <= max_staleness`, a
same-day snapshot against a 10-days-in-the-future `after_at` reads as
comfortably "fresh" -- silently presenting a not-yet-determined future
outcome as if it were already known.

Fixed: `run()` now takes an explicit `evaluation_at` parameter (defaulting
to `datetime.now(UTC)`, computed once and reused everywhere in that run,
including as `generated_at`, rather than re-derived at multiple points).
`_venue_expansion` computes `after_at_matured = after_at <= evaluation_at`
and only queries `instruments_as_of` for the "after" side when that is
true; otherwise `ready_after` stays `None` unconditionally and
`VenueExpansionEntry.after_at_matured=False` makes the reason explicit,
rather than merging with the pre-existing "no admissibly-fresh snapshot"
`None` case. Also fixed in the same pass: the readiness check itself used
to be `base.upper() + "USDT" in coverage.native_market_ids` (a
reconstructed ticker, the same class of issue as fix 3 above) -- now uses
the regime's own resolved `identity_key` against `coverage.identity_keys`,
and reports `None` (never guesses) when this regime has no resolved
identity on that exchange at all. Verified directly:
`test_venue_expansion_entry_expanded_none_when_after_at_not_matured`, and
confirmed live against real prod-like data (see "Verified" below) -- every
regime younger than 30 days now correctly reports `after_at_matured:
false` instead of a confident `ready_after`.

## Round 1 (colleague review): two P2s

- **One regime's OHLCV fetch failure lost the whole run.** `asyncio.
gather(*tasks)` with no per-task isolation meant a single exchange
  timeout or error anywhere in an unbounded, potentially thousands-of-
  regimes run would propagate straight through and discard every other
  regime's already-completed work. Fixed: the network-touching section of
  `_process_regime` (candle fetch + venue expansion) is now wrapped in its
  own `try`/`except Exception`, converting a failure into an explicit
  `ohlcv_fetch_failed` `RegimeRow` for that one regime rather than
  aborting the batch. `--concurrency 0` (or any non-positive value) used
  to construct `asyncio.Semaphore(0)`, which blocks every `acquire()`
  forever -- `run()` now raises `ValueError` before ever constructing the
  semaphore, and `--concurrency`'s own CLI parsing rejects a non-positive
  value at argument-parsing time (`_positive_int`, mirroring `token_
universe_coverage_report.py`'s own `_parse_nonnegative_days`
  convention). Verified directly: `test_run_isolates_one_regimes_ohlcv_
failure_from_the_rest` runs `run()` against two synthetic regimes (one
  wired to raise inside the fetch, one to succeed) with monkeypatched
  repositories/clients -- no real DB or network -- and asserts both rows
  come back, the failing one correctly marked, cleanup still ran for
  everything; `test_run_rejects_nonpositive_concurrency` covers the
  `concurrency=0` case.
- **Reproducibility metadata was incomplete.** `make serial-pump-regimes-
report`/`make prod-serial-pump-regimes-report` never passed `--code-
revision`/`--working-tree-dirty` (unlike the sibling `token-universe-
coverage-report` targets), so every report generated via `make` silently
  carried `code_revision="unknown"`/`working_tree_dirty=false` regardless
  of the real state. Fixed: both Makefile targets now mirror `token-
universe-coverage-report`'s own `--code-revision="$(git rev-parse
HEAD)"` / dirty-check pattern (`--working-tree-dirty` switched from a
  plain `store_true` flag to `argparse.BooleanOptionalAction` to accept
  the Makefile's own `--no-working-tree-dirty`/`--working-tree-dirty`
  pair). Separately, `input_fingerprint` only hashed `episodes`, not the
  `identity_observations` that also determine exchange/symbol choice -- an
  unchanged episode set with a changed `identity_key` would silently keep
  the same fingerprint. Fixed: `compute_input_fingerprint` now takes both
  and hashes both. Disclosed, not claimed fixed: forward-outcome NUMBERS
  still additionally depend on live OHLCV and `momentum_universe_
snapshots`' own state at run time, neither of which a fingerprint
  computed before any fetch happens could sensibly cover -- an identical
  `input_fingerprint` proves the same regime population and identity
  resolution, not byte-identical forward numbers on a re-run.

A P2 raised alongside these, not directly actionable without a larger
scope increase: `retrace_from_peak_pct`'s own docstring claimed to
"match" `app.pump_events.retrace_pct`'s sign convention
(`last_pct - peak_pct`, always <= 0) while actually computing the
opposite-signed `mfe_pct - forward_return_pct` (always >= 0). The
magnitude is the same underlying gap; only the sign claim was wrong.
Fixed by renaming the field to `retrace_magnitude_pct` and rewriting the
docstring to state plainly that this is a positive magnitude, deliberately
not signed the same way the DB column is -- correcting the claim rather
than inverting the value (a positive "how much was given back" magnitude
reads more naturally in a report than a signed value chosen to match an
unrelated table's own convention).

## Round 2 (colleague review, 2026-09-01): five more P1s

Round 1's own identity/venue-expansion fixes introduced or left in place
five more real defects, all in the identity/venue-expansion machinery
round 1 itself just built.

### 1. Identity resolution could use a FUTURE episode's own observation

Round 1 anchored `decision_at` to `regime.first_seen_at` (fix 1 of round

1. but `_resolve_regime_identities` still ran over the regime's own FULL
   `episode_ids` -- including episodes `merge_episodes_into_regimes` merges
   in much later, still within the same regime's cooldown, well after
   `decision_at` already passed. A later episode's own first-ever identity
   observation on some exchange would then get used to pick THIS regime's
   OHLCV exchange for a decision instant that predates that observation
   entirely -- a "future-known route selection" look-ahead, and one round 1's
   own anchor-to-first_seen_at fix made structurally more likely to bite
   (previously, anchoring to `last_seen_at` meant virtually every episode's
   observation necessarily preceded or coincided with the decision instant).

Fixed: `_available_identity_episode_ids` restricts identity resolution to
episodes whose own `first_seen_at` is at or before the regime's decision
boundary; `regime.episode_ids` itself (recurrence, display) stays the full
set. Verified directly:
`test_run_does_not_use_future_episode_identity_for_ohlcv_pick` runs the
full `run()` orchestration with an episode 2 hours after episode 1 (merged
into the same regime) carrying the ONLY identity observation, and asserts
the regime resolves `no_identity_observation` rather than picking it up.

### 2. Resolver silently dropped an incomplete observation instead of failing closed

`_resolve_regime_identities` built `identity_keys = {o.identity_key for o
in observations if o.identity_key is not None}` -- a group
`[identity_key='k1', identity_key=None]` collapsed to a single-element
`{"k1"}` set, i.e. "successfully resolved", when the `None` entry is
itself evidence of an incomplete observation that should make the whole
exchange ambiguous. Separately, `base_asset` was never checked at all,
even though `pump_recurrence_integrity_report.identity_reason` (already
built, already colleague-review-hardened) classifies a `base`/`base_asset`
mismatch as `base_mismatch` -- exactly the alias-collision signal that
check exists to catch.

Fixed by reusing `identity_reason` directly instead of a second, weaker,
independently-written check: an exchange's observations are only usable if
EVERY one of them individually satisfies `identity_reason(o) is None`
before the identity*key/unified_symbol agreement check even runs. Verified
directly: `test_resolve_regime_identities_mixed_present_and_none_identity*
key*is_ambiguous`, `test_resolve_regime_identities_base_mismatch_is*
ambiguous`.

### 3. Recurrence still counted purely by ticker

Regimes are grouped and merged by bare `base` (an accepted characteristic
of the reused `merge_episodes_into_regimes` -- not itself something this
report should silently override, since it is shared with `pump_
recurrence_integrity_report.py` and already went through its own review).
But `recurrence_summary`'s own `regime_index`/`regime_count_so_far`/
`next_regime_gap_minutes` were then presented as if consecutive same-base
regimes are CONFIRMED to be one real-world asset recurring -- when a
genuine delisting-and-relisting under the same ticker (the exact "牛来 vs
NIULAI"-class case this codebase's identity system exists to guard
against) would be silently counted as one asset's own recurrence history.

Rather than rearchitecting regime formation itself (out of scope -- would
mean changing the shared function's own semantics), `_identity_overlap`
adds a disclosure overlay: `RegimeRow.next_regime_same_asset` is `True`
only when this regime and the next one share an exchange with a matching
resolved `identity_key` (positive confirmation), `False` when they share
an exchange with a DIFFERENT `identity_key` (positive evidence of a
ticker collision), `None` when there is no comparable exchange either way.
A reader can now tell "confirmed same asset" apart from "same ticker,
identity unconfirmed or refuted" per recurrence link, instead of every
same-base regime being presented with equal, unstated confidence. Verified
directly: `test_run_marks_next_regime_same_asset_true_when_identity_
confirmed`, `test_run_marks_next_regime_same_asset_false_on_identity_
mismatch`.

### 4. Venue expansion structurally excluded the exact case it exists to detect

`_venue_expansion` only checked bybit/binance readiness when this regime
already had a resolved identity FROM ITS OWN PUMP-DETECTION SOURCES on
that exact exchange. But a base that genuinely expands to a new venue for
the first time has, BY DEFINITION, no pump ever detected there yet --
no source-derived identity_key for that exchange can exist. The one case
"venue expansion" exists to detect was therefore always reported `None`/
`None`, no evidence either way, defeating the feature's own purpose.

Building a full canonical cross-venue bridge (`momentum_universe_asset_
clusters`/`cluster_members`, the confirmed-cluster mapping from `feat/
momentum-universe-identity-resolution-v1`) would be the fullest fix, but
that repository currently has no READ method for clusters at all (only
`persist_clusters`) -- a new repository method plus its own real-schema
integration tests, out of proportion for this round. Fixed instead with a
disclosed two-tier match: `_venue_ready` prefers the canonical
`identity_key` match when this regime has one; when it does not, it falls
back to a reconstructed `base.upper() + "USDT"` against `AsOfCoverage.
native_market_ids` -- the exact ticker-based check round 1 removed, now
reintroduced ONLY as an explicit, disclosed fallback for exactly the case
identity*key cannot cover. `VenueExpansionEntry.match_basis` (`"identity*
key"`or`"ticker*fallback"`) is reported on every entry so a reader can
weight a ticker_fallback-based result against its own disclosed residual
collision risk, never silently treat it as identity-safe. Verified
directly: `test_venue_ready_prefers_identity_key_when_available`, `test*
venue_ready_falls_back_to_ticker_when_no_source_identity`.

### 5. Cache-integrity errors were swallowed; a venue-expansion failure discarded good horizons

Two compounding issues in the same `except Exception` block: (a) it caught
`MarketPathCacheCorruptError`/`MarketPathCacheWriteError` from `ohlcv.py`'s
own cache module, even though that module's own docstrings explicitly
require both to fail loudly -- a corrupt cache entry or a failed write is
a systemic infra problem, likely to recur identically across many other
regimes in the same run, not per-regime noise a generic `ohlcv_fetch_
failed` label should quietly absorb; (b) the same block wrapped BOTH the
OHLCV fetch AND the venue-expansion DB read together, so a venue-
expansion-only failure (a transient DB hiccup unrelated to OHLCV, which
had already succeeded) discarded the just-computed horizon outcomes too,
and mislabeled the row `ohlcv_fetch_failed` when the OHLCV fetch itself
was fine.

Fixed: `MarketPathCacheCorruptError`/`MarketPathCacheWriteError` are now
caught first and immediately re-raised, propagating out of `run()`'s own
`asyncio.gather` and aborting the whole report -- the fail-loud behavior
those exceptions' own docstrings require, deliberately NOT the same
per-regime isolation an ordinary network failure gets. Venue expansion
(and the new `delisted` check, which reuses the same repository call) now
has its OWN, separate `try`/`except`, downstream of the horizons already
being computed -- a failure there sets `venue_expansion_unresolved_
reason="venue_expansion_failed"` and leaves `horizons`/`ohlcv_unresolved_
reason` untouched. Verified directly: `test_run_lets_cache_integrity_
errors_propagate`, `test_run_isolates_venue_expansion_failure_from_
already_computed_horizons`.

## Round 2 (colleague review): three more P2s

- **Markdown hid almost everything the report actually computes.** The
  default `--format markdown` output showed only per-horizon population
  medians -- no unresolved-reason counts, no recurrence, no inter-regime
  gaps, no venue expansion, no time-to-peak, no retrace, no delisting
  (item 8's own named metrics). Fixed: `render_markdown` now also renders
  a `## Regimes` table (one row per regime: base, episode count,
  decision_at, maturity, OHLCV source, recurrence position, next-regime
  gap, `next_regime_same_asset`, `delisted`, and a compact per-exchange
  venue-expansion summary including `match_basis`) and a `## Horizon
detail` table (one row per resolved regime x horizon: return, BTC-adj,
  MFE, MAE, time-to-peak, retrace magnitude), plus per-horizon unresolved-
  reason counts in the population table. The JSON format remains the
  complete, canonical record; markdown is now a much closer proxy rather
  than a near-empty summary.
- **`input_fingerprint` ignored the `--base` filter for identity
  observations.** `episodes` was correctly narrowed by `filters.bases`,
  but `identity_observations` stayed unfiltered -- a bounded run's own
  fingerprint kept changing whenever ANY unrelated base's identity
  observations changed, defeating "same restricted input -> same
  fingerprint". Fixed: `identity_observations` is now narrowed to exactly
  the retained episodes' own `event_id`s in the same filtering step.
  Verified directly: `test_run_fingerprint_respects_base_filter`.
- **"Hold or sell" read as a stronger economic claim than the numbers
  support.** Every return/MFE/MAE figure here is a raw, GROSS OHLCV price
  move -- no spread, fees, slippage, or funding -- and for a 15-minute
  horizon in particular, gross and after-cost can differ by more than the
  whole median return shown. Fixed: the module docstring, the CLI's own
  `--help` description, and the rendered markdown header now all state
  this explicitly, and the module docstring names the concrete follow-up
  (`packages/performance`'s `calculate_performance`/`CostParameters`,
  already used by `source_lead_forward_cohort.py`'s own registered
  contract) required before any number here is read as a hold/sell
  recommendation. Verified directly:
  `test_render_markdown_includes_gross_returns_disclaimer`.

## Reuses, rather than reimplements

- `pump_recurrence_integrity_report`/`_repository`'s own `Episode`/
  `Regime`/`merge_episodes_into_regimes`/`SourceIdentityObservation`,
  `identity_reason`, and `PumpRecurrenceIntegrityRepository.load()`.
- `token_universe_coverage_v1`'s own `MomentumUniverseIdentityRepository.
instruments_as_of(exchange, as_of)` for venue expansion. Deliberately
  **not** `universe_seen_in_window` -- that method builds a non-
  survivorship-biased denominator across the _whole_ universe for a
  window; this report needs a two-instant point check per regime, and
  each regime already has its own denominator (`app.pump_events`).
- `ohlcv.py`'s shared `fetch_symbol_candles`/`ceil_to_timeframe`/
  `closed_candles`/`covers_window_without_gaps` (the last made public in
  this PR -- see fix 4 above).
- `exchange_registry.EXCHANGE_FACTORIES` for OHLCV-capable CCXT clients,
  one opened per exchange per run (not per regime) and closed once in a
  `finally` block -- the same client-lifecycle pattern
  `derivatives_context.py` already uses.
- `reporting.py`'s shared `json_ready`/`markdown_table`/
  `normalize_code_revision`/`parse_utc_datetime`.

## A real bug caught by the live smoke run, not by review

`render_json` originally called `json.dumps(json_ready(report), ...,
default=str)` directly on the dataclass instance. `reporting.json_ready`
only recurses into `dict`/`list`/`tuple` (plus a `datetime` base case) --
it does not know about dataclasses at all, so it passed the whole
`SerialPumpRegimesReport` straight through unchanged, and `json.dumps`
fell back to `default=str`, stringifying the **entire report** via its
own `repr()` into one giant JSON string literal. Invisible to `mypy`/
`ruff`/the unit tests written before the smoke run (none of them parsed
the rendered output and inspected a field); only surfaced when the CLI
was run against real data and the output piped through `json.load`.
Fixed to match `pump_recurrence_integrity_report.py`'s own established
convention: `json.dumps(json_ready(asdict(report)), ...)`. Two regression
tests parse the rendered string back with `json.loads` and assert on
individual fields.

## Verified

- 77 tests across `test_serial_pump_regimes.py` (21) and `test_serial_
pump_regimes_report.py` (56), including the fix-specific tests named in
  each Round 1 and Round 2 item above. Round 2 added seven `run()`-level
  orchestration tests against monkeypatched fakes (no real DB or network
  required): cache-integrity propagation, venue-expansion-failure
  isolation, `--base`-filtered fingerprint, future-episode-identity
  exclusion, `next_regime_same_asset` confirmed/refuted, and `delisted`.
- `ruff check` / `ruff format --check` / scoped `mypy` clean on every
  touched module and test file (including `ohlcv.py`, for the
  `covers_window_without_gaps` rename).
- Full `apps/analytics/tests` suite green after these additions.
- Live smoke runs against real local dev Postgres + live CCXT, re-run
  after both rounds: `CYS` (3 regimes, 13 merged episodes) shows
  `decision_at` anchored to each regime's own `first_seen_at`, `ohlcv_
  symbol: "CYS/USDT:USDT"` explicit in the output, `next_regime_same_
  asset: true` between its own consecutive regimes (confirmed via a
  shared binance `identity_key`), `delisted: true` (independently
  confirmed by querying `instruments_as_of("binance", now)` directly --
  CYS's own identity_key is genuinely absent from binance's current ready
  snapshot), and `venue_expansion.after_at_matured: false` for every
  regime (all younger than 30 days as of this run). `make serial-pump-
regimes-report ARGS="--base CYS --format markdown"` confirmed the real
  git revision/dirty-tree state, the new `## Regimes`/`## Horizon detail`
  sections, and the GROSS-returns disclaimer all render correctly.
- `make -n serial-pump-regimes-report ARGS="..."` / `make -n prod-serial-
pump-regimes-report` dry-run syntax check (unchanged this round).
