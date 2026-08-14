"""Deterministic matched-control candidate selector for the momentum-flow
episode study.

Implements the matched-control rule frozen in `momentum_flow_protocol.py`'s
"Matched controls" section: same EXACT instrument (not just base ticker), a
UTC time-of-day within `MATCHED_CONTROL_TIME_OF_DAY_TOLERANCE_HOURS` of the
event's own trigger time, and no pump event of that same instrument within
`MATCHED_CONTROL_EXCLUSION_WINDOW_HOURS` of the control point. Amended per
colleague review before any real run: search both calendar directions from
the trigger and take the nearest available candidate (not a fixed backward
offset sequence), so an early-epoch event is not forced into a poorly
comparable multi-week-old regime just because "backward" was picked in
advance. Liquidity/turnover/volatility balance is a reported diagnostic, not
a ranking input, for v1 -- see `ControlBalance`.

Pure and DB-free, same "pure selection, I/O supplies data" split already
used by `momentum_flow_event_repository._select_events`: this module only
produces an ORDERED sequence of candidate instants and a balance verdict
from timeline points the caller already built. It never fetches bars and
never decides whether a candidate's own flow window is actually usable --
the caller tries candidates in the returned order and keeps the first one
whose timeline resolves (see `momentum_flow_episode_study_report.py`).

Shifting by a whole number of calendar days keeps the UTC time-of-day exact
(zero drift), which trivially satisfies the +-2h tolerance rather than
searching inside the tolerance band for its own sake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Imported, not restated: an independently frozen second copy of the
# exclusion window would be able to silently drift from momentum_flow_
# protocol.py's own frozen value (colleague review, before any real run).
# protocol.py is a leaf module with no dependency on this one, so this
# import direction is safe. MATCHED_CONTROL_TIME_OF_DAY_TOLERANCE_HOURS is
# not imported as a runtime value: shifting by a whole number of calendar
# days satisfies it by construction (zero drift), not via a numeric check
# against the constant -- see the module docstring.
from .momentum_flow_protocol import MATCHED_CONTROL_EXCLUSION_WINDOW_HOURS

MATCHED_CONTROL_MAX_SEARCH_DAYS = 28
CONTROL_SELECTOR_VERSION = "momentum_flow_matched_control_v1"

# Diagnostic-only balance thresholds for v1 (not a ranking formula -- see
# module docstring). A control whose total pre-window flow notional differs
# from the event's by more than this multiple in either direction is too
# different a liquidity regime to call "matched" even though it cleared the
# timing/exclusion gates; it is marked unresolved rather than used silently.
CONTROL_FLOW_IMBALANCE_RATIO = 5.0

_DAY = timedelta(days=1)
_EXCLUSION_WINDOW = timedelta(hours=MATCHED_CONTROL_EXCLUSION_WINDOW_HOURS)


@dataclass(frozen=True)
class ControlCandidate:
    rank: int
    candidate_at: datetime
    offset_days: int


@dataclass(frozen=True)
class ControlBalance:
    event_flow_notional_usd: float | None
    control_flow_notional_usd: float | None
    flow_ratio: float | None
    balanced: bool
    reason: str | None


def candidate_control_instants(
    *,
    trigger_at: datetime,
    other_trigger_instants_same_instrument: tuple[datetime, ...],
    capture_epoch_started_at: datetime,
    until: datetime,
    max_search_days: int = MATCHED_CONTROL_MAX_SEARCH_DAYS,
) -> tuple[ControlCandidate, ...]:
    """Ordered, already-filtered candidate control instants for one event.

    `other_trigger_instants_same_instrument` must already be scoped by the
    caller to the SAME exact instrument as `trigger_at` -- this function
    does not know about instrument identity, only timestamps. A candidate
    within `MATCHED_CONTROL_EXCLUSION_WINDOW_HOURS` of trigger_at itself or
    of any other trigger for this instrument is removed from the sequence
    entirely, never merely deprioritized: it must never appear even as a
    fallback of last resort. A candidate whose own required window would
    reach outside `[capture_epoch_started_at, until]` is removed the same
    way -- a control must live inside the same corrected capture epoch the
    event does, and must itself be mature relative to `until`. Separately
    (amended after second colleague review, before any real run), a
    candidate whose own following `MATCHED_CONTROL_EXCLUSION_WINDOW_HOURS`
    quiet period would still reach past `until` is removed too: the
    +4h feature-window maturity check alone proves the candidate's own
    timeline is observable, but the exclusion rule itself is a claim that
    no pump occurred for this instrument in the following 24h, which
    cannot be verified for a period not yet covered by this report's own
    dataset.
    """
    if trigger_at.utcoffset() is None or until.utcoffset() is None:
        raise ValueError("trigger_at and until must be timezone-aware")
    if capture_epoch_started_at.utcoffset() is None:
        raise ValueError("capture_epoch_started_at must be timezone-aware")
    if max_search_days <= 0:
        raise ValueError("max_search_days must be positive")

    exclusion_instants = (trigger_at, *other_trigger_instants_same_instrument)
    candidates: list[ControlCandidate] = []
    for offset_days in range(1, max_search_days + 1):
        for direction in (-1, 1):  # earlier first: stable tie-break at equal distance
            candidate_at = trigger_at + direction * offset_days * _DAY
            # Window bounds match momentum_flow_protocol.py's LOOKBACK_OFFSETS_MINUTES
            # span (-24h accumulation start, +4h furthest post-trigger point).
            window_start = candidate_at - timedelta(hours=24)
            window_end = candidate_at + timedelta(hours=4)
            if window_start < capture_epoch_started_at or window_end > until:
                continue
            # Amended after second colleague review, before any real run:
            # the +4h feature-window check above only proves this
            # candidate's own TIMELINE is observable -- it says nothing
            # about whether a pump has since happened for this instrument.
            # The matched-control rule itself requires PROVING no pump
            # occurred within +-24h of the control point (see the exclusion
            # check below), which is a claim about the following 24h, not
            # just the next 4h. A candidate whose own +24h quiet period
            # would still reach past `until` cannot have that absence
            # verified yet -- a pump after `until` simply is not in this
            # report's dataset -- so it is not mature enough to serve as a
            # control regardless of whether its own feature window already
            # resolved. Strict `>=`, not `>` (amended after third colleague
            # review, before any real run): the contamination data this
            # check actually relies on (momentum_flow_event_repository.
            # bybit_source_instants_statement) loads pump sources with
            # `first_seen_at < until`, EXCLUSIVE -- a pump landing exactly
            # AT `until` would never appear in that loaded set at all, so
            # a candidate whose quiet period ends exactly AT `until` cannot
            # actually have its own exclusion checked against that instant.
            # `until` is an exclusive cutoff everywhere else in this report
            # (the event cohort query itself uses the same `< until`); this
            # check must match that convention, not silently allow the one
            # instant the rest of the pipeline treats as out of scope.
            if candidate_at + timedelta(hours=24) >= until:
                continue
            if any(
                abs((candidate_at - other).total_seconds()) <= _EXCLUSION_WINDOW.total_seconds()
                for other in exclusion_instants
            ):
                continue
            candidates.append(
                ControlCandidate(
                    rank=len(candidates),
                    candidate_at=candidate_at,
                    offset_days=direction * offset_days,
                )
            )
    return tuple(candidates)


def evaluate_control_balance(
    *,
    event_flow_notional_usd: float | None,
    control_flow_notional_usd: float | None,
) -> ControlBalance:
    """Diagnostic-only balance check (v1: reported, not used to rank
    candidates -- see module docstring). Either side missing its own flow
    reading is reported as unresolved rather than silently treated as an
    even match."""
    if event_flow_notional_usd is None or control_flow_notional_usd is None:
        return ControlBalance(
            event_flow_notional_usd=event_flow_notional_usd,
            control_flow_notional_usd=control_flow_notional_usd,
            flow_ratio=None,
            balanced=False,
            reason="missing_flow_reading",
        )
    if event_flow_notional_usd <= 0 or control_flow_notional_usd <= 0:
        return ControlBalance(
            event_flow_notional_usd=event_flow_notional_usd,
            control_flow_notional_usd=control_flow_notional_usd,
            flow_ratio=None,
            balanced=False,
            reason="non_positive_flow_notional",
        )
    ratio = control_flow_notional_usd / event_flow_notional_usd
    balanced = (1 / CONTROL_FLOW_IMBALANCE_RATIO) <= ratio <= CONTROL_FLOW_IMBALANCE_RATIO
    return ControlBalance(
        event_flow_notional_usd=event_flow_notional_usd,
        control_flow_notional_usd=control_flow_notional_usd,
        flow_ratio=ratio,
        balanced=balanced,
        reason=None if balanced else "flow_notional_imbalance",
    )
