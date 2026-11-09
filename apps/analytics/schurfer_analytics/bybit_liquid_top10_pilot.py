"""Liquid top-10 flow pilot: the scope of two new calendar blocks (pre-protocol).

docs/research/bybit-liquid-top10-pilot-v1.md fixes the blocks, the pool, the matching
rule and the gate before any fetch. This module writes the tape scope of the two blocks
and its size from HYP-032's archive-size record (no request). Counts follow in their own
step after the owner's go for the fetch.

The scope holds, for each instrument in the monthly top 10 on any decision day of a
block, every day from 22 days before the block (15 days of rank and sigma history for
the earliest control, which can be 7 days before the block) to 7 days after it (the
latest control), so an event at a block's edge has the same chance of a control.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import bybit_absorbed_buying as v1
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import DAY_MS, estimate, load_features
from .bybit_compressed_spring import VERSION as TAPE_VERSION
from .bybit_post_funding_screen import verified
from .bybit_squeeze_continuation import _day, _iso
from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .bybit_compressed_spring import Features

VERSION: Final = "bybit_liquid_top10_pilot_v1"
TOP: Final = 10
# Decision days, inclusive (UTC), fixed in the note before any fetch.
BLOCKS: Final = (
    (v1._ms(2024, 11, 18), v1._ms(2024, 12, 1)),
    (v1._ms(2025, 4, 14), v1._ms(2025, 4, 27)),
)
CONTROL_WINDOW_D: Final = 7
LEAD_D: Final = CONTROL_WINDOW_D + v1.SCOPE_LEAD_DAYS


def top_pools(features: Features) -> dict[int, list[str]]:
    return {m: v1.pool(features, m)[:TOP] for m in v1.MONTHS}


def scope_days(features: Features) -> dict[str, list[str]]:
    """Each top-10 member's days from 22 days before its block to 7 days after it."""
    pools = top_pools(features)
    days: dict[str, set[str]] = defaultdict(set)
    for first, last in BLOCKS:
        members = {s for d in range(first, last + DAY_MS, DAY_MS) for s in pools[v1.month_of(d)]}
        for symbol in members:
            series = features.instruments[symbol]
            d = first - LEAD_D * DAY_MS
            while d <= last + CONTROL_WINDOW_D * DAY_MS:
                if series.launch_ms <= d + DAY_MS and d <= series.end_ms:
                    days[symbol].add(_day(d))
                d += DAY_MS
    return {s: sorted(v) for s, v in sorted(days.items())}


def seen_overlap(days: dict[str, list[str]]) -> int:
    """Symbol-days shared with HYP-036's pilot scope (blocks' days with their lead)."""
    seen = set()
    for first, end in v1.BLOCKS:
        d = first - v1.SCOPE_LEAD_DAYS * DAY_MS
        while d < end:
            seen.add(_day(d))
            d += DAY_MS
    return sum(1 for v in days.values() for d in v if d in seen)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope", help="the two blocks' tape scope and its size")
    for name in ("features", "archive-sizes"):
        scope.add_argument(f"--{name}", type=Path, required=True)
        scope.add_argument(f"--{name}-sha256", required=True)
    scope.add_argument("--output-dir", type=Path, required=True)
    scope.add_argument("--code-revision", required=True)
    dirty = scope.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    args = parser.parse_args(argv)
    if args.features_sha256 != v1.FEATURES_SHA256:
        raise SystemExit(f"the note pins the features snapshot {v1.FEATURES_SHA256}")
    if args.archive_sizes_sha256 != v1.ARCHIVE_SIZES_SHA256:
        raise SystemExit(f"the size estimate pins HYP-032's record {v1.ARCHIVE_SIZES_SHA256}")
    features = load_features(verified(args.features, args.features_sha256))
    days = scope_days(features)
    listed, sizes = v1.load_archive_sizes(verified(args.archive_sizes, args.archive_sizes_sha256))
    # HYP-032's fetcher reads the version and the "primary" days of a scope.
    body = {"version": TAPE_VERSION, "study": VERSION, "mode": "blocks", "primary": days}
    digest = publish_once(args.output_dir / "scope.json", body)
    size: dict[str, Any] = estimate(days, listed, sizes) | {
        "scope_sha256": digest,
        "blocks": [[_iso(a), _iso(b)] for a, b in BLOCKS],
        "symbols": len(days),
        "symbol_days_shared_with_hyp036_pilot": seen_overlap(days),
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    publish_once(args.output_dir / "scope-size.json", size)
    sys.stdout.write(json.dumps(size, indent=2) + "\n")


if __name__ == "__main__":
    main()
