# Token universe coverage v1

Status: implemented and unit-tested, including a real-Postgres integration
suite. Scoped down from ROADMAP item 7's original text
("`research/token-universe-identity-and-expansion-v1`") after discovering,
before writing any code, that the item's two headline pieces --
canonical on-chain/exchange identity instead of a bare ticker, and never
silently merging two distinct tokens that happen to share a symbol --
were already fully implemented and merged:
`feat/momentum-universe-identity-foundation-v1` (2026-08-15, see
[momentum-universe-identity-foundation-v1.md](momentum-universe-identity-foundation-v1.md))
and `feat/momentum-universe-identity-resolution-v1` (2026-08-17, see
[momentum-universe-identity-resolution-v1.md](momentum-universe-identity-resolution-v1.md)).
The resolution doc's own worked example ("`base=BTR` on two venues with
close onboarding dates is a candidate, not proof of the same asset") is
the exact scenario the review that motivated item 7 named (牛来 vs.
NIULAI). Nothing here reimplements either PR; this is a genuinely
narrower, additive PR against the one real gap those two left.

## Why this exists: what item 7's other two pieces actually needed

`momentum_universe_identity_repository.latest_ready_instruments` only ever
reads the LATEST `app.momentum_universe_snapshots` row per exchange.
Snapshots are written once per capture-process restart (see
`PersistCaptureStartupSnapshot`'s own doc comment) -- irregular, not a
fixed cadence. That leaves two questions item 7 needed answered
unanswerable with the code as it stood:

- **Point-in-time listing coverage.** "Was base X actively listed on
  exchange E at the time of historical pump episode Y" cannot be
  reconstructed from a read that only ever sees the current state.
- **Delisted assets and a non-survivorship-biased control group.** A base
  that stops being `ready` simply stops appearing in the latest snapshot
  -- there was no way to ask "every base that was ever a real candidate
  during this window, including ones no longer listed", which
  `research/serial-pump-regimes-v1` (ROADMAP item 8) needs to avoid
  building its denominator only from tokens that happened to still be
  listed when someone thought to look.

## What this PR adds, and what it deliberately does not

**Adds**, entirely as new reads against tables that already durably
accumulate history (no schema change, no migration, no new capture):

- `token_universe_coverage.py` (pure): `SeenInstrument`,
  `mark_currently_ready`, `delisted`, `AsOfCoverage`, `WindowCoverage`. No
  DB, no I/O -- deliberately separate from the repository, mirroring this
  codebase's own `momentum_universe_identity_classifier`/`_repository`
  split, so the parts of this that are easy to get subtly wrong are
  isolated and unit-tested with synthetic inputs.
- `MomentumUniverseIdentityRepository.instruments_as_of(exchange, as_of)`:
  the nearest snapshot at or before `as_of`, explicit about staleness
  (`AsOfCoverage.is_usable(max_staleness=...)`) rather than silently
  trusting a snapshot that may predate the requested instant by weeks.
  There is no codebase-wide default tolerance -- how stale is still "the
  same listing state" is a research-contract decision for whichever
  report calls this, not something this module assumes on a caller's
  behalf.
- `MomentumUniverseIdentityRepository.universe_seen_in_window(exchange,
start, end, *, max_carry_in_staleness)`: every distinct instrument LIFE
  (keyed by `identity_key`, not bare `native_market_id` -- see "Round 2"
  below) that was `identity_status='ready'` at some point COVERING
  `[start, end)`, not merely captured inside it -- an admissibly fresh
  carry-in snapshot (the nearest one at or before `start`, within
  `max_carry_in_staleness`) is included alongside anything actually
  captured inside the window, so a window with zero capture-process
  restarts inside it still reports the universe that was genuinely
  listed throughout. Returns `WindowCoverage`, whose own
  `has_reliable_coverage` a caller MUST check before trusting `seen` as
  complete. Every returned entry's own `currently_ready` starts `None`
  (not yet classified, and never confused with "confirmed absent" -- see
  "Round 2" below); a caller must explicitly cross-reference a single,
  independent `instruments_as_of("now")` call via `mark_currently_ready`
  to classify it and derive `delisted()` from it, so a window ending well
  before "now" can never have "now"'s own listing state leak into its
  answer by accident.
- `token_universe_coverage_report.py` / `make token-universe-coverage-
report` (`ARGS` must include `--window-start`/`--window-end`/
  `--max-staleness-days`, all required, no default): ad-hoc,
  re-run-by-hand report per exchange -- control-group size, currently-
  ready count, and the (never called "delisted" in this report's own
  output -- see "Round 2") absent-from-latest-snapshot list with
  first/last-seen timestamps, plus explicit carry-in/current-snapshot
  staleness and a combined `has_reliable_coverage` flag. Read-only:
  writes nothing, unlike `momentum-universe-identity-match`.

**Does not**: rebuild cross-venue matching (reuses `momentum_universe_
identity_classifier`/`_repository` as-is), add a periodic snapshot timer
(the sparsity this PR works around is a property of when
`momentum-capture`/`momentum-capture-binance` happen to restart, not
something this PR's own scope should change), or claim dense,
regularly-sampled point-in-time coverage -- `instruments_as_of`'s honest
answer for a stale or missing snapshot is the whole point, not a gap to
paper over.

## Round 2 (colleague review): two P1s in the exact denominator this PR exists to produce

- **Window query silently excluded the carry-in state.** The first
  version filtered snapshots to `captured_at` strictly INSIDE
  `[window_start, window_end)`. Since snapshots are only written on a
  capture-process restart, a window containing zero restarts returned an
  EMPTY universe even though hundreds of instruments were genuinely
  listed the entire time -- the exact opposite of what a non-
  survivorship-biased control group needs. `universe_seen_in_window` now
  includes the nearest snapshot at or before `window_start`, but ONLY
  when it is admissibly fresh (`max_carry_in_staleness`, a required,
  no-default parameter) -- a stale or missing carry-in is reported via
  `WindowCoverage.carry_in_snapshot_captured_at`/
  `carry_in_within_tolerance=False` for diagnosis, but excluded from
  `seen` rather than silently mixed into the universe as if it were still
  representative. Caught by a real-Postgres integration test built
  specifically to reproduce it
  (`test_universe_seen_in_window_carries_forward_state_before_window`) --
  it failed against the pre-fix code exactly as the review predicted.
- **Grouped by bare `native_market_id`, discarding lifecycle identity.**
  A market id delisted and later relisted under the same ticker gets a
  new `onboarded_at` and therefore a new `identity_key` -- migration
  0028's own docstring names this exact case as the reason `identity_key`
  exists at all. Grouping by `native_market_id` alone silently merged the
  two lives into one `SeenInstrument` and could mark the old, genuinely-
  gone life `currently_ready` purely because a new, unrelated listing
  reused its ticker. `SeenInstrument` now carries `identity_key`;
  `universe_seen_in_window` groups by it, and `mark_currently_ready`
  compares by it -- the same key the rest of this codebase's identity
  system already uses. Caught the same way, by
  `test_universe_seen_in_window_distinguishes_relisted_identity` failing
  against the pre-fix grouping.

A P2 in the same round: `currently_ready` was a plain `bool` defaulting to
`False`, making "not yet classified" indistinguishable from "confirmed
absent" -- `delisted()` called before `mark_currently_ready` silently
returned every entry, directly contradicting this module's own docstring
claim that this could not happen, and the unit test written to prove it
actually called `mark_currently_ready` first, so the claim was never
really exercised. `currently_ready` is now `bool | None` (`None` =
unclassified); `delisted()` raises `ValueError` if asked to classify
anything still `None`, and the CLI report never uses the word "delisted"
in its own JSON output -- `absent_from_latest_ready_snapshot` is the
honestly-scoped name, since absence alone cannot distinguish genuine
delisting from a rename or a stale/incomplete current snapshot (see
`token_universe_coverage.delisted`'s own doc comment).

## Round 3 (colleague review): three P2s

- **Report classified against an unusable current snapshot.** If
  `instruments_as_of("now")` found no snapshot at all (or a stale one),
  its own `identity_keys` was empty -- `mark_currently_ready`/`delisted()`
  would then mark the ENTIRE historical control group absent, fabricating
  a full-universe delisting the adjacent `current_snapshot_within_
tolerance=False` flag did not prevent a reader from trusting (the
  fabricated counts/list sat right next to it in the same JSON object).
  Fixed: `token_universe_coverage_report.py` now skips classification
  entirely when `current.is_usable(...)` is false, reporting
  `currently_ready_count`/`absent_from_latest_ready_snapshot`/
  `absent_from_latest_ready_snapshot_count` as JSON `null` and an explicit
  `classification_status: "insufficient_data_no_usable_current_snapshot"`
  instead.
- **No bounds validation.** Neither `universe_seen_in_window` nor its
  CLI checked `window_start < window_end` or a non-negative staleness.
  Since `window_end` is never consulted when deciding the carry-in, an
  empty or reversed window could still return a non-empty `seen` purely
  from a valid carry-in -- a denominator for an interval that does not
  exist. Fixed: `universe_seen_in_window`/`instruments_as_of` now raise
  `ValueError`, fail-fast (before ever touching the database), on a naive
  (non-timezone-aware) timestamp, `window_start >= window_end`, or a
  negative `max_carry_in_staleness`; the CLI's own `--max-staleness-days`
  rejects a negative value at argument-parsing time.
- **New integration suite always skipped on an unreachable Postgres,**
  unlike this package's actual established convention (e.g.
  `test_pump_recurrence_integrity_repository_integration.py`'s own
  `_connect_or_skip`): CI sets `REQUIRE_INTEGRATION_DB=1` specifically so
  a broken/unprovisioned Postgres service fails the build loudly instead
  of these new SQL-correctness tests silently skipping and the run still
  going green. Fixed to follow that convention, with its own enforcement
  test (a monkeypatched failing engine) proving the raise actually fires.

## Verified

- `test_token_universe_coverage.py` (10 tests): pure
  `mark_currently_ready`/`delisted`/`AsOfCoverage`/`WindowCoverage`
  behavior -- including that matching is by `identity_key` (a relisted
  market id under the same ticker is never conflated with its
  predecessor), that `delisted()` raises on unclassified entries instead
  of guessing, and that `is_usable`'s staleness boundary is inclusive.
- `test_momentum_universe_coverage_repository_integration.py` (9 tests):
  real Postgres, seeds multiple snapshots per exchange spread over time,
  runs under `REQUIRE_INTEGRATION_DB=1` (matching CI). Directly proves the
  round-2 fixes against a real database, not just structurally: a window
  with no in-window restart still reports the carried-forward universe
  when the carry-in is admissibly fresh, reports it as unreliable (and
  excludes the stale carry-in from `seen` entirely) when it is not, and a
  relisted market id under the same ticker produces two separate
  `identity_key`-keyed entries with the old one correctly classified
  absent once the new one is live. Round-3 additions: fail-fast
  `ValueError` on a reversed/empty window, a negative
  `max_carry_in_staleness`, and a naive timestamp on either method; the
  `REQUIRE_INTEGRATION_DB=1` enforcement path itself, via a monkeypatched
  failing engine.
- A local sanity run of `make token-universe-coverage-report` was not
  meaningful to include here: this repo's local dev Postgres has
  essentially no real `momentum_universe_snapshots` history (only
  `momentum-capture`/`-binance` running continuously in production
  accumulate it via periodic restarts) -- correctness is established by
  the integration tests above, against the real schema, not by a
  local demo. A local smoke run against this near-empty data honestly
  reported `classification_status: "insufficient_data_no_usable_current_
snapshot"` and `has_reliable_coverage: false` for both exchanges rather
  than fabricating a result; a smoke run with `--max-staleness-days=-1`
  was correctly rejected by the CLI's own argument parser. Real
  control-group/absent numbers are available via `make prod-token-
universe-coverage-report` once this is deployed.

## What research/serial-pump-regimes-v1 inherits

`universe_seen_in_window` plus `mark_currently_ready`/`delisted` are the
sanctioned way to build a non-survivorship-biased candidate denominator
for a historical window: every instrument life that was really listed at
some point in that window, not only the ones that still happen to be
listed today or that `app.pump_events` happened to record. A caller MUST
check `WindowCoverage.has_reliable_coverage` before trusting the result --
an unreliable window (no admissibly fresh carry-in) should be treated as
`insufficient_data`, not a real zero. `instruments_as_of` is the
sanctioned way to check whether a specific historical episode's own asset
was actually listed on its source/target venue at that instant, with an
explicit, caller-chosen staleness tolerance rather than an assumed one.
