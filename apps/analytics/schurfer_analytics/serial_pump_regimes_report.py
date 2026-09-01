"""Discovery report for ROADMAP item 8 (`research/serial-pump-regimes-v1`):
what happened after a pump on a given asset, across every independent pump
regime (not only ones that went on to "win"), recurrence count and
inter-episode intervals, venue expansion, BTC-adjusted return, and
MFE/MAE/time-to-peak/retrace/delisting across six horizons
(15m/1h/4h/1d/7d/30d).

Discovery-only, no verdict (explicit user decision, 2026-08-31) -- see
serial_pump_regimes.py's own module doc comment. This is read-only: it
does not write to any table, does not touch trade execution, and adds no
new capture.

**GROSS returns, no costs -- not a hold/sell recommendation on its own**
(colleague review, 2026-09-01, round 2). Every `forward_return_pct`/
`btc_adjusted_return_pct`/`mfe_pct`/`mae_pct` here is a raw OHLCV price
move: no spread, no taker/maker fees, no slippage, no funding. For a
15-minute horizon in particular, gross and after-cost can differ by more
than the entire median return this report shows. This module makes no
claim about which horizon is "better to exit at" or whether holding is
profitable -- that conclusion requires a separate after-cost study using
this codebase's own shared cost model (`packages/performance`'s
`calculate_performance`/`CostParameters`, the same one `source_lead_
forward_cohort.py` already uses for its own registered contract) before
any number here is read as an economic recommendation.

Reuses, rather than reimplements:
- `pump_recurrence_integrity_report`/`_repository`'s own `Episode`/
  `Regime`/`merge_episodes_into_regimes`/`SourceIdentityObservation` and
  its `PumpRecurrenceIntegrityRepository.load()` (one repeatable-read
  transaction already returning exactly the episode + per-venue identity
  data this report needs).
- `serial_pump_regimes.py`'s own pure `decision_boundary_ms`/
  `resolve_horizon_outcome`/`recurrence_summary`.
- `ohlcv.fetch_symbol_candles` (shared, cached CCXT paging, identity-safe:
  takes an already-resolved unified symbol, never reconstructs one from a
  bare ticker -- see "Canonical identity" below) for forward OHLCV.
- `token_universe_coverage.py`'s own `MomentumUniverseIdentityRepository.
  instruments_as_of` (research/token-universe-coverage-v1) for venue
  expansion.
- `reporting.py`'s shared `json_ready`/`markdown_table`/
  `normalize_code_revision`/`parse_utc_datetime`.

## Canonical identity, not a bare ticker (colleague review, 2026-09-01)

An earlier version of this report grouped regimes by bare `base`,
picked an exchange from `app.pump_event_sources` without checking
`identity_conflict`/agreement across episodes, and fetched OHLCV via
`ohlcv.fetch_candles(client, base, ...)`, which reconstructs
`f"{base.upper()}/USDT:USDT"` -- exactly the class of bug this codebase's
identity foundation/resolution PRs and the recurrence-integrity audit
exist to prevent: a relisted or ticker-colliding instrument could silently
have its own OHLCV path built from a different contract than the one that
was actually observed. `_resolve_regime_identities` now requires every
`SourceIdentityObservation` recorded for a regime's own episode_ids, on a
given exchange, to agree on one `identity_key` and one `unified_symbol`,
with no `identity_conflict` flag set anywhere in that group -- otherwise
that exchange is treated as having no usable identity for this regime at
all (`ambiguous_identity`), never a guess at which one is right.
`_pick_ohlcv_identity` then only ever hands `_process_regime` an already-
disambiguated `ResolvedIdentity`, whose own `unified_symbol` goes straight
into `ohlcv.fetch_symbol_candles` -- no ticker reconstruction anywhere in
this module. Venue expansion (`_venue_expansion`) uses the same resolved
`identity_key`, matched against `AsOfCoverage.identity_keys`, not a
reconstructed `base.upper() + "USDT"` against `native_market_ids`.

`EXCHANGE_OHLCV_PRIORITY` is a disclosed simplification, not a literal
mirror of `apps/api-gateway/internal/pumps/handler.go`'s own
`ohlcvPriority`: the Go side sorts primarily by each exchange's own live
`volume_24h_usd`, using `ohlcvPriority` only as a tie-breaker; fetching a
fresh volume ranking per regime here would mean an extra live API call
per candidate exchange per regime, which is out of proportion for a
discovery report. This module uses the static priority list alone --
still liquidity-ordered, just not volume-refreshed per run.

## Honest limitations, disclosed rather than papered over

- **Venue expansion** only has evidence for `VENUE_EXPANSION_EXCHANGES`
  (bybit, binance -- the only exchanges `research/token-universe-
  coverage-v1`'s own snapshot history covers), and only from
  `2026-08-15` on (when that capture started). A regime from before that
  date, or asking about any other exchange, gets `ready_before`/
  `ready_after` = `None` (no evidence either way), never a guessed
  `False`. A regime whose own 30-day-forward point has not yet occurred
  as of this run's `evaluation_at` gets `ready_after=None` unconditionally
  (`VenueExpansionEntry.after_at_matured=False`) -- `instruments_as_of`'s
  own "nearest snapshot at or before as_of" semantics would otherwise
  silently answer a future instant with today's current state, presenting
  a not-yet-determined outcome as if already known.
- **OHLCV source per regime** is picked from whichever exchange(s)
  `app.pump_event_sources` recorded for that regime's own episodes with an
  unambiguous, conflict-free identity (see "Canonical identity" above), by
  a fixed liquidity-ordered priority (`EXCHANGE_OHLCV_PRIORITY`).
  `ohlcv_unresolved_reason` is set, never silently guessed, when no source
  was ever recorded, none of the recorded exchanges have an OHLCV-capable
  client, or the recorded identity itself is ambiguous/conflicting.
- **Decision anchor is `regime.first_seen_at`**, matching item 8's own
  "after a FIRST pump" framing -- see `serial_pump_regimes.py`'s own
  module docstring for why `last_seen_at` would be a look-ahead. A regime
  still within one cooldown of this run's own `evaluation_at`
  (`RegimeRow.regime_mature=False`) may still gain another merged episode
  on a later run, changing its own `episode_ids`/`last_seen_at`/
  `max_peak_pct`/recurrence numbers -- but never its `decision_at` or
  forward-outcome numbers, since neither depends on `last_seen_at`.
- **Reproducibility.** `input_fingerprint` covers both `episodes` and
  `identity_observations` (the two things a re-run's exchange/symbol
  choice actually depends on) -- but forward-outcome NUMBERS additionally
  depend on live OHLCV fetched during the run itself and on
  `momentum_universe_snapshots`' own state at run time, neither of which
  is (or sensibly could be) captured by a fingerprint computed before any
  fetch happens: a re-run against the identical `input_fingerprint` can
  still see different, newer market/snapshot data than an earlier run did.
  This is disclosed, not hidden -- an identical fingerprint proves the
  same regime population and identity resolution, not byte-identical
  forward numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Any

from .exchange_registry import EXCHANGE_FACTORIES
from .market_path_cache import MarketPathCacheCorruptError, MarketPathCacheWriteError
from .momentum_universe_identity_repository import MomentumUniverseIdentityRepository
from .ohlcv import TIMEFRAME_MS, fetch_symbol_candles
from .pump_recurrence_integrity_report import (
    Episode,
    PumpRecurrenceIntegrityFilters,
    Regime,
    SourceIdentityObservation,
    identity_reason,
    merge_episodes_into_regimes,
)
from .pump_recurrence_integrity_repository import PumpRecurrenceIntegrityRepository
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime
from .serial_pump_regimes import (
    HORIZONS_MINUTES,
    REGIME_COOLDOWN_MINUTES,
    SERIAL_PUMP_REGIMES_VERSION,
    HorizonOutcome,
    RecurrenceSummary,
    decision_boundary_ms,
    recurrence_summary,
    resolve_horizon_outcome,
)

if TYPE_CHECKING:
    from .token_universe_coverage import AsOfCoverage

# Liquidity-ordered exchange preference for picking which venue's OHLCV to
# fetch for a regime, when app.pump_event_sources recorded more than one --
# a disclosed simplification of apps/api-gateway/internal/pumps/handler.go's
# own ohlcvPriority; see this module's own docstring ("Canonical identity")
# for why this does not fetch a live volume ranking the way the Go side
# does.
EXCHANGE_OHLCV_PRIORITY: tuple[str, ...] = (
    "binance",
    "bybit",
    "okx",
    "gate",
    "bingx",
    "mexc",
    "xt",
    "lbank",
    "bitget",
    "kucoin",
    "coinex",
    "phemex",
    "cryptocom",
    "htx",
    "bitmart",
    "toobit",
    "blofin",
)

# The canonical, most liquid BTC price source for market-adjusted return --
# no existing convention in this package to reuse (this is the first
# BTC-adjustment computation here), chosen for the same liquidity reasoning
# as EXCHANGE_OHLCV_PRIORITY's own ordering. A literal unified symbol, not
# reconstructed from a base ticker -- BTC's own perpetual is not at
# meaningful risk of the relisting/collision case this module otherwise
# guards against, but using fetch_symbol_candles here too keeps every OHLCV
# fetch in this module going through the same identity-safe path.
BTC_ADJUSTMENT_EXCHANGE = "binance"
BTC_ADJUSTMENT_SYMBOL = "BTC/USDT:USDT"

# research/token-universe-coverage-v1's own snapshot history only covers
# these two exchanges -- venue_expansion is reported per this list, never
# guessed for any other exchange.
VENUE_EXPANSION_EXCHANGES: tuple[str, ...] = ("bybit", "binance")
VENUE_EXPANSION_HORIZON_MINUTES = HORIZONS_MINUTES[-1][1]  # 30d, the report's own longest horizon

# How stale an instruments_as_of snapshot may be and still count as
# evidence for venue_expansion -- no codebase-wide default (see
# AsOfCoverage.is_usable's own reasoning); chosen generously here since
# momentum_universe_snapshots is sparse by construction (capture-restart
# cadence, not a fixed schedule) and this is a discovery report, not a
# formal contract.
VENUE_EXPANSION_MAX_STALENESS = timedelta(days=14)

DEFAULT_CONCURRENCY = 4


@dataclass(frozen=True)
class SerialPumpRegimesFilters:
    since: datetime | None = None
    until: datetime | None = None
    bases: tuple[str, ...] = ()  # optional allow-list -- bounded/sample runs

    def __post_init__(self) -> None:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("--since must be earlier than --until")


@dataclass(frozen=True)
class ResolvedIdentity:
    """One regime's own unambiguous identity on one exchange -- every
    SourceIdentityObservation recorded for that (regime, exchange) pair
    agreed on identity_key and unified_symbol, and none flagged
    identity_conflict. See _resolve_regime_identities."""

    exchange: str
    identity_key: str
    unified_symbol: str


@dataclass(frozen=True)
class VenueExpansionEntry:
    """ready_before/ready_after are None exactly when there is no
    admissibly-fresh momentum_universe snapshot evidence for that instant,
    or (ready_after only) when after_at is still in the future as of this
    run's own evaluation_at -- never a guessed False, and never today's
    current state presented as if it were known at a not-yet-occurred
    instant. expanded is None whenever either side is unknown, True only
    when the base genuinely went from not-ready to ready on this exchange
    between the two instants.

    match_basis records which evidence tier produced ready_before/
    ready_after (colleague review, 2026-09-01, round 2): "identity_key"
    when this regime already had a resolved canonical identity on this
    exchange (from its own pump-detection sources) to match against
    `AsOfCoverage.identity_keys`; "ticker_fallback" when no such source
    identity existed for this exchange at all, so a reconstructed
    `base.upper() + "USDT"` was matched against `AsOfCoverage.
    native_market_ids` instead. The fallback exists because requiring a
    pre-existing source identity structurally EXCLUDES the exact case
    "venue expansion" is meant to detect: a base that was NOT observed on
    this exchange before (no pump ever detected there) genuinely has no
    source-derived identity_key for it, yet that is precisely the "did it
    newly appear here" question this field answers. A ticker_fallback
    match carries the same residual ticker-collision risk this codebase's
    identity system otherwise guards against; an identity_key match does
    not. None when neither side of `expanded` used a match (both None)."""

    exchange: str
    ready_before: bool | None
    ready_after: bool | None
    after_at_matured: bool
    match_basis: str | None

    @property
    def expanded(self) -> bool | None:
        if self.ready_before is None or self.ready_after is None:
            return None
        return (not self.ready_before) and self.ready_after


@dataclass(frozen=True)
class RegimeRow:
    base: str
    episode_ids: tuple[int, ...]
    first_seen_at: datetime
    last_seen_at: datetime
    max_peak_pct: float
    decision_at: datetime
    regime_mature: bool
    ohlcv_exchange: str | None
    ohlcv_symbol: str | None
    ohlcv_unresolved_reason: str | None
    horizons: tuple[HorizonOutcome, ...]
    recurrence: RecurrenceSummary
    next_regime_same_asset: bool | None
    venue_expansion: tuple[VenueExpansionEntry, ...]
    venue_expansion_unresolved_reason: str | None
    delisted: bool | None


@dataclass(frozen=True)
class HorizonPopulationSummary:
    horizon_label: str
    resolved_count: int
    unresolved_counts: dict[str, int] = field(default_factory=dict)
    median_forward_return_pct: float | None = None
    median_btc_adjusted_return_pct: float | None = None
    median_mfe_pct: float | None = None
    median_mae_pct: float | None = None


@dataclass(frozen=True)
class PopulationSummary:
    total_bases: int
    total_regimes: int
    regimes_with_no_ohlcv_source: int
    horizons: tuple[HorizonPopulationSummary, ...]


@dataclass(frozen=True)
class SerialPumpRegimesReport:
    report_version: str
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    input_fingerprint: str
    filters: SerialPumpRegimesFilters
    population: PopulationSummary
    regimes: tuple[RegimeRow, ...]


def _resolve_regime_identities(
    episode_ids: tuple[int, ...],
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]],
) -> dict[str, ResolvedIdentity | None]:
    """One entry per exchange that recorded ANY source observation across
    the given `episode_ids`. `episode_ids` is the caller's responsibility
    to restrict to whatever should actually count as evidence -- see
    `_available_identity_episode_ids` for the "not a future-known route"
    restriction this report's own caller applies before calling this.

    The value is a ResolvedIdentity if and only if EVERY observation for
    that exchange is itself individually clean per `identity_reason`
    (reused from `pump_recurrence_integrity_report.py` rather than
    reimplementing a second, weaker check here -- colleague review,
    2026-09-01, round 2: an earlier version here only checked
    `identity_conflict` plus key/symbol presence via a set comprehension
    that silently dropped a `None` entry from a mixed
    `{"k1", None}`-shaped group instead of treating it as evidence of an
    incomplete observation, and never checked `base_asset` against
    `identity_reason`'s own `base_mismatch` case at all) AND they all
    agree on one `identity_key`/`unified_symbol` -- None otherwise
    (ambiguous/conflicting/incomplete/base-mismatched identity for that
    exchange, fail closed, never guessed). Consumed by both
    `_pick_ohlcv_identity` (which exchange to fetch OHLCV from) and
    `_venue_expansion` (canonical identity_key for the bybit/binance
    readiness check) -- one identity resolution, two consumers, rather
    than two independently-written ones that could drift apart."""
    observations_by_exchange: dict[str, list[SourceIdentityObservation]] = defaultdict(list)
    for event_id in episode_ids:
        for source in sources_by_event.get(event_id, ()):
            observations_by_exchange[source.exchange].append(source)

    resolved: dict[str, ResolvedIdentity | None] = {}
    for exchange, observations in observations_by_exchange.items():
        if any(identity_reason(observation) is not None for observation in observations):
            resolved[exchange] = None
            continue
        # identity_reason(o) is None for every o here guarantees identity_key
        # and unified_symbol are both non-None (see identity_reason's own
        # checks) -- the `is not None` filters below are a type-narrowing
        # safety net, not a masking one, since the loop above already fails
        # closed on any observation that would need it.
        identity_keys = {o.identity_key for o in observations if o.identity_key is not None}
        unified_symbols = {o.unified_symbol for o in observations if o.unified_symbol is not None}
        if len(identity_keys) != 1 or len(unified_symbols) != 1:
            resolved[exchange] = None
            continue
        resolved[exchange] = ResolvedIdentity(
            exchange=exchange,
            identity_key=next(iter(identity_keys)),
            unified_symbol=next(iter(unified_symbols)),
        )
    return resolved


def _available_identity_episode_ids(
    regime: Regime,
    boundary_ms: int,
    episode_by_id: dict[int, Episode],
) -> tuple[int, ...]:
    """The subset of a regime's own episode_ids whose OWN first_seen_at is
    at or before this regime's decision boundary -- i.e. episodes that
    genuinely happened by the time the decision was made, not ones a
    future merge only revealed later.

    Colleague review, 2026-09-01, round 2: `_resolve_regime_identities`
    used to be called with the regime's FULL episode_ids, including
    episodes that occur strictly after `decision_at` (merge_episodes_
    into_regimes can still merge a much-later episode into the same
    regime, within its own cooldown, well after this regime's own
    decision instant already passed). A later episode's own identity
    observation -- e.g. the first time this base was ever detected on
    binance -- would then get used to pick THIS regime's OHLCV exchange
    for a decision instant that predates that observation entirely: a
    "future-known route selection" look-ahead. Restricting to episodes
    already known by `boundary_ms` closes it. `regime.episode_ids` itself
    (recurrence, display) is untouched -- only identity resolution for the
    OHLCV/venue-expansion pick is restricted."""
    return tuple(
        event_id
        for event_id in regime.episode_ids
        if int(episode_by_id[event_id].first_seen_at.timestamp() * 1000) <= boundary_ms
    )


def _identity_overlap(
    a: dict[str, ResolvedIdentity | None], b: dict[str, ResolvedIdentity | None]
) -> bool | None:
    """True iff `a` and `b` share at least one exchange with a matching,
    resolved identity_key -- positive confirmation the two regimes are
    genuinely the same underlying asset, not merely the same ticker.
    False iff they share an exchange but with a DIFFERENT identity_key --
    positive evidence they are NOT the same asset (a genuine relisted-
    ticker collision, exactly the case this checks for). None when there
    is no exchange where BOTH sides have a resolved identity to compare at
    all (cannot confirm or refute either way).

    Colleague review, 2026-09-01, round 2: `merge_episodes_into_regimes`
    (reused, not reimplemented -- see this module's own docstring) groups
    and merges purely by `(base, cooldown)`, with no identity awareness at
    all -- an accepted characteristic of that already-review-hardened,
    shared function, not something this module should silently override
    by changing how regimes themselves are formed. But `recurrence_
    summary`'s own regime_index/regime_count_so_far/next_regime_gap_
    minutes are then reported as if consecutive same-base regimes are
    confirmed to be the same real-world asset recurring, when two
    genuinely different assets that happen to share a ticker (a delisting
    and relisting under the same symbol) would be silently counted as one
    asset's own recurrence history. Rather than rearchitecting regime
    formation itself (out of scope -- would mean modifying the shared
    function's own semantics, used elsewhere), this overlay reports
    per-link identity confirmation so a reader can tell "confirmed same
    asset" apart from "same ticker, identity unconfirmed or refuted" for
    each recurrence link -- see `RegimeRow.next_regime_same_asset`."""
    shared_exchanges = set(a) & set(b)
    saw_comparable = False
    for exchange in shared_exchanges:
        identity_a, identity_b = a.get(exchange), b.get(exchange)
        if identity_a is None or identity_b is None:
            continue
        saw_comparable = True
        if identity_a.identity_key == identity_b.identity_key:
            return True
    if not saw_comparable:
        return None
    return False


def _pick_ohlcv_identity(
    regime_identities: dict[str, ResolvedIdentity | None],
) -> tuple[ResolvedIdentity | None, str | None]:
    """Pure: returns (identity, unresolved_reason). Walks
    EXCHANGE_OHLCV_PRIORITY and returns the highest-priority exchange whose
    regime_identities entry is an unambiguous ResolvedIdentity and has an
    OHLCV-capable client in EXCHANGE_FACTORIES. `ambiguous_identity` is
    returned (rather than `unsupported_exchange`) only when at least one
    prioritized, OHLCV-capable exchange DID have a recorded source but its
    identity could not be disambiguated -- otherwise a report reader could
    not tell "we could have used this exchange, but its own identity data
    was unusable" apart from "nothing here was ever a real candidate"."""
    if not regime_identities:
        return None, "no_identity_observation"
    saw_ambiguous_supported_exchange = False
    for exchange in EXCHANGE_OHLCV_PRIORITY:
        if exchange not in regime_identities or exchange not in EXCHANGE_FACTORIES:
            continue
        identity = regime_identities[exchange]
        if identity is not None:
            return identity, None
        saw_ambiguous_supported_exchange = True
    if saw_ambiguous_supported_exchange:
        return None, "ambiguous_identity"
    return None, "unsupported_exchange"


def compute_input_fingerprint(
    episodes: tuple[Episode, ...],
    identity_observations: tuple[SourceIdentityObservation, ...],
) -> str:
    """sha256 over the exact episode tuples AND identity observations this
    run's own regimes and exchange/symbol choices were built from -- so a
    report can be checked for reproducibility of ITS OWN INPUTS later, same
    convention pump_recurrence_integrity_report.py's own
    compute_input_fingerprint uses. Deliberately does not (and cannot
    meaningfully) cover live-fetched OHLCV or momentum_universe snapshot
    state at run time -- see this module's own docstring, "Reproducibility"."""
    digest = hashlib.sha256()
    for episode in sorted(episodes, key=lambda e: e.event_id):
        closed_at = episode.closed_at.isoformat() if episode.closed_at else ""
        line = (
            f"{episode.event_id}|{episode.base}|{episode.episode}|"
            f"{episode.first_seen_at.isoformat()}|{episode.last_seen_at.isoformat()}|"
            f"{episode.peak_pct}|{closed_at}\n"
        )
        digest.update(line.encode())
    for observation in sorted(
        identity_observations, key=lambda o: (o.event_id, o.exchange, o.unified_symbol or "")
    ):
        obs_line = (
            f"{observation.event_id}|{observation.exchange}|"
            f"{observation.identity_key or ''}|{observation.unified_symbol or ''}|"
            f"{observation.identity_conflict}\n"
        )
        digest.update(obs_line.encode())
    return digest.hexdigest()


def _venue_ready(
    identity: ResolvedIdentity | None,
    base: str,
    coverage: AsOfCoverage,
) -> tuple[bool | None, str | None]:
    """Pure given an already-fetched AsOfCoverage: returns (ready,
    match_basis). Prefers the canonical identity_key match when this
    regime already has a resolved identity on this exchange; otherwise
    falls back to a reconstructed-ticker match against native_market_ids
    -- see VenueExpansionEntry's own docstring for why the fallback exists
    (identity_key alone structurally cannot detect a genuine first-time
    listing on an exchange this regime was never observed on). Returns
    (None, None) when the snapshot itself is not usable -- never guesses
    either way."""
    if not coverage.is_usable(max_staleness=VENUE_EXPANSION_MAX_STALENESS):
        return None, None
    if identity is not None:
        return identity.identity_key in coverage.identity_keys, "identity_key"
    return base.upper() + "USDT" in coverage.native_market_ids, "ticker_fallback"


async def _venue_expansion(
    universe_repository: MomentumUniverseIdentityRepository,
    regime_identities: dict[str, ResolvedIdentity | None],
    base: str,
    boundary_ms: int,
    evaluation_at: datetime,
) -> tuple[VenueExpansionEntry, ...]:
    before_at = datetime.fromtimestamp(boundary_ms / 1000, tz=UTC)
    after_at = before_at + timedelta(minutes=VENUE_EXPANSION_HORIZON_MINUTES)
    after_at_matured = after_at <= evaluation_at
    entries = []
    for exchange in VENUE_EXPANSION_EXCHANGES:
        identity = regime_identities.get(exchange)
        before_coverage = await universe_repository.instruments_as_of(exchange, before_at)
        ready_before, before_basis = _venue_ready(identity, base, before_coverage)

        ready_after: bool | None = None
        after_basis: str | None = None
        if after_at_matured:
            after_coverage = await universe_repository.instruments_as_of(exchange, after_at)
            ready_after, after_basis = _venue_ready(identity, base, after_coverage)

        # ready_before's own basis is reported -- if it used identity_key,
        # ready_after (once matured) necessarily used the same regime's
        # own identity_key too (identity is fixed per regime), so the two
        # never disagree in practice; before_basis is the one attached to
        # the entry since it is always attempted (ready_after may not be,
        # while immature).
        entries.append(
            VenueExpansionEntry(
                exchange, ready_before, ready_after, after_at_matured, before_basis or after_basis
            )
        )
    return tuple(entries)


async def _check_delisted(
    universe_repository: MomentumUniverseIdentityRepository,
    identity: ResolvedIdentity,
    evaluation_at: datetime,
) -> bool | None:
    """Was this regime's own OHLCV-source identity still listed as of this
    run's evaluation_at? True/False only when an admissibly-fresh snapshot
    exists for that exchange at evaluation_at; None otherwise (no evidence
    either way, never a guessed False) -- same AsOfCoverage.is_usable
    discipline research/token-universe-coverage-v1 established. Item 8's
    own text names "delisting" as one of the things to report; this reuses
    the same repository already open for venue_expansion rather than
    adding a second one."""
    coverage = await universe_repository.instruments_as_of(identity.exchange, evaluation_at)
    if not coverage.is_usable(max_staleness=VENUE_EXPANSION_MAX_STALENESS):
        return None
    return identity.identity_key not in coverage.identity_keys


def _empty_horizons(reason: str) -> tuple[HorizonOutcome, ...]:
    return tuple(
        HorizonOutcome(label, False, reason, None, None, None, None, None, None)
        for label, _ in HORIZONS_MINUTES
    )


async def _process_regime(
    *,
    base: str,
    regime: Regime,
    recurrence: RecurrenceSummary,
    next_regime_same_asset: bool | None,
    identity: ResolvedIdentity | None,
    ohlcv_unresolved_reason: str | None,
    regime_identities: dict[str, ResolvedIdentity | None],
    clients: dict[str, Any],
    universe_repository: MomentumUniverseIdentityRepository,
    semaphore: asyncio.Semaphore,
    compute_venue_expansion: bool,
    evaluation_at: datetime,
) -> RegimeRow:
    boundary_ms = decision_boundary_ms(regime, timeframe_ms=TIMEFRAME_MS)
    decision_at = datetime.fromtimestamp(boundary_ms / 1000, tz=UTC)
    regime_mature = (evaluation_at - regime.last_seen_at) >= timedelta(
        minutes=REGIME_COOLDOWN_MINUTES
    )

    def _row(
        *,
        ohlcv_exchange: str | None,
        ohlcv_symbol: str | None,
        ohlcv_unresolved_reason: str | None,
        horizons: tuple[HorizonOutcome, ...],
        venue_expansion: tuple[VenueExpansionEntry, ...],
        venue_expansion_unresolved_reason: str | None,
        delisted: bool | None,
    ) -> RegimeRow:
        return RegimeRow(
            base,
            regime.episode_ids,
            regime.first_seen_at,
            regime.last_seen_at,
            regime.max_peak_pct,
            decision_at,
            regime_mature,
            ohlcv_exchange,
            ohlcv_symbol,
            ohlcv_unresolved_reason,
            horizons,
            recurrence,
            next_regime_same_asset,
            venue_expansion,
            venue_expansion_unresolved_reason,
            delisted,
        )

    if identity is None:
        reason = ohlcv_unresolved_reason or "unsupported_exchange"
        return _row(
            ohlcv_exchange=None,
            ohlcv_symbol=None,
            ohlcv_unresolved_reason=reason,
            horizons=_empty_horizons(reason),
            venue_expansion=(),
            venue_expansion_unresolved_reason=None,
            delisted=None,
        )

    horizon_end_ms = boundary_ms + HORIZONS_MINUTES[-1][1] * 60_000
    try:
        async with semaphore:
            target_candles = tuple(
                await fetch_symbol_candles(
                    clients[identity.exchange],
                    identity.unified_symbol,
                    boundary_ms,
                    horizon_end_ms,
                    use_cache=True,
                )
            )
            btc_candles = tuple(
                await fetch_symbol_candles(
                    clients[BTC_ADJUSTMENT_EXCHANGE],
                    BTC_ADJUSTMENT_SYMBOL,
                    boundary_ms,
                    horizon_end_ms,
                    use_cache=True,
                )
            )
    except (MarketPathCacheCorruptError, MarketPathCacheWriteError):
        # Colleague review, 2026-09-01, round 2: these are NOT ordinary
        # per-regime fetch noise -- market_path_cache.py's own docstrings
        # require both to fail loudly (a corrupt cache entry or a failed
        # write is a systemic infra problem, likely to recur identically
        # across many other regimes in the same run, not something a
        # single "ohlcv_fetch_failed" label should quietly absorb). Let it
        # propagate out of run()'s own asyncio.gather and abort the whole
        # report -- exactly what the generic except below deliberately
        # does NOT do for an ordinary network failure.
        raise
    except Exception:
        # An ordinary per-regime network/exchange failure (timeout,
        # exchange error) must not lose every other regime's already-
        # completed work in an unbounded, thousands-of-regimes run --
        # colleague review, 2026-09-01: this previously propagated
        # straight through asyncio.gather and aborted the whole report.
        # Reported as an explicit unresolved reason, not silently dropped.
        return _row(
            ohlcv_exchange=identity.exchange,
            ohlcv_symbol=identity.unified_symbol,
            ohlcv_unresolved_reason="ohlcv_fetch_failed",
            horizons=_empty_horizons("ohlcv_fetch_failed"),
            venue_expansion=(),
            venue_expansion_unresolved_reason=None,
            delisted=None,
        )

    horizons = tuple(
        resolve_horizon_outcome(
            horizon_label=label,
            horizon_minutes=minutes,
            boundary_ms=boundary_ms,
            timeframe_ms=TIMEFRAME_MS,
            candles=target_candles,
            btc_candles=btc_candles,
        )
        for label, minutes in HORIZONS_MINUTES
    )

    # venue_expansion/delisted are a SEPARATE try/except from the OHLCV
    # fetch above (colleague review, 2026-09-01, round 2): a failure here
    # (a transient momentum_universe_snapshots DB hiccup, unrelated to the
    # OHLCV fetch that already succeeded) must not discard the horizon
    # outcomes just computed, and must not be mislabeled as
    # "ohlcv_fetch_failed" when the OHLCV fetch itself was fine.
    venue_expansion: tuple[VenueExpansionEntry, ...] = ()
    venue_expansion_unresolved_reason: str | None = None
    delisted: bool | None = None
    if compute_venue_expansion:
        try:
            venue_expansion = await _venue_expansion(
                universe_repository, regime_identities, base, boundary_ms, evaluation_at
            )
            delisted = await _check_delisted(universe_repository, identity, evaluation_at)
        except Exception:
            venue_expansion_unresolved_reason = "venue_expansion_failed"

    return _row(
        ohlcv_exchange=identity.exchange,
        ohlcv_symbol=identity.unified_symbol,
        ohlcv_unresolved_reason=None,
        horizons=horizons,
        venue_expansion=venue_expansion,
        venue_expansion_unresolved_reason=venue_expansion_unresolved_reason,
        delisted=delisted,
    )


def _median_resolved(values: list[float | None]) -> float | None:
    """median() over a resolved HorizonOutcome's own numeric field --
    resolved=True guarantees every field here is non-None (see
    resolve_horizon_outcome's own contract), but that guarantee lives at
    the dataclass level, not the type checker's, so this asserts it
    explicitly rather than silently narrowing float | None -> float."""
    present = []
    for value in values:
        assert value is not None, "resolved HorizonOutcome must carry every numeric field"
        present.append(value)
    return median(present) if present else None


def _summarize_horizons(rows: tuple[RegimeRow, ...]) -> tuple[HorizonPopulationSummary, ...]:
    summaries = []
    for label, _minutes in HORIZONS_MINUTES:
        outcomes = [
            outcome for row in rows for outcome in row.horizons if outcome.horizon_label == label
        ]
        resolved = [outcome for outcome in outcomes if outcome.resolved]
        unresolved_counts: dict[str, int] = defaultdict(int)
        for outcome in outcomes:
            if not outcome.resolved and outcome.unresolved_reason is not None:
                unresolved_counts[outcome.unresolved_reason] += 1
        summaries.append(
            HorizonPopulationSummary(
                horizon_label=label,
                resolved_count=len(resolved),
                unresolved_counts=dict(unresolved_counts),
                median_forward_return_pct=_median_resolved(
                    [o.forward_return_pct for o in resolved]
                ),
                median_btc_adjusted_return_pct=_median_resolved(
                    [o.btc_adjusted_return_pct for o in resolved]
                ),
                median_mfe_pct=_median_resolved([o.mfe_pct for o in resolved]),
                median_mae_pct=_median_resolved([o.mae_pct for o in resolved]),
            )
        )
    return tuple(summaries)


async def run(
    *,
    database_url: str,
    filters: SerialPumpRegimesFilters,
    code_revision: str,
    working_tree_dirty: bool,
    concurrency: int = DEFAULT_CONCURRENCY,
    compute_venue_expansion: bool = True,
    evaluation_at: datetime | None = None,
) -> SerialPumpRegimesReport:
    if concurrency < 1:
        # A Semaphore(0) would block every acquire forever -- colleague
        # review, 2026-09-01: this previously let a bad --concurrency value
        # reach asyncio.Semaphore directly and hang the whole run silently.
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    evaluation_at = evaluation_at or datetime.now(UTC)

    recurrence_repository = PumpRecurrenceIntegrityRepository.from_url(database_url)
    try:
        episodes, identity_observations = await recurrence_repository.load(
            PumpRecurrenceIntegrityFilters(since=filters.since, until=filters.until)
        )
    finally:
        await recurrence_repository.close()

    if filters.bases:
        allowed = set(filters.bases)
        episodes = tuple(episode for episode in episodes if episode.base in allowed)
        # Colleague review, 2026-09-01, round 2: identity_observations used
        # to stay unfiltered here even after episodes was narrowed by
        # --base, so a bounded run's own input_fingerprint kept changing
        # whenever any UNRELATED base's identity observations changed --
        # defeating "same restricted input -> same fingerprint". Narrow to
        # exactly the retained episodes' own event_ids.
        retained_event_ids = {episode.event_id for episode in episodes}
        identity_observations = tuple(
            observation
            for observation in identity_observations
            if observation.event_id in retained_event_ids
        )

    episode_by_id: dict[int, Episode] = {episode.event_id: episode for episode in episodes}

    by_event_lists: dict[int, list[SourceIdentityObservation]] = defaultdict(list)
    for observation in identity_observations:
        by_event_lists[observation.event_id].append(observation)
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        event_id: tuple(obs) for event_id, obs in by_event_lists.items()
    }

    episodes_by_base: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        episodes_by_base[episode.base].append(episode)

    cooldown = timedelta(minutes=REGIME_COOLDOWN_MINUTES)
    regimes_by_base: dict[str, tuple[Regime, ...]] = {
        base: merge_episodes_into_regimes(
            tuple(sorted(base_episodes, key=lambda e: e.first_seen_at)), cooldown
        )
        for base, base_episodes in episodes_by_base.items()
    }

    # One identity resolution + OHLCV pick per regime, computed up front so
    # both the client set (needed_exchanges) and each regime's own task can
    # reuse the same resolved identities rather than re-deriving them.
    # Identity resolution is restricted to episodes already known by each
    # regime's own decision boundary (_available_identity_episode_ids) --
    # see that function's own docstring for the look-ahead this closes.
    regime_identities_by_key: dict[
        tuple[str, tuple[int, ...]], dict[str, ResolvedIdentity | None]
    ] = {}
    exchange_choice: dict[
        tuple[str, tuple[int, ...]], tuple[ResolvedIdentity | None, str | None]
    ] = {}
    needed_exchanges: set[str] = {BTC_ADJUSTMENT_EXCHANGE}
    for base, regimes in regimes_by_base.items():
        for regime in regimes:
            key = (base, regime.episode_ids)
            boundary_ms = decision_boundary_ms(regime, timeframe_ms=TIMEFRAME_MS)
            available_episode_ids = _available_identity_episode_ids(
                regime, boundary_ms, episode_by_id
            )
            regime_identities = _resolve_regime_identities(available_episode_ids, sources_by_event)
            regime_identities_by_key[key] = regime_identities
            identity, reason = _pick_ohlcv_identity(regime_identities)
            exchange_choice[key] = (identity, reason)
            if identity is not None:
                needed_exchanges.add(identity.exchange)

    clients: dict[str, Any] = {
        exchange: EXCHANGE_FACTORIES[exchange]() for exchange in needed_exchanges
    }
    universe_repository = MomentumUniverseIdentityRepository.from_url(database_url)
    semaphore = asyncio.Semaphore(concurrency)

    try:
        tasks = []
        for base, regimes in regimes_by_base.items():
            recurrences = recurrence_summary(regimes)
            # next_regime_same_asset[i] describes the link from regime i to
            # regime i+1 -- see _identity_overlap's own docstring. The last
            # regime for this base has no next link (recurrence_summary's
            # own next_regime_gap_minutes is already None there for the
            # same reason).
            for index, (regime, recurrence) in enumerate(zip(regimes, recurrences, strict=True)):
                key = (base, regime.episode_ids)
                identity, reason = exchange_choice[key]
                next_regime_same_asset: bool | None = None
                if index + 1 < len(regimes):
                    next_key = (base, regimes[index + 1].episode_ids)
                    next_regime_same_asset = _identity_overlap(
                        regime_identities_by_key[key], regime_identities_by_key[next_key]
                    )
                tasks.append(
                    _process_regime(
                        base=base,
                        regime=regime,
                        recurrence=recurrence,
                        next_regime_same_asset=next_regime_same_asset,
                        identity=identity,
                        ohlcv_unresolved_reason=reason,
                        regime_identities=regime_identities_by_key[key],
                        clients=clients,
                        universe_repository=universe_repository,
                        semaphore=semaphore,
                        compute_venue_expansion=compute_venue_expansion,
                        evaluation_at=evaluation_at,
                    )
                )
        rows = tuple(await asyncio.gather(*tasks))
    finally:
        for client in clients.values():
            with suppress(Exception):
                await client.close()
        await universe_repository.close()

    population = PopulationSummary(
        total_bases=len(regimes_by_base),
        total_regimes=len(rows),
        regimes_with_no_ohlcv_source=sum(1 for row in rows if row.ohlcv_exchange is None),
        horizons=_summarize_horizons(rows),
    )
    return SerialPumpRegimesReport(
        report_version=SERIAL_PUMP_REGIMES_VERSION,
        generated_at=evaluation_at,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        input_fingerprint=compute_input_fingerprint(episodes, identity_observations),
        filters=filters,
        population=population,
        regimes=rows,
    )


def render_json(report: SerialPumpRegimesReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False) + "\n"


def _fmt_pct(value: float | None) -> str:
    return f"{value:.2f}%" if value is not None else "n/a"


def _fmt_bool_or_none(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _fmt_venue_expansion(entries: tuple[VenueExpansionEntry, ...]) -> str:
    if not entries:
        return "n/a"
    parts = []
    for entry in entries:
        basis = f", basis={entry.match_basis}" if entry.match_basis else ""
        maturity = "" if entry.after_at_matured else ", after not matured"
        parts.append(
            f"{entry.exchange}: expanded={_fmt_bool_or_none(entry.expanded)}{basis}{maturity}"
        )
    return "; ".join(parts)


def _fmt_unresolved_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{reason}:{count}" for reason, count in sorted(counts.items()))


def render_markdown(report: SerialPumpRegimesReport) -> str:
    lines = [
        f"# Serial pump regimes discovery ({report.report_version})",
        "",
        "**GROSS returns only -- no spread/fees/slippage/funding. Not a "
        "hold/sell recommendation on its own; see this report's own module "
        "docstring for what an after-cost read would still need.**",
        "",
        f"Generated: {report.generated_at.isoformat()} | "
        f"Revision: {report.code_revision}"
        f"{' (dirty)' if report.working_tree_dirty else ''} | "
        f"Input fingerprint: {report.input_fingerprint[:12]}",
        "",
        f"Bases: {report.population.total_bases} | "
        f"Regimes: {report.population.total_regimes} | "
        f"Regimes with no OHLCV source: {report.population.regimes_with_no_ohlcv_source}",
        "",
        "## Per-horizon population summary",
        "",
    ]
    horizon_rows = [
        (
            summary.horizon_label,
            summary.resolved_count,
            _fmt_pct(summary.median_forward_return_pct),
            _fmt_pct(summary.median_btc_adjusted_return_pct),
            _fmt_pct(summary.median_mfe_pct),
            _fmt_pct(summary.median_mae_pct),
            _fmt_unresolved_counts(summary.unresolved_counts),
        )
        for summary in report.population.horizons
    ]
    lines.extend(
        markdown_table(
            (
                "horizon",
                "resolved",
                "median return",
                "median btc-adj",
                "median mfe",
                "median mae",
                "unresolved reasons",
            ),
            horizon_rows,
        )
    )
    lines.append("")

    lines.append("## Regimes")
    lines.append("")
    regime_rows = [
        (
            row.base,
            len(row.episode_ids),
            row.decision_at.isoformat(),
            "yes" if row.regime_mature else "no",
            row.ohlcv_exchange or f"none ({row.ohlcv_unresolved_reason})",
            f"{row.recurrence.regime_index + 1}/{row.recurrence.regime_count_so_far}",
            f"{row.recurrence.next_regime_gap_minutes:.1f}m"
            if row.recurrence.next_regime_gap_minutes is not None
            else "-",
            _fmt_bool_or_none(row.next_regime_same_asset),
            _fmt_bool_or_none(row.delisted),
            row.venue_expansion_unresolved_reason or _fmt_venue_expansion(row.venue_expansion),
        )
        for row in report.regimes
    ]
    lines.extend(
        markdown_table(
            (
                "base",
                "episodes",
                "decision_at",
                "mature",
                "ohlcv source",
                "recurrence #",
                "next gap",
                "next same asset",
                "delisted",
                "venue expansion",
            ),
            regime_rows,
        )
    )
    lines.append("")

    lines.append("## Horizon detail (resolved only -- see JSON for unresolved reasons per regime)")
    lines.append("")
    detail_rows = [
        (
            row.base,
            ",".join(str(eid) for eid in row.episode_ids),
            horizon.horizon_label,
            _fmt_pct(horizon.forward_return_pct),
            _fmt_pct(horizon.btc_adjusted_return_pct),
            _fmt_pct(horizon.mfe_pct),
            _fmt_pct(horizon.mae_pct),
            f"{horizon.time_to_peak_minutes:.1f}m"
            if horizon.time_to_peak_minutes is not None
            else "n/a",
            _fmt_pct(horizon.retrace_magnitude_pct),
        )
        for row in report.regimes
        for horizon in row.horizons
        if horizon.resolved
    ]
    lines.extend(
        markdown_table(
            (
                "base",
                "episode_ids",
                "horizon",
                "return",
                "btc-adj",
                "mfe",
                "mae",
                "time-to-peak",
                "retrace magnitude",
            ),
            detail_rows,
        )
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"--concurrency must be >= 1, got {parsed}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discovery-only forward-outcome read across every independent pump regime. "
            "GROSS returns only (no spread/fees/slippage/funding) -- not a hold/sell "
            "recommendation; see this module's own docstring."
        )
    )
    parser.add_argument("--since", type=parse_utc_datetime, help="inclusive UTC ISO-8601 cutoff")
    parser.add_argument("--until", type=parse_utc_datetime, help="exclusive UTC ISO-8601 cutoff")
    parser.add_argument(
        "--base",
        action="append",
        dest="bases",
        help="restrict to this base; repeat for a bounded/sample run (default: every base)",
    )
    parser.add_argument("--concurrency", type=_positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--no-venue-expansion",
        action="store_true",
        help=(
            "skip the momentum_universe reads (faster; omits venue_expansion "
            "AND delisted, since both read the same snapshot history)"
        ),
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA", ""))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for serial-pump-regimes-report")
    code_revision = normalize_code_revision(args.code_revision) if args.code_revision else "unknown"
    filters = SerialPumpRegimesFilters(
        since=args.since, until=args.until, bases=tuple(args.bases or ())
    )
    report = await run(
        database_url=db_url,
        filters=filters,
        code_revision=code_revision,
        working_tree_dirty=args.working_tree_dirty,
        concurrency=args.concurrency,
        compute_venue_expansion=not args.no_venue_expansion,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
