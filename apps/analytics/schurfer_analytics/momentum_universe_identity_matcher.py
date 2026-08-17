"""CLI entrypoint for cross-venue instrument identity matching.

Ad-hoc report-style invocation (like oi-growth-filter-report,
momentum-flow-episode-study-report, gate-identity-candidate-tooling --
see the Makefile's own prod-*-report targets), not a persistent worker:
this job reads the LATEST already-persisted momentum_universe_instruments
snapshot per exchange (see momentum_universe_identity_repository's own
doc comment) and those snapshots currently only get written once, at
capture-process startup -- there is no periodic re-snapshot mechanism yet
for a scheduled timer to usefully re-trigger against. Re-run by hand (or
via a future systemd timer, once snapshot refresh itself becomes
periodic) whenever a capture process has restarted on either venue since
the last run.

Wires momentum_universe_identity_classifier (pure) to
momentum_universe_identity_repository (I/O): reads every exchange passed
via --exchange (repeatable; default both currently-captured venues),
classifies, and replaces the entire cluster/member table content with the
result (see the repository's own full-resync contract).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime

from .momentum_universe_identity_classifier import MATCH_RULESET_VERSION, classify
from .momentum_universe_identity_repository import MomentumUniverseIdentityRepository


async def run(
    *,
    database_url: str,
    exchanges: tuple[str, ...],
    resolved_at: datetime,
) -> dict[str, object]:
    repository = MomentumUniverseIdentityRepository.from_url(database_url)
    try:
        # Concurrent, not sequential: each exchange's own read is
        # independent (its own snapshot lookup, its own instrument rows),
        # so there is no reason to pay N round-trips serially as more
        # venues are onboarded -- exactly the growth this classifier is
        # designed for (see its own venue-count-agnostic doc comment).
        instruments_by_venue = await asyncio.gather(
            *(repository.latest_ready_instruments(exchange) for exchange in exchanges)
        )
        instruments_by_exchange = dict(zip(exchanges, instruments_by_venue, strict=True))
        clusters = classify(instruments_by_exchange, resolved_at=resolved_at)
        members_written = await repository.persist_clusters(
            clusters,
            match_ruleset_version=MATCH_RULESET_VERSION,
            resolved_at=resolved_at,
        )
        status_counts: dict[str, int] = {}
        for cluster in clusters:
            for member in cluster.members:
                status_counts[member.match_status] = status_counts.get(member.match_status, 0) + 1
        return {
            "match_ruleset_version": MATCH_RULESET_VERSION,
            "resolved_at": resolved_at.isoformat(),
            "exchanges": {
                exchange: len(instruments)
                for exchange, instruments in instruments_by_exchange.items()
            },
            "clusters_written": len(clusters),
            "members_written": members_written,
            "match_status_counts": status_counts,
        }
    finally:
        await repository.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exchange",
        action="append",
        help="exchange to include in this matching run; repeat for multiple "
        "(default: bybit, binance -- the two currently-captured venues)",
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
        run(database_url=database_url, exchanges=exchanges, resolved_at=datetime.now(UTC))
    )
    summary["code_revision"] = args.code_revision
    summary["working_tree_dirty"] = args.working_tree_dirty
    json.dump(summary, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
