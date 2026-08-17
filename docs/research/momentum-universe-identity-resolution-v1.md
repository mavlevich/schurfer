# Momentum universe identity resolution v1

Status: implemented and unit-tested, including a real-Postgres integration
test and a live upgrade/downgrade/upgrade migration cycle, plus a sanity
run against real prod data (2026-08-17: 463 clusters from 516 Bybit / 525
Binance ready instruments). PR 9 of the 30-PR roadmap (resolution half of
"cross-venue identity"), following `feat/momentum-universe-identity-
foundation-v1` (PR 8). See that doc's own "What a future resolution PR
inherits" section, which specified this PR's target vocabulary and gave
the worked example this PR's own classifier is built to match, before any
of this code existed.

## What this PR is and is not

**Is**: a pure classifier
(`schurfer_analytics.momentum_universe_identity_classifier`) that reads
the latest ready-instrument snapshot from every captured exchange, groups
instruments by `(base, canonical_market_type)`, and assigns each group
member its own `match_status` -- plus a repository/CLI pair
(`momentum_universe_identity_repository.py`,
`momentum_universe_identity_matcher.py`, `make momentum-universe-identity-
match`) that persists the result into two new tables.

**Is not** a live-decision-path component. This does not touch trade
execution, `momentum_flow_watch`, or paper trading. It is an ad-hoc,
re-run-by-hand report (like `oi-growth-filter-report` or `gate-identity-
candidate-tooling`), not a persistent worker -- see the matcher's own doc
comment for why a systemd timer would be premature right now (the
upstream snapshot data itself only refreshes on a capture-process
restart, not periodically).

## Venue-count-agnostic by design, not Bybit/Binance-specific

The obvious naive schema -- two columns, `bybit_identity_key` and
`binance_identity_key` -- stops working the moment a third venue is
added. This PR uses a cluster model instead:
`app.momentum_universe_asset_clusters` (one row per real-world asset a
matching run identifies) and `app.momentum_universe_cluster_members` (N
rows under it, one per venue's own instrument judged to belong to that
asset). `classify()` itself takes `dict[exchange, instruments]`, not
positional Bybit/Binance arguments, and groups N-way. Adding a third venue
is new capture-side work (its own `momentumsource.Instrument`
population) plus a new dict entry passed to `classify()` -- no schema
change, no classifier rewrite. Verified directly: `test_three_venue_
cluster_mixed_status_per_member` runs a synthetic three-exchange group and
checks each member gets its own independent `match_status`.

## match_status is scoped per membership, not per cluster

A cluster with three members can have two `confirmed` and one `conflict`
(e.g. two venues share a long-established asset, a third venue's own
same-ticker instrument onboarded recently with no correlating evidence).
Collapsing that to one cluster-level status would hide exactly the
divergence this step exists to surface -- so `match_status` lives on
`cluster_members`, one independent value per (cluster, exchange,
native_market_id) row.

## The classification rules, and where each one came from

The foundation doc's own colleague review specified the vocabulary
(`candidate`, `confirmed`, `conflict`, `insufficient_evidence`,
`manual_review_required`, `not_same_asset`) and one explicit worked
example before this PR existed: _"base=BTR on two venues with close
onboarding dates is a candidate, not proof of the same asset."_ That
example is the anchor the whole ladder below is built to satisfy --
**no path in this classifier reaches `confirmed` from a recently-listed
instrument's own onboarding-time evidence alone.**

- **Both established** (onboarded >= `RECENT_LISTING_WINDOW` = 90 days
  before the match run) -> `confirmed`. The one deliberately accepted
  exception to "never confirmed from a bare ticker match alone" (see
  below).
- **Recently-listed, onboarded within `CLOSE_ONBOARD_DELTA`** (7 days) of
  another member -> `candidate`. Matches the worked example verbatim.
- **Recently-listed, onboarded within `AMBIGUOUS_ONBOARD_DELTA`** (30
  days) but beyond `CLOSE_ONBOARD_DELTA` -> `insufficient_evidence`.
- **Recently-listed, beyond `AMBIGUOUS_ONBOARD_DELTA`** of every other
  member -> `conflict`.
- **One exchange contributed more than one ready instrument under the
  same base in this run** -> `manual_review_required` for every member of
  that group, including otherwise-unambiguous exchanges (with one side's
  own identity unclear, there is no reliable "other" to compare timing
  against for anyone else either). Never observed against real prod data
  as of 2026-08-17 (zero duplicate bases within either exchange), but not
  trusted blindly.
- **`not_same_asset`** is declared in the schema's CHECK constraint for
  forward compatibility and never produced: there is no evidence source
  yet strong enough to positively assert two same-ticker instruments are
  _different_ assets (that needs something like a price-series divergence
  check, which does not exist). Verified directly:
  `test_not_same_asset_is_never_produced` sweeps a range of onboarding
  deltas across the established/recent boundary and asserts the status
  never appears.

`CLOSE_ONBOARD_DELTA` (7d) and `AMBIGUOUS_ONBOARD_DELTA` (30d) are not
arbitrary round numbers: chosen against a direct read of real prod data
(2026-08-17), where 258 of 463 base-matched Bybit/Binance pairs land
within 10 days of each other, and the smallest delta observed for a
genuinely established pair (BTC) was 188 days -- comfortably clear of
both thresholds, so the `is_established` branch is checked first and this
band can never accidentally swallow an old-asset case.

## The one deliberately accepted simplification, and its tracking note

The established-both-sides branch still promotes straight from a bare
`base` + `canonical_market_type` match -- exactly what the foundation
doc's own review warned against, done anyway because ticker-squatting an
asset that has already traded under that ticker on multiple venues for
months is not a realistic risk. This was an explicit user decision (not a
default this PR silently picked): asked directly whether to ship this now
and tighten later with a real second evidence source (e.g. a price-
correlation check, once one exists), or stay conservative from the start
and require that second source before ever reaching `confirmed`. The
answer was to ship the simpler rule now -- see `ROADMAP.md`'s own item 8
entry for the explicit tracking note to revisit this once a corroborating
evidence source exists.

## Full-resync persistence, not an append-only ledger

`MomentumUniverseIdentityRepository.persist_clusters` replaces the entire
`asset_clusters`/`cluster_members` table content in one transaction on
every run, rather than diffing against the previous run. A base that
stops cross-matching (a venue delists it, or its onboarding data
regresses out of `ready`) simply has no cluster row after the next run --
not a stale leftover a downstream reader has to know to ignore. Verified
directly against real Postgres:
`test_round_trip_against_real_schema`'s own second half seeds a cluster,
persists it, then re-runs with one exchange's instrument gone and asserts
the cluster is fully cleared, not left half-populated.

No FK from `cluster_members` back to `momentum_universe_instruments`: a
run reads the LATEST snapshot per exchange and is fully recomputed each
time, matching the same reasoning `PersistUniverseSnapshot`'s own
idempotent-upsert design already applies to snapshots themselves, one
layer up. `identity_key` is still carried per member row for provenance,
even though it is not a live FK target (a snapshot's own `identity_key` is
not unique across repeated snapshots by design -- see migration 0028's
own docstring).

## Real-schema testing, not just mocked SQL assertions

Every other `*_repository.py` module in this package is tested with a
mocked SQLAlchemy connection, asserting on the compiled SQL string. This
repository is new, so that alone cannot catch a `Table()` column
definition that has silently drifted from the actual migration DDL --
the same underlying "never verified against the real schema" gap that
produced this session's two production NULL-scan crashes on the Go side,
even though the concrete failure shape differs in Python (SQLAlchemy
raises instead of a silent bad scan). `test_momentum_universe_identity_
repository_integration.py` seeds real rows into a local dev Postgres,
round-trips them through `latest_ready_instruments` -> `classify` ->
`persist_clusters`, and reads the persisted rows back directly with raw
SQL. Skips (does not fail) when no Postgres is reachable, same convention
as the Go integration tests.

## What item 9 inherits

- `app.momentum_universe_cluster_members` rows with `match_status =
'confirmed'` are the safe-to-use cross-venue asset identity mapping the
  multivenue combiner (`multivenue_confirmed_v1`) needs to join a WATCH
  decision on one venue against the same real-world asset's own state on
  another. `candidate`/`insufficient_evidence`/`conflict`/
  `manual_review_required` rows exist to be inspected, not silently
  treated as confirmed matches by a downstream consumer.
- `match_ruleset_version` on `asset_clusters` (currently `"v1"`) is the
  same versioning discipline as `momentumcapture.CaptureVersion` and every
  `*_contract.py`'s own frozen version string: bump it whenever the
  classification rules change, so a persisted `match_status` stays
  interpretable against the ruleset that actually produced it.
