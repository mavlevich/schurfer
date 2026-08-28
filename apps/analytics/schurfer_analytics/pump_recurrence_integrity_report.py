"""Discovery-only integrity audit for `app.pump_events` recurrence: research/
pump-recurrence-integrity-audit-v1, Phase 0 ahead of any precursor/serial-pump
research (see docs/research/discovery-ledger.md HYP-013's own confirmation_
requirement, which this report's population-level output feeds).

## Why this exists

Eyeballing `app.pump_events` per base (raw episode counts, `peak_pct`) invites
two concrete errors, both confirmed against the actual detector/scanner code
before writing this report, not assumed:

1. **Episode count is not independent-event count.** `SCAN_INTERVAL` defaults
   to 60s (`config.py`) and an episode closes after `PUMP_CLOSE_AFTER_MISSES`
   (default 3) consecutive missed scans -- `persistence.py`'s `close_retrace`.
   A single sustained move that dips below the entry threshold and
   re-crosses it minutes later reopens as a *new* episode. A base with 122
   raw `pump_events` rows may be a handful of independent price regimes
   fragmented by detector re-arming, not 122 independent recurrences. This
   report's `fragmentation_ratio` (raw episodes / independent regimes after
   merging with a cooldown gap) is the number that actually answers "how
   often does this base independently recur."
2. **`peak_pct` is not compoundable.** `persistence.py`'s `_high_24h_pct`
   computes a rolling 24h high relative to a *reconstructed* 24h-ago open
   (`open_24h = price / (1 + change_pct / 100)`), independently per episode.
   It is not a return between episode boundaries, and summing or chaining it
   across episodes overstates cumulative appreciation. This report never
   sums `peak_pct` across episodes; `max_peak_pct` per regime is reported
   as-is, labeled for what it is.

A third, orthogonal problem: `scanner.py`'s `_dedup` groups entries purely by
`base` string across every exchange ("Group entries by base asset, keep all
exchanges"). Two structurally different failure modes follow, and this
report checks for both using `app.pump_event_sources`' own already-populated
identity columns (`identity_key`/`market_id`/`market_type`/`onboarded_at`/
`identity_conflict`, added in migration 0014, filled incrementally by
`persistence.py`'s UPSERT since 2026-07-23 -- not new capture, reused as-is):

- **`shared_identity_key_across_bases`** and **`shared_base_asset_across_bases`**:
  the same canonical instrument (or, failing an exact `identity_key` match,
  the same exchange-reported currency code) resolves under two different
  `base` strings -- the direct integrity check for the exact "牛来 vs
  NIULAI" ambiguity the discussion that motivated this report ran into.
- **`base_maps_to_multiple_instruments_on_one_exchange`**: the same `base`
  on the same exchange resolves to more than one distinct instrument across
  its own episodes (a relist, redenomination, or contract change on that
  one venue) -- the scanner's per-base aggregation silently mixes these
  into one row today.

**Colleague review (2026-08-28) caught two real gaps, both fixed:**

1. The first version excluded any observation with `identity_reason() ==
   "base_mismatch"` from collision detection entirely -- but a real alias
   (exactly the 牛来/NIULAI shape: the event's own `base` label disagrees
   with the exchange's reported `base_asset`) *is* a `base_mismatch` by
   construction. Excluding it made the detector structurally blind to the
   one scenario it exists to catch, while still reporting a reassuring
   "0 collisions found". `identity_reason()` is unchanged (still used to
   report how well a single observation resolves), but collision detection
   now uses the looser `_identity_usable_for_collisions()` (excludes only
   `identity_conflict`/`missing_identity`, keeps `base_mismatch` observations
   in the grouping pool) plus the new `shared_base_asset_across_bases` kind.
2. Two bases that never share a common exchange (e.g. 牛来 seen only on
   `gate`, NIULAI only on `bingx`/`lbank`/`mexc`) cannot be compared by any
   mechanism here: `identity_key` is venue-prefixed and `base_asset`
   grouping is per-exchange. Reporting "0 collisions" for such a pair would
   silently read as proof they are different instruments, when it is
   actually "this report has no comparable observation at all". `--case-
   study-cross-check` bases are now checked pairwise for exactly this via
   `find_cross_venue_unresolved_pairs`, surfaced as its own report section
   -- absence from `identity_collisions` never stands alone as evidence of
   non-collision for a pair that also appears in `cross_venue_unresolved`.

Identity classification (`identity_reason`) still reuses `source_lead.py`'s
`_identity_reason` discipline, generalized: pump events are not limited to
USDT swaps the way source-lead routes are, so the swap/USDT-only checks are
dropped and only the universal checks are kept (conflict flag, key/symbol
presence, base match).

## Coverage and reproducibility, disclosed rather than assumed

`identity_observations_statement` joins from `pump_event_sources`, so an
event with zero source rows (pre-attribution-capture history, or an
exchange this report does not query) is invisible to that query outright --
not resolved, not unresolved, just absent. `population.
events_without_source_observations` counts exactly this gap by comparing
every event's own id against the identity-observation event ids, and
`population.identity_audit_incomplete` is `True` whenever that count is
nonzero, so a caller cannot read a clean "0 unresolved, 0 collisions" as
full coverage when it is not.

`--since`/`--until` bound which events are included by `first_seen_at`; they
do **not** freeze `last_seen_at`/`peak_pct`/identity fields at the cutoff --
those are read as their current, possibly-since-mutated values. This is a
current-state audit, not a point-in-time reconstruction: re-running the same
filters on a different day can produce different numbers for episodes that
were still open (or whose identity was still stabilizing) at the nominal
`--until` boundary. `population.episodes_open_at_cutoff` counts episodes
whose `last_seen_at` had not yet closed as of `--until` (0 when `--until`
is unset), and `input_fingerprint` (SHA-256 over the exact episode and
identity rows this run actually saw) makes a changed re-run detectable
rather than silently different.

## What this report deliberately does not claim

No statistical test, no p-value, no Holm correction, no trading verdict.
Nothing here selects a threshold or a candidate feature. `CASE_STUDY_BASES`
(the tickers that originally motivated this report) are surfaced separately,
for readability only -- population summary statistics are computed over
every base with at least one `pump_events` row, never over just the case
studies, precisely to avoid the survivorship-bias trap of characterizing
"what these winners have in common" without the denominator of bases that
never recurred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any

from .reporting import (
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)

PUMP_RECURRENCE_INTEGRITY_REPORT_VERSION = "pump_recurrence_integrity_report_v2"

# Named in the discussion that motivated this report. Case studies only --
# never a source of population statistics or thresholds. 牛来 and NIULAI are
# both included deliberately: the report's own collision/cross-venue checks
# are what decide whether they are comparable, not this list.
CASE_STUDY_BASES: tuple[str, ...] = (
    "BTR",
    "CATE",
    "ONG",
    "GME1",
    "MOVR",
    "JIMOTHY",
    "牛来",
    "NIULAI",
)

REGIME_COOLDOWNS: tuple[tuple[str, timedelta], ...] = (
    ("24h", timedelta(hours=24)),
    ("48h", timedelta(hours=48)),
)

_INTERVAL_BOUNDS: tuple[tuple[str, timedelta], ...] = (
    ("under_5m", timedelta(minutes=5)),
    ("under_1h", timedelta(hours=1)),
    ("under_24h", timedelta(hours=24)),
    ("d1_to_7d", timedelta(days=7)),
)


@dataclass(frozen=True)
class PumpRecurrenceIntegrityFilters:
    since: datetime | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("--since must be earlier than --until")


@dataclass(frozen=True)
class Episode:
    event_id: int
    base: str
    episode: int
    first_seen_at: datetime
    last_seen_at: datetime
    peak_pct: float
    closed_at: datetime | None


@dataclass(frozen=True)
class Regime:
    """One independent price regime: one or more raw detector episodes merged
    because the gap between them was shorter than the regime's cooldown."""

    base: str
    episode_ids: tuple[int, ...]
    first_seen_at: datetime
    last_seen_at: datetime
    max_peak_pct: float


@dataclass(frozen=True)
class BaseFragmentation:
    base: str
    raw_episode_count: int
    regime_counts: dict[str, int]  # cooldown label -> independent regime count
    fragmentation_ratios: dict[str, float]  # cooldown label -> raw / regimes
    interval_histogram: dict[str, int]
    first_seen_at: datetime
    last_seen_at: datetime
    max_peak_pct: float


@dataclass(frozen=True)
class SourceIdentityObservation:
    event_id: int
    base: str
    exchange: str
    identity_key: str | None
    unified_symbol: str | None
    base_asset: str | None
    identity_conflict: bool


@dataclass(frozen=True)
class IdentityCollision:
    kind: str  # "shared_identity_key_across_bases" | "shared_base_asset_across_bases"
    # | "base_maps_to_multiple_instruments_on_one_exchange"
    bases: tuple[str, ...]
    identity_keys: tuple[str, ...]
    exchanges: tuple[str, ...]


@dataclass(frozen=True)
class CrossVenueUnresolvedPair:
    """Two bases that share no common exchange with a usable identity
    observation between them, so neither `shared_identity_key_across_bases`
    nor `shared_base_asset_across_bases` could ever fire for this pair --
    `identity_key` is venue-prefixed and `base_asset` grouping is per-
    exchange, both structurally require a common exchange to compare on.
    Absence from `identity_collisions` is not evidence these are different
    instruments when the pair also appears here."""

    bases: tuple[str, str]
    first_exchanges: tuple[str, ...]
    second_exchanges: tuple[str, ...]


@dataclass(frozen=True)
class PopulationSummary:
    total_bases: int
    total_raw_episodes: int
    median_fragmentation_ratio_24h: float
    p90_fragmentation_ratio_24h: float
    max_fragmentation_ratio_24h: float
    resolved_identity_observations: int
    unresolved_identity_observations: int
    events_without_source_observations: int
    identity_collision_count: int
    episodes_open_at_cutoff: int
    identity_audit_incomplete: bool


@dataclass(frozen=True)
class PumpRecurrenceIntegrityReport:
    report_version: str
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    input_fingerprint: str
    filters: PumpRecurrenceIntegrityFilters
    population: PopulationSummary
    fragmentation_by_base: tuple[BaseFragmentation, ...]
    identity_collisions: tuple[IdentityCollision, ...]
    cross_venue_unresolved: tuple[CrossVenueUnresolvedPair, ...] = field(default_factory=tuple)
    case_studies: tuple[BaseFragmentation, ...] = field(default_factory=tuple)


def classify_interval(gap: timedelta) -> str:
    if gap < timedelta(0):
        return "overlapping"
    for label, bound in _INTERVAL_BOUNDS:
        if gap < bound:
            return label
    return "over_7d"


def interval_histogram(episodes: tuple[Episode, ...]) -> dict[str, int]:
    """Gaps between consecutive RAW detector episodes (already first_seen_at-
    sorted, single base) -- deliberately raw-to-raw, not regime-to-regime:
    this is descriptive evidence for *why* merging is needed, not itself the
    merge, so it must not share `merge_episodes_into_regimes`' running-
    maximum bookkeeping. `overlapping` (a negative gap) is its own bucket
    rather than folded into `under_5m`, since a negative gap signals a
    data-ordering anomaly worth surfacing on its own, not ordinary rapid
    recurrence.
    """
    counts: dict[str, int] = defaultdict(int)
    for prev, curr in pairwise(episodes):
        counts[classify_interval(curr.first_seen_at - prev.last_seen_at)] += 1
    return dict(counts)


def merge_episodes_into_regimes(
    episodes: tuple[Episode, ...], cooldown: timedelta
) -> tuple[Regime, ...]:
    """Merge one base's chronologically-ordered detector episodes into
    independent price regimes: consecutive episodes merge into one regime
    when the gap between the regime's own running-maximum `last_seen_at` so
    far and the next episode's `first_seen_at` is shorter than `cooldown`.

    Compares against the regime's running maximum, not the immediately
    preceding raw episode's own `last_seen_at` (colleague review, 2026-08-28):
    for overlapping/nested episodes -- a short episode entirely contained
    inside a longer one that started earlier -- comparing against the
    immediately preceding episode alone can measure a gap from a `last_seen_at`
    earlier than the regime's true extent, incorrectly splitting one regime
    into two.

    `episodes` must already be sorted by `first_seen_at` ascending and share
    one `base` -- the caller's job. Both invariants are checked and raise
    loudly rather than silently producing a wrong merge.
    """
    if not episodes:
        return ()
    base = episodes[0].base
    for prev, curr in pairwise(episodes):
        if curr.base != base:
            raise ValueError("merge_episodes_into_regimes requires a single base")
        if curr.first_seen_at < prev.first_seen_at:
            raise ValueError("merge_episodes_into_regimes requires first_seen_at-sorted input")

    regimes: list[Regime] = []
    ids = [episodes[0].event_id]
    first = episodes[0].first_seen_at
    last = episodes[0].last_seen_at
    peak = episodes[0].peak_pct

    for _prev, curr in pairwise(episodes):
        if curr.first_seen_at - last < cooldown:
            ids.append(curr.event_id)
            last = max(last, curr.last_seen_at)
            peak = max(peak, curr.peak_pct)
        else:
            regimes.append(Regime(base, tuple(ids), first, last, peak))
            ids = [curr.event_id]
            first = curr.first_seen_at
            last = curr.last_seen_at
            peak = curr.peak_pct

    regimes.append(Regime(base, tuple(ids), first, last, peak))
    return tuple(regimes)


def compute_base_fragmentation(episodes: tuple[Episode, ...]) -> BaseFragmentation:
    if not episodes:
        raise ValueError("compute_base_fragmentation requires at least one episode")
    ordered = tuple(sorted(episodes, key=lambda episode: episode.first_seen_at))
    regime_counts: dict[str, int] = {}
    fragmentation_ratios: dict[str, float] = {}
    for label, cooldown in REGIME_COOLDOWNS:
        regimes = merge_episodes_into_regimes(ordered, cooldown)
        regime_counts[label] = len(regimes)
        fragmentation_ratios[label] = len(ordered) / len(regimes) if regimes else 0.0
    return BaseFragmentation(
        base=ordered[0].base,
        raw_episode_count=len(ordered),
        regime_counts=regime_counts,
        fragmentation_ratios=fragmentation_ratios,
        interval_histogram=interval_histogram(ordered),
        first_seen_at=ordered[0].first_seen_at,
        last_seen_at=max(episode.last_seen_at for episode in ordered),
        max_peak_pct=max(episode.peak_pct for episode in ordered),
    )


def identity_reason(observation: SourceIdentityObservation) -> str | None:
    """Fail-closed classification reusing `source_lead.py`'s
    `_identity_reason` discipline, generalized for pump events (not limited
    to USDT swaps the way source-lead routes are, so the swap/USDT-only
    checks are dropped; conflict flag, key/symbol presence, and base match
    are kept as-is). Reports how well ONE observation resolves -- use
    `_identity_usable_for_collisions` for grouping decisions instead; a
    `base_mismatch` observation must stay in the collision-detection pool
    (it is itself the alias signal), even though it is correctly reported
    as not fully resolved here.
    """
    if observation.identity_conflict:
        return "identity_conflict"
    if not observation.identity_key or not observation.unified_symbol:
        return "missing_identity"
    if (observation.base_asset or "").casefold() != observation.base.casefold():
        return "base_mismatch"
    return None


def _identity_usable_for_collisions(observation: SourceIdentityObservation) -> bool:
    """Looser than `identity_reason() is None`: excludes only a genuinely
    uninterpretable observation (conflicting, or missing key/symbol
    outright). A `base_mismatch` observation is kept -- it is the direct
    evidence collision detection exists to find, not a reason to look away
    from it."""
    return (
        not observation.identity_conflict
        and observation.identity_key is not None
        and observation.unified_symbol is not None
    )


def detect_identity_collisions(
    observations: tuple[SourceIdentityObservation, ...],
) -> tuple[IdentityCollision, ...]:
    """Three independent collision checks, deliberately NOT the same grouping:

    - `shared_identity_key_across_bases`: the exact same `identity_key`
      resolves under two different `base` strings. `identity_key` already
      embeds the venue (e.g. "binance:swap:ADAUSDT:..."), so this genuinely
      means one instrument is tracked under two ticker labels.
    - `shared_base_asset_across_bases`: the same exchange reports the same
      currency code (`base_asset`) under two different event `base` labels,
      even when `identity_key` itself did not line up exactly (market_id
      formatting drift, an onboarded_at that has not stabilized yet) -- the
      exact 牛来 vs NIULAI shape: the event's own base disagrees with what
      the exchange calls the asset. This is deliberately NOT gated on
      `identity_reason() is None`; see `_identity_usable_for_collisions`.
    - `base_maps_to_multiple_instruments_on_one_exchange`: the SAME base on
      the SAME exchange resolves to more than one `identity_key` across its
      own episodes -- a relist, redenomination, or contract change on that
      one venue.

    Grouping by `identity_key` alone across every exchange is deliberately
    NOT done: `identity_key`'s venue prefix means a single, correctly
    cross-listed instrument (e.g. ADA on binance/bybit/gate/...) always
    produces a distinct key per exchange by construction. That is normal
    multi-venue listing, not a collision -- comparing across exchanges this
    way was tried and produces hundreds of false positives on the real
    population (confirmed against production data before this report
    shipped). Two bases that never share a common exchange cannot be
    compared by either check here at all -- see
    `find_cross_venue_unresolved_pairs`, which makes that gap explicit
    instead of letting an absent collision read as proof of non-collision.
    """
    usable = [
        observation for observation in observations if _identity_usable_for_collisions(observation)
    ]

    bases_by_key: dict[str, set[str]] = defaultdict(set)
    exchanges_by_key: dict[str, set[str]] = defaultdict(set)
    keys_by_base_exchange: dict[tuple[str, str], set[str]] = defaultdict(set)
    bases_by_exchange_asset: dict[tuple[str, str], set[str]] = defaultdict(set)
    for observation in usable:
        key = observation.identity_key
        if key is None:  # pragma: no cover - _identity_usable_for_collisions already excludes this
            continue
        bases_by_key[key].add(observation.base)
        exchanges_by_key[key].add(observation.exchange)
        keys_by_base_exchange[(observation.base, observation.exchange)].add(key)
        if observation.base_asset:
            bases_by_exchange_asset[(observation.exchange, observation.base_asset.casefold())].add(
                observation.base
            )

    collisions: list[IdentityCollision] = []
    for key, bases in sorted(bases_by_key.items()):
        if len(bases) > 1:
            collisions.append(
                IdentityCollision(
                    kind="shared_identity_key_across_bases",
                    bases=tuple(sorted(bases)),
                    identity_keys=(key,),
                    exchanges=tuple(sorted(exchanges_by_key[key])),
                )
            )
    for (base, exchange), keys in sorted(keys_by_base_exchange.items()):
        if len(keys) > 1:
            collisions.append(
                IdentityCollision(
                    kind="base_maps_to_multiple_instruments_on_one_exchange",
                    bases=(base,),
                    identity_keys=tuple(sorted(keys)),
                    exchanges=(exchange,),
                )
            )
    for (exchange, _asset), bases in sorted(bases_by_exchange_asset.items()):
        if len(bases) > 1:
            collisions.append(
                IdentityCollision(
                    kind="shared_base_asset_across_bases",
                    bases=tuple(sorted(bases)),
                    identity_keys=(),
                    exchanges=(exchange,),
                )
            )
    return tuple(collisions)


def find_cross_venue_unresolved_pairs(
    candidate_bases: tuple[str, ...],
    observations: tuple[SourceIdentityObservation, ...],
) -> tuple[CrossVenueUnresolvedPair, ...]:
    """For every pair within `candidate_bases` that each have at least one
    usable identity observation but share NO common exchange between them,
    report the pair explicitly: `detect_identity_collisions` cannot compare
    them by construction (identity_key is venue-prefixed, base_asset
    grouping is per-exchange), so their absence from `identity_collisions`
    means "not comparable by this method", never "confirmed different
    instruments". A base with zero usable observations anywhere is a
    coverage gap (see `events_without_source_observations`), not reported
    here to avoid conflating the two different failure modes.
    """
    exchanges_by_base: dict[str, set[str]] = defaultdict(set)
    for observation in observations:
        if _identity_usable_for_collisions(observation):
            exchanges_by_base[observation.base].add(observation.exchange)

    pairs: list[CrossVenueUnresolvedPair] = []
    for index, first in enumerate(candidate_bases):
        first_exchanges = exchanges_by_base.get(first, set())
        if not first_exchanges:
            continue
        for second in candidate_bases[index + 1 :]:
            second_exchanges = exchanges_by_base.get(second, set())
            if second_exchanges and not (first_exchanges & second_exchanges):
                pairs.append(
                    CrossVenueUnresolvedPair(
                        bases=(first, second),
                        first_exchanges=tuple(sorted(first_exchanges)),
                        second_exchanges=tuple(sorted(second_exchanges)),
                    )
                )
    return tuple(pairs)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _canonical_episode_row(episode: Episode) -> dict[str, Any]:
    return {
        "event_id": episode.event_id,
        "base": episode.base,
        "episode": episode.episode,
        "first_seen_at": episode.first_seen_at.isoformat(),
        "last_seen_at": episode.last_seen_at.isoformat(),
        "peak_pct": episode.peak_pct,
        "closed_at": episode.closed_at.isoformat() if episode.closed_at else None,
    }


def _canonical_identity_row(observation: SourceIdentityObservation) -> dict[str, Any]:
    return {
        "event_id": observation.event_id,
        "base": observation.base,
        "exchange": observation.exchange,
        "identity_key": observation.identity_key,
        "unified_symbol": observation.unified_symbol,
        "base_asset": observation.base_asset,
        "identity_conflict": observation.identity_conflict,
    }


def compute_input_fingerprint(
    episodes: tuple[Episode, ...],
    identity_observations: tuple[SourceIdentityObservation, ...],
) -> str:
    """SHA-256 over the canonical JSON of the exact episode and identity rows
    this run actually saw. `--since`/`--until` bound which events are
    included by `first_seen_at`; they do not freeze the mutable fields of
    those events at the cutoff (see the module docstring's "Coverage and
    reproducibility" section). A changed fingerprint across two runs with
    identical filters means the underlying rows genuinely changed since,
    which this makes detectable instead of silent.
    """
    payload = {
        "episodes": sorted(
            (_canonical_episode_row(episode) for episode in episodes),
            key=lambda row: row["event_id"],
        ),
        "identity_observations": sorted(
            (_canonical_identity_row(observation) for observation in identity_observations),
            key=lambda row: (row["event_id"], row["exchange"]),
        ),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_report(
    filters: PumpRecurrenceIntegrityFilters,
    episodes: tuple[Episode, ...],
    identity_observations: tuple[SourceIdentityObservation, ...],
    *,
    code_revision: str,
    working_tree_dirty: bool,
    generated_at: datetime | None = None,
) -> PumpRecurrenceIntegrityReport:
    by_base: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        by_base[episode.base].append(episode)

    fragmentation_by_base = tuple(
        sorted(
            (compute_base_fragmentation(tuple(rows)) for rows in by_base.values()),
            key=lambda row: (-row.fragmentation_ratios["24h"], row.base),
        )
    )
    case_studies = tuple(row for row in fragmentation_by_base if row.base in CASE_STUDY_BASES)

    identity_collisions = detect_identity_collisions(identity_observations)
    cross_venue_unresolved = find_cross_venue_unresolved_pairs(
        CASE_STUDY_BASES, identity_observations
    )

    resolved_count = sum(
        1 for observation in identity_observations if identity_reason(observation) is None
    )
    events_with_identity_rows = {observation.event_id for observation in identity_observations}
    all_event_ids = {episode.event_id for episode in episodes}
    events_without_source = len(all_event_ids - events_with_identity_rows)

    ratios_24h = [row.fragmentation_ratios["24h"] for row in fragmentation_by_base]
    episodes_open_at_cutoff = (
        sum(1 for episode in episodes if episode.last_seen_at >= filters.until)
        if filters.until is not None
        else 0
    )
    population = PopulationSummary(
        total_bases=len(fragmentation_by_base),
        total_raw_episodes=len(episodes),
        median_fragmentation_ratio_24h=median(ratios_24h) if ratios_24h else 0.0,
        p90_fragmentation_ratio_24h=_percentile(ratios_24h, 0.9),
        max_fragmentation_ratio_24h=max(ratios_24h) if ratios_24h else 0.0,
        resolved_identity_observations=resolved_count,
        unresolved_identity_observations=len(identity_observations) - resolved_count,
        events_without_source_observations=events_without_source,
        identity_collision_count=len(identity_collisions),
        episodes_open_at_cutoff=episodes_open_at_cutoff,
        identity_audit_incomplete=events_without_source > 0,
    )

    return PumpRecurrenceIntegrityReport(
        report_version=PUMP_RECURRENCE_INTEGRITY_REPORT_VERSION,
        generated_at=generated_at or datetime.now(UTC),
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        input_fingerprint=compute_input_fingerprint(episodes, identity_observations),
        filters=filters,
        population=population,
        fragmentation_by_base=fragmentation_by_base,
        identity_collisions=identity_collisions,
        cross_venue_unresolved=cross_venue_unresolved,
        case_studies=case_studies,
    )


def render_json(report: PumpRecurrenceIntegrityReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def _fragmentation_rows(rows: tuple[BaseFragmentation, ...]) -> list[tuple[Any, ...]]:
    return [
        (
            row.base,
            row.raw_episode_count,
            row.regime_counts["24h"],
            f"{row.fragmentation_ratios['24h']:.1f}x",
            row.regime_counts["48h"],
            f"{row.fragmentation_ratios['48h']:.1f}x",
            f"{row.max_peak_pct:.1f}%",
        )
        for row in rows
    ]


def render_markdown(report: PumpRecurrenceIntegrityReport) -> str:
    scope: list[str] = []
    if report.filters.since:
        scope.append(f"since={report.filters.since.isoformat()}")
    if report.filters.until:
        scope.append(f"until={report.filters.until.isoformat()}")
    lines = [
        "# Pump Recurrence Integrity Audit",
        "",
        "Discovery-only data-quality report. No statistical test, no trading",
        "verdict -- see the module docstring for exactly what this does and does",
        "not claim. Current-state audit, not a point-in-time reconstruction --",
        'see the module docstring\'s "Coverage and reproducibility" section.',
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Code revision: {report.code_revision}"
        + (" (dirty)" if report.working_tree_dirty else ""),
        f"Input fingerprint: {report.input_fingerprint}",
        f"Scope: {', '.join(scope) if scope else 'all time'}",
        "",
        "## Population summary",
        "",
        f"- Total bases: {report.population.total_bases}",
        f"- Total raw episodes: {report.population.total_raw_episodes}",
        f"- Fragmentation ratio (24h cooldown) -- median: "
        f"{report.population.median_fragmentation_ratio_24h:.1f}x, "
        f"p90: {report.population.p90_fragmentation_ratio_24h:.1f}x, "
        f"max: {report.population.max_fragmentation_ratio_24h:.1f}x",
        f"- Identity observations resolved: {report.population.resolved_identity_observations} "
        f"(unresolved: {report.population.unresolved_identity_observations})",
        f"- Events with zero source rows (invisible to identity audit): "
        f"{report.population.events_without_source_observations}"
        + (
            " -- IDENTITY AUDIT INCOMPLETE, coverage gap exists"
            if report.population.identity_audit_incomplete
            else ""
        ),
        f"- Identity collisions found: {report.population.identity_collision_count}",
        f"- Episodes still open at --until cutoff (0 if --until unset): "
        f"{report.population.episodes_open_at_cutoff}",
        "",
        "## Identity collisions",
        "",
    ]
    if report.identity_collisions:
        lines.extend(
            markdown_table(
                ("Kind", "Bases", "Identity keys", "Exchanges"),
                [
                    (
                        collision.kind,
                        ", ".join(collision.bases),
                        ", ".join(collision.identity_keys),
                        ", ".join(collision.exchanges),
                    )
                    for collision in report.identity_collisions
                ],
            )
        )
    else:
        lines.append("_None found._")
    lines.extend(
        [
            "",
            "## Cross-venue unresolved pairs (case studies only)",
            "",
            "Absence from the identity collisions table above is NOT evidence these",
            "pairs are different instruments -- these pairs share no common exchange,",
            "so neither collision check could ever compare them.",
            "",
        ]
    )
    if report.cross_venue_unresolved:
        lines.extend(
            markdown_table(
                ("Bases", "First base exchanges", "Second base exchanges"),
                [
                    (
                        " / ".join(pair.bases),
                        ", ".join(pair.first_exchanges),
                        ", ".join(pair.second_exchanges),
                    )
                    for pair in report.cross_venue_unresolved
                ],
            )
        )
    else:
        lines.append(
            "_None -- every case-study base pair shares at least one comparable exchange._"
        )
    lines.extend(
        [
            "",
            "## Case studies (informational only -- not a source of population statistics)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Base",
                "Raw episodes",
                "Regimes (24h)",
                "Ratio (24h)",
                "Regimes (48h)",
                "Ratio (48h)",
                "Max peak_pct",
            ),
            _fragmentation_rows(report.case_studies),
        )
    )
    lines.extend(["", "## Full population (top 30 by 24h fragmentation ratio)", ""])
    lines.extend(
        markdown_table(
            (
                "Base",
                "Raw episodes",
                "Regimes (24h)",
                "Ratio (24h)",
                "Regimes (48h)",
                "Ratio (48h)",
                "Max peak_pct",
            ),
            _fragmentation_rows(report.fragmentation_by_base[:30]),
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discovery-only pump_events recurrence/identity integrity audit"
    )
    parser.add_argument("--since", type=parse_utc_datetime, help="inclusive UTC ISO-8601 cutoff")
    parser.add_argument("--until", type=parse_utc_datetime, help="exclusive UTC ISO-8601 cutoff")
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA", ""))
    parser.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .pump_recurrence_integrity_repository import PumpRecurrenceIntegrityRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for pump-recurrence-integrity-report")
    code_revision = normalize_code_revision(args.code_revision) if args.code_revision else "unknown"
    filters = PumpRecurrenceIntegrityFilters(since=args.since, until=args.until)
    repository = PumpRecurrenceIntegrityRepository.from_url(db_url)
    try:
        episodes, identity_observations = await repository.load(filters)
    finally:
        await repository.close()
    report = build_report(
        filters,
        episodes,
        identity_observations,
        code_revision=code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    import asyncio

    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
