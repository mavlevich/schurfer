"""CLI entrypoint for the token-universe control-group/absent-from-latest-
snapshot report (research/token-universe-coverage-v1).

Ad-hoc report-style invocation, matching momentum-universe-identity-
matcher's own convention (see its doc comment) -- not a persistent worker,
re-run by hand against a chosen window. Wires token_universe_coverage
(pure) to MomentumUniverseIdentityRepository's own instruments_as_of/
universe_seen_in_window (I/O): for each --exchange, reads every instrument
LIFE that was identity_status='ready' at some point covering
[--window-start, --window-end) -- carrying forward the nearest snapshot
before window_start, not only snapshots captured inside it, see
universe_seen_in_window's own doc comment -- the control-group universe
(every asset that COULD have been a candidate during the window, not only
ones app.pump_events happened to record), cross-references the exchange's
own current ready set (as of report run time) to classify which of those
are still ready, and reports the rest.

Deliberately reports absence from the current snapshot, never claims
"delisted" as a proven fact in this report's own output: a rename under a
new identity_key or a stale/incomplete current snapshot look identical
from a bare set-difference (see token_universe_coverage.delisted's own doc
comment). current_snapshot_within_tolerance and window's own
carry_in_within_tolerance/has_reliable_coverage are reported explicitly so
a caller can tell a trustworthy read apart from one where either boundary
lacked close-enough snapshot evidence.

This is read-only: it does not write to momentum_universe_asset_clusters/
_cluster_members (that stays momentum_universe_identity_matcher's own job)
and adds no new capture, matching the reasoning in ROADMAP.md's item 7
entry for why this was scoped as a read against already-persisted snapshot
history rather than new infrastructure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta

from .momentum_universe_identity_repository import MomentumUniverseIdentityRepository
from .token_universe_coverage import COVERAGE_VERSION, delisted, mark_currently_ready


async def run(
    *,
    database_url: str,
    exchanges: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    max_staleness: timedelta,
) -> dict[str, object]:
    repository = MomentumUniverseIdentityRepository.from_url(database_url)
    try:
        now = datetime.now(UTC)
        by_exchange: dict[str, object] = {}
        for exchange in exchanges:
            window = await repository.universe_seen_in_window(
                exchange, window_start, window_end, max_carry_in_staleness=max_staleness
            )
            current = await repository.instruments_as_of(exchange, now)
            current_is_usable = current.is_usable(max_staleness=max_staleness)

            # Colleague review, round 3: classifying against an unusable
            # current snapshot (none exists, or the nearest one is stale
            # beyond max_staleness) is worse than reporting nothing -- an
            # empty/stale current.identity_keys would mark EVERY historical
            # entry "absent", fabricating a full-universe delisting that
            # current_snapshot_within_tolerance=False alone does not
            # prevent a careless reader from trusting (the counts/list would
            # already be sitting right next to it in the same JSON object).
            # Skip classification entirely instead; report null fields with
            # an explicit classification_status a caller cannot miss.
            if current_is_usable:
                marked = mark_currently_ready(window.seen, current.identity_keys)
                absent = delisted(marked)
                currently_ready_count: int | None = len(marked) - len(absent)
                absent_count: int | None = len(absent)
                absent_list: list[dict[str, str]] | None = [
                    {
                        "identity_key": entry.identity_key,
                        "native_market_id": entry.native_market_id,
                        "base": entry.base,
                        "canonical_market_type": entry.canonical_market_type,
                        "first_seen_ready_at": entry.first_seen_ready_at.isoformat(),
                        "last_seen_ready_at": entry.last_seen_ready_at.isoformat(),
                    }
                    for entry in absent
                ]
                classification_status = "ok"
            else:
                currently_ready_count = None
                absent_count = None
                absent_list = None
                classification_status = "insufficient_data_no_usable_current_snapshot"

            by_exchange[exchange] = {
                "classification_status": classification_status,
                "control_group_size": len(window.seen),
                "currently_ready_count": currently_ready_count,
                "absent_from_latest_ready_snapshot_count": absent_count,
                "absent_from_latest_ready_snapshot": absent_list,
                "carry_in_snapshot_captured_at": (
                    window.carry_in_snapshot_captured_at.isoformat()
                    if window.carry_in_snapshot_captured_at is not None
                    else None
                ),
                "carry_in_within_tolerance": window.carry_in_within_tolerance,
                "current_snapshot_captured_at": (
                    current.snapshot_captured_at.isoformat()
                    if current.snapshot_captured_at is not None
                    else None
                ),
                "current_snapshot_within_tolerance": current_is_usable,
                # Both boundaries must have close-enough snapshot evidence for
                # control_group_size/absent_from_latest_ready_snapshot to be
                # trusted as complete -- not just the window's own start.
                "has_reliable_coverage": window.has_reliable_coverage and current_is_usable,
            }
        return {
            "coverage_version": COVERAGE_VERSION,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "max_staleness_days": max_staleness.days,
            "exchanges": by_exchange,
        }
    finally:
        await repository.close()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_nonnegative_days(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"--max-staleness-days must be >= 0, got {parsed}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exchange",
        action="append",
        help="exchange to include in this report; repeat for multiple "
        "(default: bybit, binance -- the two currently-captured venues)",
    )
    parser.add_argument(
        "--window-start", required=True, type=_parse_iso, help="ISO-8601 timestamp, inclusive"
    )
    parser.add_argument(
        "--window-end", required=True, type=_parse_iso, help="ISO-8601 timestamp, exclusive"
    )
    parser.add_argument(
        "--max-staleness-days",
        required=True,
        type=_parse_nonnegative_days,
        help="max days (>= 0) a carry-in or current snapshot may lag its own reference "
        "instant (window_start / report run time) and still count as reliable coverage "
        "-- no default, this is a research-contract decision for whoever runs the report",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    exchanges = tuple(args.exchange or ("bybit", "binance"))
    database_url = os.environ["DATABASE_URL"]
    summary = asyncio.run(
        run(
            database_url=database_url,
            exchanges=exchanges,
            window_start=args.window_start,
            window_end=args.window_end,
            max_staleness=timedelta(days=args.max_staleness_days),
        )
    )
    summary["code_revision"] = args.code_revision
    summary["working_tree_dirty"] = args.working_tree_dirty
    json.dump(summary, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
