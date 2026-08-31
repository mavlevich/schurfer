"""Pure point-in-time listing-coverage classification (research/token-
universe-coverage-v1).

Colleague review, ROADMAP item 7 ("token universe identity and expansion"):
before writing any code here, momentum_universe_identity_foundation_v1
(2026-08-15) and momentum_universe_identity_resolution_v1 (2026-08-17) were
found to already implement the two pieces the item's own text described as
new work -- durable per-instrument identity metadata and cross-venue
confirmed/candidate/conflict/insufficient_evidence/manual_review_required
matching (momentum_universe_identity_classifier.classify()). Neither is
reimplemented here. The one real, verified gap: app.momentum_universe_
snapshots only gets a new row when a momentum-capture process restarts --
irregular, not a fixed cadence -- so "was base X actively listed on
exchange E at some past instant T" (needed to build a non-survivorship-
biased control group for research/serial-pump-regimes-v1, and to know
which bases have since been delisted) cannot be answered today. This
module adds that read, entirely from snapshot history that is already
durably persisted (no new capture, no schema change) -- see
momentum_universe_identity_repository.py for the I/O half.

Kept pure and separate from the repository on purpose, mirroring this
codebase's own momentum_universe_identity_classifier/_repository split:
this classification is a pure set comparison given already-fetched data,
easy to get subtly wrong, so it is isolated here and unit-tested with
synthetic inputs rather than only exercised end-to-end against a real
database.

Colleague review, round 2, closed two real defects in the first version
of this module:

- **Grouped by native_market_id instead of identity_key.** A market id
  that gets delisted and later relisted under the same ticker (migration
  0028's own docstring names this exact case) is a DIFFERENT instrument --
  a new onboarded_at, a new identity_key -- not a continuation of the old
  one. Grouping by native_market_id alone silently merged the two lives
  into one SeenInstrument, understating first_seen_ready_at's own meaning
  and, worse, could mark a genuinely-gone old listing "currently_ready"
  just because a new, unrelated listing reused its native_market_id.
  SeenInstrument now carries identity_key and mark_currently_ready
  compares by identity_key -- the exact key the rest of this codebase's
  identity system already uses to keep two lives of the same market id
  apart.
- **bool default made "not yet classified" indistinguishable from
  "confirmed absent."** delisted() before mark_currently_ready silently
  returned every entry (every currently_ready started False), directly
  contradicting this module's own docstring claim that this could not
  happen -- and the unit test written to prove it actually called
  mark_currently_ready first, so the claim was never really tested.
  currently_ready is now Optional[bool] (None = not yet classified);
  delisted() raises if it is ever asked to classify an unclassified
  entry, rather than silently guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime, timedelta

COVERAGE_VERSION = "token_universe_coverage_v2"


@dataclass(frozen=True)
class SeenInstrument:
    """One distinct instrument LIFE -- keyed by identity_key, not bare
    native_market_id, so a delisted-then-relisted market id under the same
    ticker is never conflated with its predecessor -- that was
    identity_status='ready' in at least one momentum_universe_instruments
    row whose owning snapshot's captured_at fell inside the query's own
    coverage set (see WindowCoverage). currently_ready is None until
    mark_currently_ready classifies it -- never guessed at construction
    time, and never silently treated as "confirmed absent" just because it
    has not been classified yet (see delisted's own fail-closed check)."""

    exchange: str
    identity_key: str
    native_market_id: str
    base: str
    canonical_market_type: str
    first_seen_ready_at: datetime
    last_seen_ready_at: datetime
    currently_ready: bool | None = None


def mark_currently_ready(
    seen: tuple[SeenInstrument, ...],
    currently_ready_identity_keys: frozenset[str],
) -> tuple[SeenInstrument, ...]:
    """Sets currently_ready on every entry to whether its own identity_key
    (not native_market_id -- see the module doc comment's own relisting
    case) is in currently_ready_identity_keys, which must come from a
    single, fully independent "what is ready right now" read (e.g.
    MomentumUniverseIdentityRepository.instruments_as_of's own
    identity_keys as of "now"), never from this same windowed query --
    otherwise a window ending well before "now" could have "now"'s own
    listing state leak into its answer. Returns a NEW tuple
    (dataclasses.replace); never mutates entries in seen, so a caller
    reusing the same seen tuple against two different currently-ready sets
    cannot have the first call's answer bleed into the second."""
    return tuple(
        replace(entry, currently_ready=entry.identity_key in currently_ready_identity_keys)
        for entry in seen
    )


def delisted(seen: tuple[SeenInstrument, ...]) -> tuple[SeenInstrument, ...]:
    """The subset of an already currently_ready-classified seen tuple (see
    mark_currently_ready) whose identity_key is absent from the exchange's
    latest ready snapshot -- i.e. was seen ready inside the queried
    coverage window but not currently. Fails closed if asked to classify
    anything still unclassified (currently_ready is None): raises
    ValueError rather than silently treating "not yet classified" as
    "confirmed absent" the way a bool default did in an earlier version of
    this module. This does NOT prove genuine delisting on its own -- a
    rename under a new identity_key, or a stale/incomplete latest
    snapshot, look identical from here; see AsOfCoverage.is_usable for the
    freshness check a caller must also apply before treating this as
    evidence, and prefer the report's own more honestly-named
    'absent_from_latest_ready_snapshot' framing over calling this
    'delisted' in anything user-facing."""
    unclassified = [entry for entry in seen if entry.currently_ready is None]
    if unclassified:
        raise ValueError(
            f"delisted() called with {len(unclassified)} unclassified SeenInstrument "
            "entries (currently_ready is None) -- call mark_currently_ready on the "
            "full tuple first; e.g. "
            f"{unclassified[0].exchange}:{unclassified[0].identity_key}"
        )
    return tuple(entry for entry in seen if entry.currently_ready is False)


@dataclass(frozen=True)
class AsOfCoverage:
    """The result of asking "what was ready on this exchange at instant
    as_of" against sparse, irregularly-captured snapshot history.
    snapshot_captured_at/staleness are None only when no snapshot exists at
    or before as_of at all (e.g. as_of predates this exchange's capture
    process ever starting) -- instruments is then always empty too, never a
    guess. A non-None staleness does not mean the answer is necessarily
    good; see is_usable."""

    exchange: str
    as_of: datetime
    snapshot_captured_at: datetime | None
    native_market_ids: frozenset[str]
    identity_keys: frozenset[str]

    @property
    def staleness(self) -> timedelta | None:
        if self.snapshot_captured_at is None:
            return None
        return self.as_of - self.snapshot_captured_at

    def is_usable(self, *, max_staleness: timedelta) -> bool:
        """False when no snapshot exists at or before as_of, or the nearest
        one is older than max_staleness -- callers must check this
        explicitly and exclude the episode from a formal read rather than
        silently trust a listing snapshot that predates the instant being
        asked about by more than this caller-chosen tolerance. There is no
        codebase-wide default tolerance: how stale is still "the same
        listing state" is a research-contract decision for whichever
        report calls this, not something this module should assume."""
        if self.snapshot_captured_at is None:
            return False
        return (self.as_of - self.snapshot_captured_at) <= max_staleness


@dataclass(frozen=True)
class WindowCoverage:
    """The result of asking "every instrument ready at some point during
    [window_start, window_end)" against sparse snapshot history -- the
    control-group universe research/serial-pump-regimes-v1 needs.

    Colleague review, round 2: filtering snapshots to captured_at INSIDE
    the window alone silently excludes the carry-in state -- an exchange's
    own last snapshot before window_start still describes what was ready
    for the whole window, right up to the next restart, which could land
    anywhere inside or even after the window. Without a carry-in, a window
    containing no capture-process restart at all returns an empty universe
    even though hundreds of instruments were genuinely eligible the entire
    time.

    The carry-in must itself be ADMISSIBLY FRESH to be trusted: only a
    snapshot within max_carry_in_staleness of window_start is actually
    used to populate `seen` -- a carry-in far outside that tolerance is
    reported via carry_in_snapshot_captured_at (for diagnosis) but
    excluded from `seen`, never silently mixed into the universe as if it
    were still representative. `seen` can legitimately be empty: when the
    only candidate carry-in is stale beyond tolerance (or none exists) and
    no snapshot was captured inside the window either, there is genuinely
    no evidence to report, not a construction guarantee otherwise. A
    caller MUST check has_reliable_coverage (equivalently,
    carry_in_within_tolerance) before treating `seen` as complete coverage
    of the window's own start -- see universe_seen_in_window's own
    max_carry_in_staleness parameter, which decides it."""

    exchange: str
    window_start: datetime
    window_end: datetime
    carry_in_snapshot_captured_at: datetime | None
    carry_in_within_tolerance: bool
    seen: tuple[SeenInstrument, ...]

    @property
    def has_reliable_coverage(self) -> bool:
        """False when there was no snapshot at all at or before
        window_start, or the nearest one was staler than the caller's own
        max_carry_in_staleness -- in either case that carry-in is excluded
        from `seen` (see the class doc comment), so the window's own start
        may be undercounted: a real listing active since before the window
        began, but with no admissibly fresh snapshot evidence to prove it.
        A caller building a formal denominator must check this and treat
        an unreliable window as insufficient_data, not silently trust a
        possibly-undercounted universe."""
        return self.carry_in_within_tolerance
