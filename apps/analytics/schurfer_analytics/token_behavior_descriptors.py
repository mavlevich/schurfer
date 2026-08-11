"""Point-in-time token-history descriptors: analysis/token-behavior-discovery-v1.

Pre-registered 2026-08-11, before any of these descriptors were computed
against real outcomes. This is a full pre-registration for the discovery
family below -- the frozen contract, not just a description of the code --
so that the eventual bridge to `challenger_inference`/`simulate_decision`
(a separate, later PR) has nothing left to decide after seeing results. This
module is itself deliberately self-contained and DB-free: every function
here takes already-loaded bars and returns a value, so it can be
unit-tested without a live database or the replay/challenger machinery the
future bridge wires it into.

## Research question

Can pre-decision token-history descriptors (recurrence of historical
spikes, volatility, recovery behavior, listing age) filter out bad short
trades in the existing pump-short strategy, on top of the unchanged
`score_6` baseline?

## Data window (frozen)

The already-frozen Parquet dataset
`backups/token-history/token_history_ohlcv_v1/20260810T081729Z-6f781fae/`:
`2026-07-26T00:00:00Z` <= decision < `2026-08-09T23:21:14.187586Z`. Verify
`dataset_content_fingerprint ==
22d23eba6997b509802cd3fe7a50b7dd90958a525ade5275ffbd7444b5cd0651` before
trusting it for anything -- a manifest drift invalidates the whole cohort.

## Baseline

`score_6` baseline-triggered decisions (`SCORE_THRESHOLD_BASELINE_POLICY`),
restricted to instruments covered by the frozen dataset (47 instruments).
The baseline itself is completely unchanged, same as every other filter
report in this project (see `oi_growth_filter_report.py`).

## Candidate filter family (bounded, 4, Holm-corrected jointly)

Each candidate gates the baseline's own decision to cash or not -- never
recomputes a different decision, never selects a different entry. Threshold
= the descriptor's own median over the baseline cohort, computed once and
frozen, never re-derived per candidate or per run.

1. `prior_spike_count_90d == 0` -> cash. No spike in the last 90 days is a
   "first-time mover", riskier/less predictable for a reversion short.
2. `historical_volatility_30d > median` -> cash. Above-median historical
   volatility gives worse risk/reward on a reversion trade.
3. `days_since_last_spike_recovery > median` (among tokens with a resolved
   prior spike; tokens with `no_prior_spike` are excluded from this
   candidate's population, not folded into cash or trade) -> cash. Slow
   historical recovery suggests still-fragile price action.
4. `listing_age_days < median` -> cash. A freshly-listed token has thinner
   order books and less-established trading behavior.

## `historical_spike_v1` (replaces `app.pump_events` as the history source)

The scanner has only been running a matter of days, so "zero recorded pump
events in the last 90 days" for a token is indistinguishable from "the
scanner simply wasn't watching yet" (left-censoring), not evidence the
token never spiked. Spikes are instead derived entirely from the token's
own OHLCV (`detect_historical_spikes`):
- Exact same-venue instrument (no cross-venue substitution).
- A day qualifies when its fully-closed high / the immediately preceding
  fully-closed day's close - 1 >= 30%.
- Consecutive qualifying days merge into one episode.
- `prior_spike_count_90d` needs the full 90 days plus one more (for the
  oldest in-window day's own previous close) actually covered by bars, or
  it is unresolved, never a confident zero.
- Recovery: the first subsequent fully-closed daily close within +-10% of
  the episode's `pre_spike_close`, measured from the episode's LAST day
  (when the move stopped), not its first.
- An unresolved (not-yet-recovered) episode stays right-censored via
  `RecoveryResult.observed_for_days` -- see that dataclass's own docstring.
- The pump currently being decided on can never appear in its own history:
  an episode requires its own day to be fully closed, and the current
  decision's own day never is at decision time. This is structural, not an
  exclusion a caller could forget to apply.

## `historical_volatility_30d`

- Window: 30 calendar days, anchored to `known_at_ms` (when each bar's
  close actually became available), not to bar-open time -- anchoring to
  open time undercounts by one full bar for most (non-UTC-midnight)
  decisions, making a `min_returns=29` threshold unreachable in practice.
- Minimum: 29 log-returns from 30 fully-closed daily closes. Any shortfall
  (a gap, a short listing history) is unresolved, never a value computed on
  a thinner, noisier sample.

## Primary metric

Paired delta (challenger net_return_pct - baseline net_return_pct) per
candidate, cluster-bootstrapped by asset, Holm-Bonferroni corrected across
all 4 candidates jointly, via `challenger_inference.build_challenger_
inference` -- the same machinery `oi_growth_filter_report.py` already uses,
not reimplemented here.

## Discovery readiness gates (frozen; this is discovery, not confirmation)

- >= 60 comparable baseline-triggered decisions.
- >= 20 distinct asset clusters.
- >= 2 distinct UTC weeks.
- No single UTC week over 70% of the formal sample (concentration cap).
- The descriptor resolved (not unresolved/missing) for >= 80% of the
  baseline population -- a data-availability pattern must never masquerade
  as the effect being tested.
- A surviving candidate must actually change the outcome of >= 10 trades
  across >= 8 distinct assets (a materiality floor: a statistically
  "significant" result driven by 2 trades on 1 asset is not a candidate).
- Holm-adjusted paired delta lower bound > 0 is necessary but not
  sufficient: the challenger must also have its own cash-inclusive
  expectancy > 0 AND profit factor > 1 on the exact same frozen sample. A
  positive delta against a losing baseline is not a profitable filter.
- Last UTC week (temporal robustness slice, not a formal test): the
  effect's direction and the challenger's own expectancy must both stay
  positive within that slice alone. Statistical significance is NOT
  required within a slice this small -- only sign-consistency.
- At most ONE candidate may be nominated as a forward filter per this
  discovery pass, per ROADMAP's discovery-vs-confirmation discipline.

## Confirmation requirement (stricter, after nomination -- a separate PR)

If a candidate survives discovery: register its exact frozen rule (the
descriptor formula AND the frozen threshold, both exactly as computed in
this pass) plus a point-in-time SNAPSHOT of its input data. A future
forward cohort must never re-fetch fresh OHLCV history and recompute
descriptors against it -- delisting or OHLCV-availability changes between
now and then would introduce survivorship bias into what is supposed to be
a frozen, reproducible rule. The new forward cohort starts strictly after
`2026-08-09T23:21:14.187586Z` and needs, matching `oi_growth_filter_report.
py`'s own precedent: >= 100 episodes, >= 30 asset clusters, >= 4 distinct
UTC weeks, before any promotion verdict.

## Point-in-time hazards fixed here after review, all specific to DAILY bars

1. A daily bar's `close` describes the price at the END of that calendar
   day, while `ts_ms` marks its START. Treating a bar as "known" the moment
   `ts_ms < decision_ts` leaks same-day future information whenever a
   decision falls within the same calendar day as a bar it is compared
   against. Fix, used throughout: a bar only counts as available once its
   full period has elapsed, i.e. `bar.ts_ms + ONE_DAY_MS <= decision_ts_ms`
   (`DailyBar.known_at_ms`).
2. `sorted(x.close for x in bars if ...)` sorts by PRICE, not by time --
   daily log-returns computed on a price-sorted sequence are not returns
   between chronologically adjacent days at all. Bars must be sorted by
   `ts_ms` first, closes extracted in that order, before differencing.
3. A "pre-event reference price" selected by `bar.ts_ms < event_ms` alone is
   not enough: a bar whose day STARTED before the event but whose CLOSE only
   became known after it (because the event happened partway through that
   same day) still leaks event-affected price into the reference. The
   reference bar must satisfy `bar.known_at_ms <= event_ms`, not just
   `bar.ts_ms < event_ms`.

Every descriptor is computed using ONLY data strictly before the decision's
own timestamp (`decision_ts`) -- never the decision's own outcome, never a
later bar. A descriptor that cannot be computed from the available
point-in-time data (insufficient lookback, insufficient observation time)
returns `None`/a sentinel status and must be treated as unresolved by the
caller, never silently folded into zero, "did not trigger the filter", or
any other implicit default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import log
from statistics import pstdev
from typing import Literal

ONE_DAY_MS = 86_400_000

# --- shared bar input shape ---


@dataclass(frozen=True)
class DailyBar:
    """One row from a token-history Parquet file (token_history_parquet_
    dataset.py's `bars` table: ts_ms, open, high, low, close, volume, ...).
    Only the fields these descriptors need. `ts_ms` is the START of the
    bar's UTC day; `close`/`high` are only actually known once that whole
    day has elapsed -- see the module docstring."""

    ts_ms: int
    close: float
    high: float

    @property
    def at(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ms / 1000, tz=UTC)

    @property
    def known_at_ms(self) -> int:
        """The earliest instant this bar's close/high are actually
        available: the end of its own UTC day, not its start."""
        return self.ts_ms + ONE_DAY_MS


def _known_before(bars: tuple[DailyBar, ...], decision_ts_ms: int) -> list[DailyBar]:
    """Bars whose full day has elapsed strictly before decision_ts, sorted
    chronologically. The single shared point-in-time filter every descriptor
    below that touches bars must go through -- see module docstring hazard 1."""
    return sorted(
        (bar for bar in bars if bar.known_at_ms <= decision_ts_ms),
        key=lambda bar: bar.ts_ms,
    )


def _by_day(bars: list[DailyBar]) -> dict[int, DailyBar]:
    return {bar.ts_ms: bar for bar in bars}


# --- listing age ---


def listing_age_days(*, decision_ts: datetime, onboarded_at: datetime) -> float:
    """Days between the instrument's own recorded onboarding time and this
    decision. Both timestamps already come from data recorded before or at
    decision time (onboarded_at is a historical fact, not derived from
    anything the decision could look ahead into), so no point-in-time guard
    is needed here beyond the inputs themselves being correct."""
    return (decision_ts - onboarded_at).total_seconds() / 86400.0


# --- historical volatility ---


def historical_volatility(
    *,
    bars: tuple[DailyBar, ...],
    decision_ts: datetime,
    lookback_days: int,
    min_returns: int,
) -> float | None:
    """Population standard deviation of daily log-returns over the
    `lookback_days` calendar days strictly before decision_ts.

    The window is filtered by `known_at_ms` (when a bar's close actually
    became available), not by `ts_ms` (when its day started): filtering by
    ts_ms alongside decision_ts's own (usually intraday, not
    midnight-aligned) time-of-day systematically undercounts by one full
    bar for most decisions, since the window boundary falls mid-day rather
    than aligned to the UTC-midnight-anchored bars -- `min_returns=30`
    against a ts_ms-filtered 30-day window is unreachable for almost every
    real decision. Anchoring the window to known_at_ms instead counts "the
    last `lookback_days` days of information actually available as of
    decision_ts", which is both the correct point-in-time semantics and
    achievable in practice.

    `min_returns` (not `min_observations`): with C closes there are only
    C-1 usable log-returns, and it is the RETURNS count a volatility
    estimate's reliability actually depends on. Has no default deliberately
    -- the caller must consciously freeze a real minimum (e.g. requiring
    most of a 30-day window to actually be covered) before this is used for
    anything, not accept whatever a thin-history token happens to produce.
    Returns None (unresolved, not zero) whenever fewer than `min_returns`
    log-returns can be formed."""
    decision_ts_ms = int(decision_ts.timestamp() * 1000)
    window_start_known_at_ms = decision_ts_ms - lookback_days * ONE_DAY_MS
    known_bars = _known_before(bars, decision_ts_ms)
    closes = [
        bar.close
        for bar in known_bars
        if bar.known_at_ms > window_start_known_at_ms and bar.close > 0
    ]
    if len(closes) - 1 < min_returns:
        return None
    log_returns = [log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    return pstdev(log_returns)


# --- historical spikes (OHLCV-derived, replaces app.pump_events history) ---


@dataclass(frozen=True)
class HistoricalSpike:
    """A run of one or more consecutive fully-closed UTC daily bars where
    high/previous_close - 1 >= the frozen threshold (`historical_spike_v1`),
    merged into a single episode. `pre_spike_close` is the close on the
    calendar day immediately before the episode started -- already
    guaranteed known before the episode itself by construction (hazard 3)."""

    first_day_ts_ms: int
    last_day_ts_ms: int
    pre_spike_close: float


@dataclass(frozen=True)
class SpikeHistory:
    """Result of scanning for historical spikes over a specific lookback.
    `coverage_ok` is False when the available bars do not reach far enough
    back to rule out spikes hidden by insufficient history (left-censoring)
    -- callers must treat `spikes`/any count derived from it as unresolved
    whenever this is False, not as a confident empty/zero result."""

    spikes: tuple[HistoricalSpike, ...]
    coverage_ok: bool


def detect_historical_spikes(
    *,
    bars: tuple[DailyBar, ...],
    decision_ts: datetime,
    lookback_days: int,
    threshold_pct: float,
) -> SpikeHistory:
    """Scans fully-closed daily bars strictly before decision_ts (hazard 1)
    for days where high/previous_close - 1 >= threshold_pct, where
    previous_close is the immediately preceding CALENDAR day's close (a
    data gap -- no bar for that exact preceding day -- makes that day's
    eligibility unknown, not non-qualifying, and breaks any in-progress
    run). Consecutive qualifying days merge into one episode.

    The pump currently being decided on can never appear here: an episode
    requires its own day to be fully closed, and the current decision's own
    day never is at decision time -- this is structural, not an explicit
    exclusion a caller could forget to pass.

    `coverage_ok` requires bars reaching back at least `lookback_days` + 1
    full days before decision_ts (the extra day is for the oldest
    in-window day's own previous-close). Without that much history, an
    apparent zero-spike result cannot be distinguished from "we simply
    don't have the data to know" -- exactly the left-censoring problem this
    function exists to avoid."""
    decision_ts_ms = int(decision_ts.timestamp() * 1000)
    known_bars = _known_before(bars, decision_ts_ms)
    by_day = _by_day(known_bars)

    window_start_ms = decision_ts_ms - lookback_days * ONE_DAY_MS
    oldest_needed_ms = window_start_ms - ONE_DAY_MS
    coverage_ok = any(bar.ts_ms <= oldest_needed_ms for bar in known_bars)

    in_window_days = sorted(ts_ms for ts_ms in by_day if window_start_ms <= ts_ms < decision_ts_ms)

    spikes: list[HistoricalSpike] = []
    episode_start_ms: int | None = None
    episode_end_ms: int | None = None
    episode_pre_close: float | None = None
    for day_ms in in_window_days:
        previous = by_day.get(day_ms - ONE_DAY_MS)
        current = by_day[day_ms]
        qualifies = (
            previous is not None
            and previous.close > 0
            and (current.high / previous.close - 1) >= threshold_pct / 100.0
        )
        if qualifies:
            if episode_start_ms is None:
                episode_start_ms = day_ms
                episode_pre_close = previous.close  # type: ignore[union-attr]
            episode_end_ms = day_ms
        elif episode_start_ms is not None:
            spikes.append(
                HistoricalSpike(episode_start_ms, episode_end_ms, episode_pre_close)  # type: ignore[arg-type]
            )
            episode_start_ms = episode_end_ms = episode_pre_close = None
    if episode_start_ms is not None:
        spikes.append(
            HistoricalSpike(episode_start_ms, episode_end_ms, episode_pre_close)  # type: ignore[arg-type]
        )

    return SpikeHistory(spikes=tuple(spikes), coverage_ok=coverage_ok)


def prior_spike_count(*, spike_history: SpikeHistory) -> int | None:
    """None (unresolved) when coverage is insufficient to trust a zero;
    otherwise the count of distinct spike episodes in the scanned window."""
    if not spike_history.coverage_ok:
        return None
    return len(spike_history.spikes)


# --- recovery from the most recent historical spike ---

RecoveryStatus = Literal[
    "no_prior_spike",
    "missing_reference_price",
    "not_yet_recovered_by_decision",
    "recovered",
]


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryStatus
    # Only set when status == "recovered". None in every other case -- a
    # caller must switch on status first, never treat a None here as zero.
    recovered_in_days: float | None = None
    # The spike this result is relative to; None only for "no_prior_spike".
    reference_spike: HistoricalSpike | None = None
    # Set whenever reference_spike is set: days elapsed between the spike
    # episode ENDING and decision_ts. This is the right-censoring signal for
    # "not_yet_recovered_by_decision" -- that status means "had not
    # recovered within observed_for_days", which is only a DEFINITIVE
    # non-recovery once observed_for_days exceeds whatever threshold a
    # candidate filter freezes. Shorter than that threshold means the
    # decision simply came too soon to know, which is unresolved, not a
    # confirmed slow-recovery signal.
    observed_for_days: float | None = None


def days_since_last_spike_recovery(
    *,
    bars: tuple[DailyBar, ...],
    decision_ts: datetime,
    spike_history: SpikeHistory,
    recovery_band_pct: float,
) -> RecoveryResult:
    """For the most recent historical spike episode (see
    `detect_historical_spikes`), how many days elapsed between that
    episode's last day and price first closing back within
    +-recovery_band_pct of `pre_spike_close`. Recovery is measured from the
    episode's END (when the run of qualifying days stopped), not its start
    -- recovery is a property of the price reverting after the move
    finished, not of how long the move itself lasted.

    `no_prior_spike` covers both a genuine absence of any spike AND
    insufficient coverage to say so (`spike_history.coverage_ok is False`)
    -- both are "cannot identify a reference episode", the same unresolved
    outcome for this descriptor's purposes.

    `missing_reference_price` is now DISTINCT from `no_prior_spike`: it
    means a real spike episode was identified but this token's own bars do
    not actually contain its `pre_spike_close` (should not happen given
    `detect_historical_spikes` only forms episodes with a confirmed
    previous close, but kept as an explicit, honestly-labeled defensive
    path rather than silently reusing a different diagnosis)."""
    if not spike_history.coverage_ok or not spike_history.spikes:
        return RecoveryResult(status="no_prior_spike")
    reference_spike = max(spike_history.spikes, key=lambda spike: spike.last_day_ts_ms)
    if reference_spike.pre_spike_close <= 0:
        return RecoveryResult(status="missing_reference_price", reference_spike=reference_spike)

    decision_ts_ms = int(decision_ts.timestamp() * 1000)
    observed_for_days = (decision_ts_ms - reference_spike.last_day_ts_ms) / (1000 * 86400.0)

    known_bars = _known_before(bars, decision_ts_ms)
    lower = reference_spike.pre_spike_close * (1 - recovery_band_pct / 100.0)
    upper = reference_spike.pre_spike_close * (1 + recovery_band_pct / 100.0)
    for bar in known_bars:
        if bar.ts_ms <= reference_spike.last_day_ts_ms:
            continue
        if lower <= bar.close <= upper:
            recovered_days = (bar.ts_ms - reference_spike.last_day_ts_ms) / (1000 * 86400.0)
            return RecoveryResult(
                status="recovered",
                recovered_in_days=recovered_days,
                reference_spike=reference_spike,
                observed_for_days=observed_for_days,
            )
    return RecoveryResult(
        status="not_yet_recovered_by_decision",
        reference_spike=reference_spike,
        observed_for_days=observed_for_days,
    )
