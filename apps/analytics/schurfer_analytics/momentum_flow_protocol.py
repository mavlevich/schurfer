"""Frozen calibration protocol: analysis/bybit-early-momentum-event-study-v0.

Pre-registered 2026-08-11, before this module's own implementation was run
against any real data. This freezes METHODOLOGY only -- lookbacks, feature
definitions, exit horizons, cost model, completeness rules, and the ban on
promotion-style claims from the calibration window. It deliberately does
NOT freeze numeric thresholds: the whole point of the calibration pass is
to look at real distributions of OI/flow/price behavior around pump events
before picking any threshold, so freezing numbers now would make them as
arbitrary as the thing this project's discovery discipline exists to avoid
(see the module docstring precedent in `token_behavior_descriptors.py`).

## Research question

Do OI and taker buy/sell flow show a detectable accumulation phase before
a pump trigger, and does post-breakout flow show a detectable distribution/
reversion phase -- on top of (not instead of) the existing pump-short
strategy? If so, three separate research lanes become worth a real forward
test; if not, this stops here instead of manufacturing a signal from
whatever the calibration window happens to show.

## Family and lanes (fixed before any inference)

`momentum_flow_state_v1` is ONE family for the ten-family research budget
(see ROADMAP.md, "Research portfolio and capital discipline"; the ledger
counts a family at its first canonical outcome-bearing read, not at this
calibration pass -- see the "Calibration vs. confirmation" section below).
It contains three lanes, evaluated jointly as one Holm-corrected family
whenever a real statistical claim is eventually made about them (never
during calibration, which makes no statistical claim at all):

- `early_long`: OI and taker buy flow both rising while price is still
  range-bound -- a candidate accumulation signal for a long entry BEFORE
  the pump scanner's own trigger would fire.
- `distribution_short`: after a breakout, taker sell flow rising while
  buy flow fades and OI starts unwinding -- a candidate signal that
  reversion is starting, independent of (and potentially earlier than)
  the existing `pump_short_v1_market_quality` strategy's own entry.
- `pump_short_flow_veto`: whether confirmed-still-rising buy pressure and
  OI at the moment the existing pump-short strategy would enter should
  veto or delay that entry. This lane touches the LIVE strategy's own
  entry logic in spirit, so it is NOT folded into `pump_short_v1_
  market_quality`'s existing budget line for free -- it uses the new
  momentum-flow dataset and was proposed only after informally viewing
  BTR/VELVET-style cases, so it pays its own family cost like the other
  two lanes, not a discount for touching an existing strategy.

At most ONE lane may be nominated as a forward candidate from any single
canonical read of this family, same discipline as every other discovery
line in this project (see `token_behavior_descriptors.py`'s tie-break
rule): ties broken by lowest Holm-adjusted p-value, then by lane name.
Nominating a second lane later is a new family-budget spend on its own
untouched cohort, not a follow-on freebie.

## Calibration vs. confirmation (the two-phase freeze this module resolves)

Phase 1 (this module, frozen now): methodology only, no numbers. Lookbacks,
feature definitions, matched-control rule (deferred, see below), entry/exit
timing, cost model, completeness rules are all fixed before any real data
is viewed through this code.

Phase 2 (this PR's own implementation, run only after `CALIBRATION_WINDOW_
UNTIL` has elapsed): a single calibration run over the window below.
Produces ONLY descriptive statistics -- distributions, coverage, lead-time
observations, coarse candidate threshold RANGES. This run is logged to
`docs/research/discovery-ledger.md` as one entry for the whole family
(status `parked` or a descriptive non-promotable status; NEVER `candidate`
directly from calibration) once it actually runs, which is the point this
family's budget slot is spent (4/10 -> 5/10 as of the date that run
actually happens, not at this PR's merge).

Phase 3 (a separate, later PR: `analysis/momentum-flow-forward-contract-
v1`): freezes the ONE nominated lane's exact numeric thresholds, entry
quote, stops/exits, and cost model, informed by (but not fit to) what
calibration showed. Confirmation runs strictly on a cohort starting AFTER
`CALIBRATION_WINDOW_UNTIL`, never re-touching the calibration window.

## Measurement-only event cohort (frozen; amended 2026-08-12, colleague
review, before any real run)

The event population comes from `momentum_flow_event_repository.py`, NOT
`replay.py`/`replay_repository.py`'s eligibility contract. That contract
was built for outcome-based challenger reports and requires a resolved
N-hour trade outcome, a CLOSED pump event (`event_closed_at < until`), and
liquidity/features presence on a selected decision -- none of which this
report needs, and reusing it anyway would bias the cohort: requiring a
closed event specifically excludes long-lived pumps, meaning the event's
own character would determine sample membership. This report's cohort
needs only point-in-time identity (which pump event, base, venue and exact
derivative instrument) and a trigger instant; maturity against the
calibration window is checked separately (see "Event maturity" below).
Source time is resolved before identity readiness: only sources tied at
the event's earliest observed source timestamp may qualify, preventing a
later venue confirmation from being selected retrospectively. The chosen
tie must satisfy the project's existing exact same-venue identity contract
(identity key + market id + unified symbol, swap, matching base/USDT quote
and settle, onboarding before trigger, no conflict). A future version
could compare all earliest-source ties instead of the deterministic
exchange-name tie-break used by v0.

## Open-interest staleness policy (frozen; amended 2026-08-12, TWICE, both
before any real run -- see the two colleague-review notes below)

The Go collector carries the previous OI reading forward into a bar whose
own minute saw no fresh OI push. `open_interest is not None` on a bar
therefore does NOT mean that bar observed OI fresh; it may be repeating
an arbitrarily old value.

First amendment (later found insufficient, kept here for the record):
gated OI resolution on the bar's own `ticker_observed_this_minute` flag.
Second amendment (colleague review, 2026-08-12, before any real run):
`ticker_observed_this_minute` is set on ANY successful ticker message
that minute (`AddTickerObservation` in `apps/collector/internal/momentum/
momentum.go`), independent of whether that specific message carried an
OI update -- Bybit's ticker delta stream can update price without OI in
the same message. A bar can have `ticker_observed_this_minute=True` and
`open_interest` still carried forward from an earlier bar. The real
freshness signals are each metric's own event/observation timestamps: the
Go collector updates them only when that specific ticker field is present.
The event timestamp must belong to the bar's one-minute bucket; the
observation timestamp is when the value was actually known and may cross
the bucket boundary because of transport lag. Amount and USD value are
resolved independently through `open_interest_{event,observed}_at` and
`open_interest_value_{event,observed}_at`; freshness of one never certifies
the other. A candidate is usable only when it was observed by the target
instant and is no older than `MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES`.

## Calibration window (frozen)

`CALIBRATION_WINDOW_UNTIL` is fixed to the Bybit early-momentum-capture
canary's own 72h checkpoint due time (`momentum_canary_checkpoints.py`'s
epoch): `2026-08-13T19:05:41.810000Z`. This module's report CLI refuses to
run against a live database before that instant has actually elapsed (see
`momentum_flow_event_study_report.py`'s own gate) -- not because a
descriptive-only calibration pass could be p-hacked in the usual
promotion sense (it makes no promotion claim, ever), but because running
early would mean iterating against a partial, still-changing momentum-bars
window during active development, which is exactly the kind of "peek while
building" this project's discipline exists to avoid even when the formal
statistical risk is lower.

`CALIBRATION_WINDOW_SINCE` is deliberately NOT fixed to a single value:
old price/OI/pump-event history goes back as far as `ReplayFilters`/the
token-history dataset can supply it (bounded only by data availability),
while the NEW momentum-flow bars (`timeseries.bybit_momentum_bars_1m`)
only exist from the canary's own start (`2026-08-10T19:05:41.810000Z`) --
see `MOMENTUM_FLOW_BARS_AVAILABLE_FROM`. A pump event before that instant
gets old-data-only features (`flow_availability="unavailable_pre_capture"`
on every lookback point); the join code must never fabricate zeros for a
window with no real flow observation.

## Lookback offsets (minutes relative to the pump trigger, frozen)

Pre-trigger (accumulation window): -1440, -720, -480, -240, -120, -60,
-30, -15, -5. Trigger: 0. Post-trigger (breakout/distribution window):
+5, +15, +30, +60, +120, +240. See `LOOKBACK_OFFSETS_MINUTES`.

## Event maturity (frozen; amended 2026-08-11, colleague review, before
any real run)

An event only enters the report if its full lookback span --
`trigger + LOOKBACK_OFFSETS_MINUTES[-1]` minutes (the furthest post-trigger
point, currently +240) -- falls at or before `CALIBRATION_WINDOW_UNTIL`.
An event near the tail of the calibration window whose post-trigger
points would otherwise need data from AFTER the registered cutoff is
dropped entirely, not partially computed with whatever data happened to
exist yet: silently fetching past the cutoff for a "late" event would
both contaminate the calibration/confirmation boundary this whole
protocol exists to keep clean, and be indistinguishable from a genuine
data gap in the resulting timeline. See `momentum_flow_event_study_
report.py`'s own maturity filter.

## Point-in-time known-at rule (frozen; amended 2026-08-11, colleague
review, before any real run)

A bar's own start timestamp is NOT when its data becomes available --
same hazard `token_behavior_descriptors.py` documents for daily bars,
equally real at 1-minute/5-minute resolution. A price candle opening at
`ts_ms` only has a known CLOSE once its own period has fully elapsed
(`ts_ms + duration_ms`); a momentum-flow bar's `bucket_start` only has a
known cumulative buy/sell/OI reading once that bucket's own minute has
elapsed (`bucket_start + 60_000`). Every point in a timeline reads only
bars whose `known_at_ms <= this lookback's own target instant` -- using
the bar's own start time instead would leak up to one full bar-period of
future information into every lookback point, most obviously the point
falling inside the bar currently forming as of `target_ms`.

## Feature set (v0, deliberately bounded -- see "Out of scope" below)

Computed per lookback point, from whatever data is available at that
point (old price/OI-only, or old + new flow-enriched):
- `price_change_pct`: close at this lookback vs. the reference close at
  a FIXED anchor offset -- specifically `LOOKBACK_OFFSETS_MINUTES[0]`
  (the earliest requested pre-trigger point), never the trigger itself
  (which would make every pre-trigger point trivially "still flat
  relative to itself"), and never a per-event "whichever point happened
  to have data first" (amended 2026-08-11, colleague review: an
  event-specific floating anchor makes the SAME offset across different
  events measure "% change from 24h ago" for one event and "% change
  from itself, trivially 0" for another whose earlier history simply
  failed to fetch -- silently mixing two incomparable quantities into one
  aggregate mean). If the anchor offset's own price is unavailable for a
  given event, that event's `price_change_pct` is `None` at EVERY
  lookback point, not silently re-anchored to a different, event-specific
  offset.
- `realized_volatility`: population stdev of log-returns over the bars
  observed strictly between the timeline's start and this lookback point.
- `oi_change_pct` / `oi_value_change_pct`: open interest (amount / USD
  value) is a point-in-time LEVEL, not a cumulative flow -- same
  "closest known reading at or before this instant" semantics as price,
  anchored to the SAME fixed offset (`LOOKBACK_OFFSETS_MINUTES[0]`), from
  `open_interest` / `open_interest_value` in the new momentum-flow bars
  only (old data has no reliable point-in-time OI granular enough for
  this join; see `derivatives_context.py` for the coarser probe this
  project already has for older windows). If no known OI reading exists
  at or before the anchor (most commonly: the event's own accumulation
  window reaches back before `MOMENTUM_FLOW_BARS_AVAILABLE_FROM`),
  `None` at every point, same fail-closed rule as the price anchor. This
  is deliberately independent of the buy/sell coverage-fraction gate
  below -- a level does not need "every minute since the window started"
  to be trustworthy the way a running sum does.
- `buy_notional_usd` / `sell_notional_usd`: cumulative taker notional
  since the timeline's start through this lookback point (flow bars
  only). Gated by coverage fraction, not a single anchor bar -- see the
  completeness rule below. The timeline's own FIRST requested offset has
  a structurally zero-width window (nothing has accumulated yet at the
  instant the window just opened) and is therefore always excluded here;
  this is the expected "a running total starts at zero observations"
  reading, not a data gap.
- `net_flow_notional_usd`: `buy_notional_usd - sell_notional_usd` over the
  same cumulative window.
- `flow_availability`: `"unavailable_pre_capture"` | `"gap_excluded"` |
  `"available"` -- fail-closed per point, never silently treated as zero
  flow. `"gap_excluded"` covers a flow bar present but `complete=False`
  (see completeness rule below).

## Completeness / fail-closed exclusion rule (frozen)

A flow bar contributes to any cumulative feature only when its own
`complete` flag is `True` (both `ticker_complete` AND `trades_complete`,
per the momentum-capture contract -- see `0024_bybit_momentum_bars_1m`'s
own migration docstring). An incomplete bar's minute is excluded from the
cumulative sums entirely, not treated as zero volume for that minute --
zero-filling an incomplete bar would understate real flow and could
manufacture a false "buying dried up" read purely from a collection gap
(exactly the failure mode the 2026-08-11 canary queue-pressure incident
showed is real on this data source). `unbackfilled_gap_minutes > 0`
overlapping the cumulative window is treated the same way: excluded, not
zero-filled. This module's own timeline engine
(`momentum_flow_timeline.py`) never distinguishes a "startup gap" from a
"reconnect gap" from "real signal absence" any more precisely than this in
v0 -- that finer distinction is exactly what `fix/momentum-capture-
readiness-observability-v1` (a separate, parallel PR) is for; until it
ships, this module only ever knows "complete" or "excluded".

Excluding an incomplete bar from a SUM and zero-filling it produce the
IDENTICAL number for that sum -- a plain running total cannot tell "this
minute contributed nothing because it was excluded" apart from "this
minute contributed nothing because it was recorded as zero". Excluding
bars from the sum is therefore not, by itself, enough to honor this
section's own stated intent (amended 2026-08-11, colleague review, before
any real run): `FLOW_AVAILABLE` additionally requires the window's
coverage fraction (bars actually present and complete, divided by the
number of one-minute buckets the window spans) to equal
`FLOW_FULL_COVERAGE_FRACTION` exactly. Any non-empty window short of full
coverage is `FLOW_PARTIAL_COVERAGE`, excluded from the clean per-lookback
aggregates the same way `FLOW_GAP_EXCLUDED` already is -- an undercounted
cumulative sum must never be presented as if it were the complete one.

## Entry / exit timing (frozen, matches this project's established replay
convention -- see `ohlcv.py`'s `next_timeframe_after`)

A hypothetical entry (used only in a later confirmation-stage economics
report, not computed by this module) would use the next fully-closed bar
strictly after the pump trigger, never the trigger bar itself. Exit
horizons for any future economics read: 5, 15, 30, 60, 120, 240 minutes
after entry -- the same set as the post-trigger lookback offsets above, so
a feature observed at a lookback and a hypothetical outcome at the same
horizon are directly comparable without a second, differently-spaced grid.

## Cost model (frozen)

Any future economics read uses `schurfer_performance.accounting.
CostParameters`'s existing defaults (`taker_fee_bps_per_side=10.0`,
`funding_cost_bps_per_8h=5.0`), the same shared cost model every other
challenger report in this project already uses. Not reimplemented here.

## Matched controls (rule frozen, selection code deferred to v1)

A matched control for a pump event: the same base asset, a UTC time-of-day
within +-2 hours of the event's own trigger time, on a day with no pump
event of its own for that asset within +-24 hours of the control point (to
avoid a control that is itself contaminated by a different pump). This
module freezes the RULE now so a future selection implementation cannot
quietly redefine "matched" after seeing which controls look convenient;
the selection code itself is out of scope for v0 (see below) and must
import this exact rule when built, not restate it.

## Explicitly out of scope for v0 (planned, not built here)

- Matched-control SELECTION code (rule is frozen above; implementation is
  a follow-up).
- 10s/30s burst features, large-trade histogram share, top-K trade
  analysis (the new bars carry this data; v0's timeline engine does not
  yet read those columns).
- Hypothetical trade economics (MFE/MAE, cash-inclusive PnL, capacity).
- False-WATCH base rate (requires the WATCH state machine, a separate
  later PR).
- BTR/VELVET or any other externally-sourced Telegram case study as
  anything other than an illustrative, separately-labeled example. See
  [[external-telegram-reference-only]]: never a data or label source for
  this or any Schurfer research line.

## What v0 actually answers (amended 2026-08-11, colleague review, before
any real run)

The stated research question -- whether OI/flow show DETECTABLE
accumulation before a trigger -- needs a baseline to be detectable
against. v0's own report renders only raw, pooled descriptive statistics
per lookback (mean/median price change, mean OI change, mean net flow)
across pump events with no matched-control comparison, since control
selection is out of scope for v0 (see above). Read in isolation, v0's own
output can describe what pump-event windows look like; it cannot yet say
whether that looks unusual relative to a non-pump baseline, which is what
the research question actually asks. Treat v0 as the input a human uses
to sanity-check coverage/join correctness and eyeball rough magnitudes
before matched-control selection lands, not as an answer to the research
question by itself.

## Banned claims from the calibration read (enforced by convention, not
just documentation -- `momentum_flow_event_study_report.py`'s own render
functions never emit a p-value, Holm correction, or promotion verdict)

Once matched-control selection exists, the calibration run may describe:
which lookbacks show any separation at all between pump events and
matched controls; how many minutes/hours before a trigger the first
detectable separation tends to appear; how often OI/flow rise without a
subsequent price move; coverage and flow-availability rates; and which of
the three lanes looks worth a real forward test. It may NOT claim: that a
lane is profitable, that a profit factor computed on calibration data is
evidence, that a threshold found here is validated, that paper trading
may start, or that a single BTR/VELVET-shaped trajectory confirms an
edge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

MOMENTUM_FLOW_PROTOCOL_VERSION = "momentum_flow_protocol_v0"
MOMENTUM_FLOW_FAMILY = "momentum_flow_state_v1"

LANE_EARLY_LONG = "early_long"
LANE_DISTRIBUTION_SHORT = "distribution_short"
LANE_PUMP_SHORT_FLOW_VETO = "pump_short_flow_veto"
MOMENTUM_FLOW_LANES = (LANE_EARLY_LONG, LANE_DISTRIBUTION_SHORT, LANE_PUMP_SHORT_FLOW_VETO)

# The Bybit early-momentum-capture canary's own start and 72h checkpoint,
# both taken directly from the running canary's `started_at_ms`
# (1786388741810) -- not independently re-derived, so this module can
# never silently drift from the actual capture window it depends on.
MOMENTUM_FLOW_BARS_AVAILABLE_FROM = datetime(2026, 8, 10, 19, 5, 41, 810000, tzinfo=UTC)
CALIBRATION_WINDOW_UNTIL = datetime(2026, 8, 13, 19, 5, 41, 810000, tzinfo=UTC)

# Not empirically derived -- picked without a real measured OI push
# inter-arrival distribution, same honest caveat as
# apps/collector/internal/momentum/main.go's own tickerGapThreshold.
# Revisit once the canary has real per-symbol OI inter-arrival data. A
# candidate OI observation older than this relative to the lookback point
# being evaluated is treated as too stale to represent "now", not used
# regardless of how recent it is relative to other candidates. See
# momentum_flow_timeline.py's `_closest_known_oi_at_or_before`.
MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES = 30

# Minutes relative to the pump trigger (0). Negative = pre-trigger
# (accumulation window), positive = post-trigger (breakout/distribution
# window). Frozen; adding or removing a lookback changes what a
# calibration read can say and must be its own documented amendment, not
# a silent edit.
LOOKBACK_OFFSETS_MINUTES: tuple[int, ...] = (
    -1440,
    -720,
    -480,
    -240,
    -120,
    -60,
    -30,
    -15,
    -5,
    0,
    5,
    15,
    30,
    60,
    120,
    240,
)

# Same set as the post-trigger lookbacks above, kept as an explicit named
# constant because a future economics report reads it under its own name
# (exit horizons), not as "the positive half of LOOKBACK_OFFSETS_MINUTES"
# re-derived ad hoc at every call site.
EXIT_HORIZONS_MINUTES: tuple[int, ...] = tuple(m for m in LOOKBACK_OFFSETS_MINUTES if m > 0)

# Matched-control rule, frozen per the module docstring. Consumed by a
# future selection implementation; not applied by anything in v0.
MATCHED_CONTROL_TIME_OF_DAY_TOLERANCE_HOURS = 2.0
MATCHED_CONTROL_EXCLUSION_WINDOW_HOURS = 24.0

FlowAvailability = str  # "unavailable_pre_capture"|"gap_excluded"|"partial_coverage"|"available"
FLOW_UNAVAILABLE_PRE_CAPTURE: FlowAvailability = "unavailable_pre_capture"
FLOW_GAP_EXCLUDED: FlowAvailability = "gap_excluded"
# Amended 2026-08-11 (colleague review, before any real run): a window with
# SOME but not ALL of its expected one-minute flow bars present used to be
# reported as flatly "available", silently zero-filling the missing/
# incomplete minutes into the cumulative sum by omission -- mathematically
# indistinguishable from actually treating them as zero volume, which is
# exactly what the fail-closed exclusion rule above says must never happen.
# FLOW_AVAILABLE now requires exactly 100% of the expected one-minute bars
# in the cumulative window to be present AND complete; anything less is
# FLOW_PARTIAL_COVERAGE, excluded from the clean per-lookback aggregates
# the same way FLOW_GAP_EXCLUDED already is, with its own coverage
# fraction (`TimelinePoint.flow_coverage_pct`) kept for diagnostics.
FLOW_PARTIAL_COVERAGE: FlowAvailability = "partial_coverage"
FLOW_AVAILABLE: FlowAvailability = "available"
# 100% required for FLOW_AVAILABLE -- see the amendment note above. Not
# configurable in v0: a lower bar would need its own explicit
# justification and is not something to silently default to.
FLOW_FULL_COVERAGE_FRACTION = 1.0


def flow_bars_available_at(moment: datetime) -> bool:
    """Whether the new momentum-flow bars table could possibly have data
    at this instant -- purely a capture-window check, not a completeness
    check (a bar can exist and still fail completeness; see the module
    docstring's fail-closed exclusion rule, applied by
    `momentum_flow_timeline.py`, not here)."""
    return moment >= MOMENTUM_FLOW_BARS_AVAILABLE_FROM


def event_is_mature(trigger_at: datetime, until: datetime) -> bool:
    """Whether an event's full lookback span -- out to the furthest
    post-trigger offset -- fits at or before `until` (normally
    `CALIBRATION_WINDOW_UNTIL`). See the module docstring's "Event
    maturity" section: an event that fails this must be dropped from the
    report entirely, never partially computed with data that would have
    to reach past the registered cutoff."""
    return trigger_at + timedelta(minutes=LOOKBACK_OFFSETS_MINUTES[-1]) <= until
