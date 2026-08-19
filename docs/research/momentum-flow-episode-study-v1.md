# Momentum flow episode study v1

Status: descriptive prerequisites for HYP-014 (`discovery-ledger.md`, family
`momentum-flow`, status `parked`). This report does not confirm the family
and does not move it out of `parked`. It produces the measurement
prerequisites HYP-014's own `confirmation_requirement` field lists: an
untouched forward cohort, matched controls, and WATCH recall/lead-time.
Exact-venue after-cost economics, false-WATCH precision, capacity, week/asset
concentration sensitivity, and a Holm-corrected family read remain a later
report, once this one shows the prerequisites are actually satisfiable.

Implements the `analysis/momentum-flow-episode-study-v1` ROADMAP item. Not a
new, separate report name: an earlier working draft of this PR was scoped
under a different name and folded into this already-registered item after
review, specifically to avoid two near-duplicate momentum-flow reports with
unclear evidentiary status.

## Frozen scope

- Primary population: Bybit-native pump events only
  (`MeasurementEvent.exchange == "bybit"`, from `momentum_flow_event_
repository.py`'s existing point-in-time source/identity selection). A pump
  first observed on another exchange and joined to Bybit's own flow/OI is a
  cross-venue proxy, not this venue's own precursor signal -- those events
  are counted in the coverage funnel as `cross_venue_secondary` and excluded
  from the primary comparison, not silently merged into it.
- `dataset_since = capture_epoch_started_at + 24h` (the earliest lookback
  offset, `LOOKBACK_OFFSETS_MINUTES[0]`): no event is scored before the
  corrected capture epoch has accumulated its own full pre-trigger window.
  `--capture-epoch-started-at` is a required CLI argument with no default --
  never hardcoded. On the FIRST run of this research line, take it from
  `market:momentumcapture:health:bybit` or `runtime/momentum-canary-checkpoints
.json`; from then on, this exact boundary is FROZEN by `momentum_flow_
cohort_acceptance.py` and every later run must re-supply the SAME value
  (amended after third colleague review, before any real run: the earlier
  advice to re-derive "the current epoch" on every run meant a later
  momentum-capture restart would silently hand the SAME logical report a
  different cohort boundary, discarding comparability with already-
  accumulated history -- this stopped being hypothetical once momentum-
  capture actually restarted in production while an earlier boundary was
  still the one in use). A conflicting value is refused unless
  `--accept-new-cohort-boundary` is explicitly passed -- a deliberate,
  logged re-baseline decision, never a routine default. The accepted
  boundary persists to `MOMENTUM_FLOW_EPISODE_STUDY_COHORT_STATE_PATH`
  (`/runtime/momentum-flow-episode-study-cohort.json` in Docker, mounted
  read-write for the `analytics` service specifically, since every
  `docker compose run --rm` report invocation is otherwise a disposable
  container) and is echoed in the manifest's own `capture_epoch_started_at`
  field. This module does not yet track capture restarts/gaps as their own
  provenance or data-quality intervals within an already-accepted cohort --
  acknowledged follow-up work, not built here.
- WATCH recall specifically is computed only over events at or after
  `max(dataset_since, momentum_flow_watch_runs.cohort_started_at)`: WATCH
  could not have evaluated a pump before its own worker started, independent
  of how much capture history already existed. The wider price/flow
  descriptive comparison does not require this narrower bound.
- No CCXT calls anywhere in this report. Every price/flow/OI reading comes
  from already-captured `timeseries.bybit_momentum_bars_1m` rows, so unlike
  its v0 predecessor (`momentum_flow_event_study_report.py`) this report can
  run as a `prod-*` Makefile target.
- Bars are looked up by `event.market_id` -- Bybit's own EXACT traded market
  id for a bybit*native event, already resolved by `momentum_flow_event*
  repository.py`'s identity contract -- never by reconstructing a naive
`{base}USDT` symbol (amended after third colleague review, before any
  real run: the reconstruction happens to match every current instrument,
  but silently diverges the moment one has an unusual market id or gets
  relisted, discarding identity the event cohort already resolved as
  exact).
- Per-symbol bar fetches are bounded to `+-(control_max_search_days + 24h)`
  around that symbol's own triggers, clipped to `[capture_epoch_started_at,
until]` -- not the whole epoch-to-date range, which would grow unbounded
  week over week on a host with a tight memory budget. `_run` additionally
  processes one symbol at a time and releases that symbol's bars before
  fetching the next, rather than holding every symbol's rows resident at
  once (amended after colleague review, before any real run) -- the
  `prod-momentum-flow-episode-study-report` Makefile target also carries the
  same `PROD_REPORT_MIN_HEADROOM_MB` preflight used by comparable reports as
  a second line of defense.

## Matched controls (frozen; amended after colleague review before any real

run)

For each event, search candidate control instants for the SAME exact
instrument (identity key, market id, unified symbol -- not just base ticker),
nearest calendar distance first in EITHER direction (interleaved +-1, +-2,
+-3, ... days; a whole-day shift keeps UTC time-of-day exact). A candidate is
removed from the sequence entirely, never merely deprioritized, if:

- it falls within 24h of the event's own trigger (self-exclusion -- a shift
  of exactly 1 day is exactly the 24h boundary and is therefore always
  excluded for every event, not only when another pump happens to be
  nearby);
- it falls within 24h of any OTHER pump trigger for the same exact
  instrument -- resolved against every real Bybit pump instant for that
  base, not only the ones that also qualify for this report's own primary
  cohort (amended after colleague review, before any real run):
  `momentum_flow_event_repository.bybit_source_instants_statement` is a
  wider, identity-agnostic query on purpose, so a control point cannot land
  next to a pump that was cross-venue-first, identity-excluded, or simply
  outside `[dataset_since, until)` for this run;
- its own required `[-24h, +4h]` window would reach outside
  `[capture_epoch_started_at, until]`;
- its own following 24h quiet period would reach past `until` (amended after
  second colleague review, before any real run): the `+4h` feature-window
  check above only proves the candidate's own TIMELINE is observable, not
  that no pump has since occurred for this instrument -- the exclusion rule
  itself is a claim about the following 24h, which cannot be verified for a
  period this report's dataset does not yet cover. `candidate_at + 24h <
until`, STRICT, is required on top of, not instead of, the `+4h` maturity
  check -- matching `until`'s own exclusive-cutoff convention everywhere
  else in this report (amended after third colleague review, before any
  real run: the contamination data this check relies on, `bybit_source_
instants_statement`, loads pump sources with `first_seen_at < until`,
  exclusive; a candidate whose quiet period ended exactly AT `until` could
  not actually have its own exclusion checked against that instant, so the
  bound here must match, not allow the one edge instant the rest of the
  pipeline treats as out of scope).

The first surviving candidate whose own timeline resolves any flow
availability is used, unconditionally -- balance is never used to keep
searching for a more convenient candidate (amended after colleague review,
before any real run: doing so would make it a de facto ranking input despite
being documented as diagnostic-only). Liquidity balance (total buy+sell
notional at the FROZEN offset-0 point -- the full `[-24h, trigger)`
accumulation, the same period on both sides; amended after second colleague
review, before any real run: taking whichever offset happened to resolve
first, independently per side, could compare two structurally different
accumulation periods) is checked once against that candidate and reported as
a diagnostic. Outside a 5x ratio either direction the episode's status is
`control_unbalanced`; if either side's own offset-0 reading never resolved
(or resolved non-positive) the episode's status is `control_unresolved`
instead -- a reading that could not be compared at all is reported
differently from one that WAS compared and found too different. Turnover and
pre-window realized-volatility balance are left for a later version's
ranking formula, per the frozen matched-control rule in `momentum_flow_
protocol.py` -- this version reports liquidity balance only.

## What this report computes

- Coverage funnel: "Identity-ready cohort events" (`dataset_events`, i.e.
  `len(events)` -- already EXCLUDES `no_identity_ready_earliest_source` and
  any other upstream identity-funnel exclusion; renamed from the earlier
  "(all sources)" label after third colleague review, before any real run,
  since that label implied this count already included the upstream
  exclusions listed as their own rows immediately below, which it never
  did), `cross_venue_secondary`, `immature`, `event_flow_unavailable`,
  `control_unresolved` (no candidate's own timeline ever resolved, OR a
  candidate resolved but its own offset-0 flow reading did not),
  `control_unbalanced` (a candidate resolved, its offset-0 reading was
  usable, but the ratio failed the 5x diagnostic), complete episodes. An
  event reaching this report before `dataset_since` is a broken caller
  contract and raises rather than appearing in the funnel -- the
  repository's own query already scopes to `since=dataset_since`.
- Per-lookback (`LOOKBACK_OFFSETS_MINUTES`, the same frozen offsets as
  `momentum_flow_protocol.py`) PAIRED descriptive means: for each metric
  (price change, OI change, net flow notional) independently, only episodes
  where BOTH the event's own point and the matched control's own point
  resolved that metric are included, with their own paired N and mean paired
  delta (amended after colleague review, before any real run: appending
  event/control values independently let the two means come from different,
  unequal-length, non-corresponding sets of episodes, which was not really a
  paired comparison). Pooled, unweighted means -- no clustered bootstrap, no
  p-value, no Holm correction.
- WATCH recall: fraction of denominator events with an eligible `watch`
  decision at or before the trigger, median lead time in minutes, and the
  count/fraction where the first WATCH decision for that instrument arrived
  only after the trigger (a signal a live strategy could never have acted on
  early). The denominator additionally requires `WatchLinkage.
watch_observable` -- 100% coverage of expected one-minute evaluation
  buckets over `[trigger - 240m, trigger]` (amended after second colleague
  review, before any real run): `momentum_flow_watch_evaluations_1m` is a
  per-instrument, per-minute table, so a MISSING row over that span means
  the worker was not verifiably running or had a data gap, not that it ran
  and genuinely saw nothing. An event that is mature and in the WATCH
  cohort's own time window but fails this coverage gate is reported in
  `WatchRecallSummary.unresolved_events`, not counted as a WATCH miss.
  Coverage is computed on `bucket_start` (the market minute a row covers),
  NOT `decision_at` (when the row became readable) -- amended after third
  colleague review, before any real run: production `decision_at` trails
  `bucket_start` by roughly 90-100 seconds of evaluator latency and can
  trail much further after a restart or catch-up backlog, so using
  `decision_at` to dedupe "which minutes were covered" would silently
  miscount coverage under exactly the conditions this gate exists to catch.
  A bucket counts toward coverage only when its own `decision_at` is at or
  before the trigger (a decision that arrived after the pump could not have
  informed anything before it) AND `quality_ready` is true (a
  `rejected_quality` bucket was processed but never reached a real
  watch/no-watch call, so it cannot stand in for the registered validation
  plan's own `pumps_with_complete_pre_window` denominator). Recall and lead
  time remain computed on `decision_at`, unchanged -- a genuinely different
  question (when a decision was actionable) from which minutes were
  processed at all.
- Segments: liquidity terciles (event's own anchor flow notional) and
  repeat-token (base seen earlier in this same report's own cohort).

## What this report does not compute

No p-value, Holm correction, profit factor, expectancy, capacity, or
promotion verdict. No after-cost trade economics. No false-WATCH-rate
precision (`mature_watches_followed_by_pump / mature_eligible_watches`) --
that needs a broader WATCH-only denominator this report does not build. See
`docs/research/momentum-flow-validation-plan-v1.md`'s "Primary measurements"
section for the full confirmation-track apparatus this report deliberately
leaves for later.

## Running it

The FIRST run of this research line accepts and freezes the cohort
boundary; every later run must re-supply the exact same value (see "Frozen
scope" above):

```bash
make momentum-flow-episode-study-report \
  ARGS="--capture-epoch-started-at 2026-08-14T12:04:47.168Z --format json"

make prod-momentum-flow-episode-study-report \
  ARGS="--capture-epoch-started-at <value from market:momentumcapture:health:bybit> \
        --until 2026-09-01T00:00:00Z --format json" \
  > backups/reports/momentum-flow-episode-study-<date>.json
```

A later run with a DIFFERENT `--capture-epoch-started-at` (e.g. because
momentum-capture restarted and the health check now reports a newer
`started_at_ms`) is refused with a `CohortBoundaryConflictError`. Re-supply
the already-accepted value from `runtime/momentum-flow-episode-study-
cohort.json` (or `/runtime/...` inside the container) to keep going, or
make a deliberate decision to re-baseline:

```bash
make prod-momentum-flow-episode-study-report \
  ARGS="--capture-epoch-started-at <new value> --accept-new-cohort-boundary \
        --until 2026-09-01T00:00:00Z --format json"
```

Do not log a `discovery-ledger.md` row from this report's own descriptive
output: HYP-014 remains `parked` until the fuller confirmation-track report
(the one this report is prerequisite work for) actually runs and reads a
statistical result.
