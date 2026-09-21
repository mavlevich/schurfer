"""Read-only export of point-in-time PER-ROUTE identity for the abnormal-flow scan.

Uses ONLY historical ``app.momentum_universe_snapshots`` +
``app.momentum_universe_instruments``: each route's ``identity_key`` is point-in-time
via the snapshot it appeared in. It deliberately does NOT read
``momentum_universe_cluster_members`` -- that table holds only the CURRENT classifier
result (``persist_clusters`` deletes prior rows), so it cannot back a historical
cross-venue join. Cross-venue merging, if ever needed for a unique-asset count, must be
recomputed per historical boundary from the snapshots THEN available, with a pinned
classifier version, and ``candidate``/``conflict`` never merged; that is a deferred
follow-up. This v1 emits per-route identity only, so the scan reports honest per-venue
counts and never claims a cross-venue unique-asset total.

A snapshot is a full-universe replacement, so an instrument's identity is valid from its
snapshot's ``captured_at`` until the venue's NEXT snapshot (or open-ended). ``valid_to``
is an interval boundary, not proof the universe stayed complete across a long gap; the
scan records the snapshot age at each decision separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

IDENTITY_EXPORT_VERSION = "abnormal_flow_identity_export_v1"


@dataclass(frozen=True)
class SnapshotInstrumentRow:
    """One (snapshot, instrument) row: a route's identity as captured in one snapshot."""

    exchange: str
    market_type: str
    native_market_id: str
    identity_key: str
    identity_status: str
    captured_at: datetime


def build_identity_records(
    rows: list[SnapshotInstrumentRow], *, window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    """Turn snapshot rows into point-in-time per-route identity records overlapping
    ``[window_start, window_end)``. ``valid_to`` is the venue's next snapshot boundary
    (or null); ``snapshot_captured_at`` is kept so the scan can measure snapshot age. A
    snapshot replaces the whole universe, so a route absent from a later snapshot simply
    has no record covering that later interval (it is delisted at that boundary)."""
    # Per venue, the ordered set of snapshot boundaries.
    boundaries: dict[str, list[datetime]] = {}
    for row in rows:
        boundaries.setdefault(row.exchange, [])
    for exchange in boundaries:
        seen = sorted({r.captured_at for r in rows if r.exchange == exchange})
        boundaries[exchange] = seen

    def next_boundary(exchange: str, captured_at: datetime) -> datetime | None:
        after = [b for b in boundaries[exchange] if b > captured_at]
        return after[0] if after else None

    records: list[dict[str, Any]] = []
    for row in rows:
        valid_from = row.captured_at
        valid_to = next_boundary(row.exchange, row.captured_at)
        # Keep only intervals that overlap the requested window.
        if valid_from >= window_end:
            continue
        if valid_to is not None and valid_to <= window_start:
            continue
        records.append(
            {
                "exchange": row.exchange,
                "market_type": row.market_type,
                "native_market_id": row.native_market_id,
                # per-route identity_key (never a cross-venue cluster)
                "canonical_asset": row.identity_key,
                "identity_status": row.identity_status,
                "snapshot_captured_at": valid_from.isoformat(),
                "valid_from": valid_from.isoformat(),
                "valid_to": None if valid_to is None else valid_to.isoformat(),
            }
        )
    records.sort(key=lambda r: (r["exchange"], r["native_market_id"], r["valid_from"]))
    return records


_SNAPSHOT_SQL = """
SELECT s.exchange AS exchange,
       s.captured_at AS captured_at,
       i.native_market_id AS native_market_id,
       i.identity_key AS identity_key,
       i.identity_status AS identity_status,
       coalesce(i.canonical_market_type, 'linear') AS market_type
FROM app.momentum_universe_snapshots s
JOIN app.momentum_universe_instruments i
  ON i.exchange = s.exchange
 AND i.universe_version = s.universe_version
 AND i.catalog_version = s.catalog_version
WHERE i.identity_key IS NOT NULL
ORDER BY s.exchange, s.captured_at, i.native_market_id
"""


async def read_snapshot_rows(database_url: str) -> list[SnapshotInstrumentRow]:
    """Read every snapshot+instrument row (read-only). Snapshots are written rarely, so
    the whole history is small; the scan window is applied in ``build_identity_records``."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(database_url), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(_SNAPSHOT_SQL))
            out: list[SnapshotInstrumentRow] = []
            for row in result:
                captured = row.captured_at
                if captured.tzinfo is None:
                    captured = captured.replace(tzinfo=UTC)
                out.append(
                    SnapshotInstrumentRow(
                        exchange=str(row.exchange),
                        market_type=str(row.market_type),
                        native_market_id=str(row.native_market_id),
                        identity_key=str(row.identity_key),
                        identity_status=str(row.identity_status),
                        captured_at=captured,
                    )
                )
            return out
    finally:
        await engine.dispose()


def export_identity(
    database_url: str, *, window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    """Read snapshots and build the point-in-time identity artifact for the window."""
    rows = asyncio.run(read_snapshot_rows(database_url))
    records = build_identity_records(rows, window_start=window_start, window_end=window_end)
    venues = sorted({r["exchange"] for r in records})
    coverage = {
        exchange: {
            "records": sum(1 for r in records if r["exchange"] == exchange),
            "snapshots": sorted(
                {r["snapshot_captured_at"] for r in records if r["exchange"] == exchange}
            ),
        }
        for exchange in venues
    }
    return {
        "identity_export_version": IDENTITY_EXPORT_VERSION,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "coverage": coverage,
        "records": records,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="Prod DB URL (read-only use)")
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True, help="exclusive")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    window_start = datetime(
        args.start_day.year, args.start_day.month, args.start_day.day, tzinfo=UTC
    )
    window_end = datetime(args.end_day.year, args.end_day.month, args.end_day.day, tzinfo=UTC)
    artifact = export_identity(args.database_url, window_start=window_start, window_end=window_end)
    args.out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    total = len(artifact["records"])
    sys.stdout.write(f"exported {total} identity records to {args.out}\n")


if __name__ == "__main__":
    main()
