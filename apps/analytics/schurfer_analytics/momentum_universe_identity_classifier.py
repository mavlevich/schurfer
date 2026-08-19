"""Pure cross-venue instrument identity matching (ROADMAP item 8's own
resolution half, following feat/momentum-universe-identity-foundation-v1).

Deliberately venue-count-agnostic: classify() takes a dict keyed by
exchange name, not two positional Bybit/Binance arguments, so a third
(fourth, ... twentieth) venue is a new dict entry, not a rewrite. Grouping
is N-way (by base + canonical_market_type across every exchange present),
not a pairwise Bybit-vs-Binance comparison.

No DB, no async, no I/O -- everything here is deterministic given its
inputs, which is what makes MATCH_RULESET_VERSION meaningful: bump it
whenever the classification rules below change, so a persisted
match_status stays interpretable against the ruleset that actually
produced it (same versioning discipline as momentumcapture.CaptureVersion
and every *_contract.py module's own frozen version string in this
codebase). Named match_ruleset_version (not resolver_version) specifically
to avoid colliding with outcomes.RESOLVER_VERSION, which is a completely
different concept (trade outcome resolution, not identity matching) that
happens to live in the same package.

match_status vocabulary (see docs/research/momentum-universe-identity-
foundation-v1.md's own "What a future resolution PR inherits" section,
which specified this vocabulary -- including its own worked example --
before this module existed): candidate, confirmed, conflict,
insufficient_evidence, manual_review_required, not_same_asset.

  confirmed
      Every member of the cross-venue group is an established instrument
      (onboarded at least RECENT_LISTING_WINDOW before the match run --
      ticker-squatting an asset that has already traded under that ticker
      on multiple venues for months is not a realistic risk). This is the
      ONLY path to confirmed a bare base + canonical_market_type match
      can take in v1.

      Known, deliberately accepted simplification (colleague-reviewed
      choice, not an oversight): this still promotes straight from a bare
      ticker match, with no second corroborating evidence source, which
      docs/research/momentum-universe-identity-foundation-v1.md explicitly
      warns against ("never an automatic confirmed from a bare ticker
      match alone"). It is done anyway for the established-only case, on
      the reasoning above, with the explicit intent to tighten it later
      (e.g. requiring a price-correlation check once one exists) once real
      evidence justifies it -- see ROADMAP.md's own item 8 entry for the
      tracking note.

  candidate
      A recently-listed member's onboarded_at is within
      CLOSE_ONBOARD_DELTA of another member's -- matches the foundation
      doc's own worked example verbatim ("base=BTR on two venues with
      close onboarding dates is a candidate, not proof of the same
      asset"): a near-simultaneous cross-listing is suggestive, but a bare
      ticker match is still not enough alone to call it confirmed.

  insufficient_evidence
      A recently-listed member's onboarded_at is within
      AMBIGUOUS_ONBOARD_DELTA of another member's but beyond
      CLOSE_ONBOARD_DELTA -- genuinely ambiguous: could be the same asset
      listed at a normal cross-venue lag, could be an unrelated instrument
      that happens to share a ticker. Neither confirming nor conflicting
      evidence dominates.

  conflict
      A recently-listed member's onboarded_at is not within
      AMBIGUOUS_ONBOARD_DELTA of any other member in its own group -- the
      shared ticker has no supporting onboarding-time evidence, which is
      the ticker-squatting-shaped case this whole step exists to catch.

  manual_review_required
      One exchange contributed more than one ready instrument under the
      same (base, canonical_market_type) in this run -- an upstream data
      anomaly the current live universe has never actually shown (516
      Bybit / 525 Binance ready instruments, zero duplicate bases within
      either exchange, verified against prod on 2026-08-17), but not one
      this module trusts blindly. When it happens, EVERY member of that
      base's group is marked manual_review_required, including
      instruments from otherwise-unambiguous exchanges: with one side's
      own identity unclear, there is no reliable "other" to compare
      onboarding time against for anyone else in the group either.

  not_same_asset
      Declared in the schema's own CHECK constraint (see migration 0029)
      for forward compatibility, but never produced by this module. There
      is currently no evidence source strong enough to positively assert
      two same-ticker instruments are DIFFERENT assets (that would need
      something like a price-series divergence check, which does not
      exist yet) -- inventing a heuristic for it here would be exactly
      the kind of guessed classification this whole step is designed to
      avoid producing.

A base+canonical_market_type group with instruments from only ONE exchange
is not a cross-venue match at all and produces no cluster -- most of the
live universe (53 of 516 Bybit bases, 62 of 525 Binance bases, as of the
same 2026-08-17 prod check) is exactly this: an asset only one of the two
captured venues currently lists.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

MatchStatus = Literal[
    "candidate",
    "confirmed",
    "conflict",
    "insufficient_evidence",
    "manual_review_required",
    "not_same_asset",
]

MATCH_RULESET_VERSION = "v1"

# An instrument onboarded at least this long before the match run is
# "established" -- old enough that a same-ticker collision landing on it
# now would itself be a newsworthy, investigable event, not something this
# heuristic needs to catch; see the module doc comment's own confirmed
# rule.
RECENT_LISTING_WINDOW = timedelta(days=90)

# A recently-listed member within this of another member's onboarded_at is
# treated as a near-simultaneous cross-listing (candidate -- see the
# module doc comment's own worked example from the foundation doc). Chosen
# against real prod data (2026-08-17): 258 of 463 base-matched Bybit/
# Binance pairs land within 10 days of each other; this is comfortably
# inside that cluster.
CLOSE_ONBOARD_DELTA = timedelta(days=7)

# A recently-listed member within this (but beyond CLOSE_ONBOARD_DELTA) of
# another member's onboarded_at is insufficient_evidence; beyond this,
# conflict. Chosen well below the smallest delta observed for a genuinely
# established pair in the same prod check (188 days, BTC) so this band can
# never accidentally swallow an old-asset case -- those are excluded from
# this branch entirely by the is_established check before
# AMBIGUOUS_ONBOARD_DELTA is ever consulted.
AMBIGUOUS_ONBOARD_DELTA = timedelta(days=30)


@dataclass(frozen=True)
class CandidateInstrument:
    """One venue's own ready instrument, as read from the latest
    momentum_universe_instruments snapshot for that exchange. Deliberately
    not momentumsource.Instrument itself (a Go type) or any repository row
    shape -- the minimal fields this module's own classification actually
    uses, so this module has zero dependency on how a caller fetches them.
    """

    exchange: str
    native_market_id: str
    base: str
    canonical_market_type: str
    identity_key: str
    onboarded_at: datetime


@dataclass(frozen=True)
class ClusterMember:
    exchange: str
    native_market_id: str
    identity_key: str
    onboarded_at: datetime
    match_status: MatchStatus
    match_reason: str


@dataclass(frozen=True)
class AssetCluster:
    cluster_key: str
    base: str
    canonical_market_type: str
    members: tuple[ClusterMember, ...]


def _established(instrument: CandidateInstrument, resolved_at: datetime) -> bool:
    return (resolved_at - instrument.onboarded_at) >= RECENT_LISTING_WINDOW


def _member(instrument: CandidateInstrument, *, status: MatchStatus, reason: str) -> ClusterMember:
    return ClusterMember(
        exchange=instrument.exchange,
        native_market_id=instrument.native_market_id,
        identity_key=instrument.identity_key,
        onboarded_at=instrument.onboarded_at,
        match_status=status,
        match_reason=reason,
    )


def _cluster(base: str, canonical_market_type: str, members: list[ClusterMember]) -> AssetCluster:
    return AssetCluster(
        cluster_key=f"{base}:{canonical_market_type}",
        base=base,
        canonical_market_type=canonical_market_type,
        members=tuple(members),
    )


def _classify_group(
    base: str,
    canonical_market_type: str,
    group: tuple[CandidateInstrument, ...],
    *,
    resolved_at: datetime,
) -> AssetCluster | None:
    distinct_exchanges = {instrument.exchange for instrument in group}
    if len(distinct_exchanges) < 2:
        return None

    exchange_counts: dict[str, int] = defaultdict(int)
    for instrument in group:
        exchange_counts[instrument.exchange] += 1
    duplicated_exchanges = sorted(
        exchange for exchange, count in exchange_counts.items() if count > 1
    )

    if duplicated_exchanges:
        reason = (
            "manual review: "
            f"{', '.join(duplicated_exchanges)} each contributed more than one "
            f"ready instrument under base={base!r} "
            f"canonical_market_type={canonical_market_type!r} in this run"
        )
        members = [
            _member(instrument, status="manual_review_required", reason=reason)
            for instrument in group
        ]
        return _cluster(base, canonical_market_type, members)

    if all(_established(instrument, resolved_at) for instrument in group):
        reason = (
            f"all {len(group)} members established "
            f"(onboarded >= {RECENT_LISTING_WINDOW.days}d before match run)"
        )
        members = [_member(instrument, status="confirmed", reason=reason) for instrument in group]
        return _cluster(base, canonical_market_type, members)

    members = []
    for instrument in group:
        if _established(instrument, resolved_at):
            members.append(
                _member(
                    instrument,
                    status="confirmed",
                    reason=(
                        f"established member (onboarded >= {RECENT_LISTING_WINDOW.days}d "
                        "before match run)"
                    ),
                )
            )
            continue
        others = [other for other in group if other is not instrument]
        nearest = min(others, key=lambda other: abs(instrument.onboarded_at - other.onboarded_at))
        delta = abs(instrument.onboarded_at - nearest.onboarded_at)
        delta_days = delta.total_seconds() / 86400.0
        if delta <= CLOSE_ONBOARD_DELTA:
            status: MatchStatus = "candidate"
            reason = (
                f"recent listing, onboarded {delta_days:.1f}d from nearest member "
                f"({nearest.exchange}) -- within close cross-listing window "
                f"({CLOSE_ONBOARD_DELTA.days}d), but a bare ticker match is still "
                "not confirmed alone"
            )
        elif delta <= AMBIGUOUS_ONBOARD_DELTA:
            status = "insufficient_evidence"
            reason = (
                f"recent listing, onboarded {delta_days:.1f}d from nearest member "
                f"({nearest.exchange}) -- neither close enough to support nor far "
                "enough to contradict a match"
            )
        else:
            status = "conflict"
            reason = (
                f"recent listing, onboarded {delta_days:.1f}d from nearest member "
                f"({nearest.exchange}) -- no corroborating onboarding-time evidence"
            )
        members.append(_member(instrument, status=status, reason=reason))

    return _cluster(base, canonical_market_type, members)


def classify(
    instruments_by_exchange: dict[str, tuple[CandidateInstrument, ...]],
    *,
    resolved_at: datetime,
) -> tuple[AssetCluster, ...]:
    """Groups every ready instrument across every exchange present by
    (base, canonical_market_type) and classifies each one's own cross-
    venue match_status. Only groups with instruments from at least two
    distinct exchanges produce a cluster; a base only one exchange lists
    is not a cross-venue match and is silently omitted (not an error --
    the large majority of the live universe is single-venue-only).

    Raises ValueError if any instrument's own exchange field disagrees
    with the dict key it was passed under -- a caller bug (mis-keyed
    dict), not data this function silently trusts either side of.
    """
    groups: dict[tuple[str, str], list[CandidateInstrument]] = defaultdict(list)
    for exchange, instruments in instruments_by_exchange.items():
        for instrument in instruments:
            if instrument.exchange != exchange:
                raise ValueError(
                    f"instrument.exchange={instrument.exchange!r} does not match "
                    f"its own dict key {exchange!r}"
                )
            groups[(instrument.base, instrument.canonical_market_type)].append(instrument)

    clusters: list[AssetCluster] = []
    for (base, canonical_market_type), group in groups.items():
        cluster = _classify_group(
            base, canonical_market_type, tuple(group), resolved_at=resolved_at
        )
        if cluster is not None:
            clusters.append(cluster)
    return tuple(sorted(clusters, key=lambda cluster: cluster.cluster_key))
