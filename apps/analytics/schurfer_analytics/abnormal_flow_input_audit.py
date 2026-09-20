"""Outcome-blind input audit for the proposed abnormal-flow economic screen.

Only cold-bar manifests and input columns are read. This module never selects
future prices, pump events, paper outcomes, or returns. It deliberately refuses
an incomplete or unverified day instead of turning a gap into a smaller sample.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .cold_bar_export import EXPORT_VERSION, SOURCE_TABLE, ExportManifest, verify_local

AUDIT_VERSION = "abnormal_flow_input_audit_v1"


@dataclass(frozen=True)
class DayInput:
    day: str
    file_name: str
    row_count: int
    sha256: str
    source_fingerprint: str


@dataclass(frozen=True)
class VenueDayCoverage:
    day: str
    file_name: str
    exchange: str
    market_type: str
    capture_version: str
    rows: int
    price_complete: int
    trades_complete: int
    open_interest_complete: int
    native_oi_present: int
    oi_event_at_present: int
    oi_observed_at_present: int
    oi_value_present: int
    bid_ask_valid: int
    positive_flow: int
    finalized_within_5m: int


@dataclass(frozen=True)
class InputAudit:
    audit_version: str
    window_start: str
    window_end: str
    days: tuple[DayInput, ...]
    coverage: tuple[VenueDayCoverage, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _days(start: date, end: date) -> tuple[date, ...]:
    if end <= start:
        raise ValueError("end day must be after start day")
    result: list[date] = []
    day = start
    while day < end:
        result.append(day)
        day += timedelta(days=1)
    return tuple(result)


def _verified_input(out_dir: Path, day: date) -> tuple[Path, ExportManifest]:
    manifest = verify_local(out_dir, day)
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if (
        manifest.day != day.isoformat()
        or manifest.file_name != f"bars-{day.isoformat()}.parquet"
        or manifest.bucket_start_from != start.isoformat()
        or manifest.bucket_start_until != (start + timedelta(days=1)).isoformat()
        or manifest.source_table != SOURCE_TABLE
        or manifest.export_version != EXPORT_VERSION
    ):
        raise ValueError(f"{day}: cold-bar manifest identity or bounds mismatch")
    if (
        manifest.fidelity_verified is not True
        or not manifest.source_fingerprint
        or manifest.source_fingerprint != manifest.file_fingerprint
    ):
        raise ValueError(f"{day}: cold-bar source fidelity is not proven")
    return out_dir / manifest.file_name, manifest


_COVERAGE_SQL = """
SELECT
    filename AS source_file,
    CAST(bucket_start AT TIME ZONE 'UTC' AS DATE) AS day,
    exchange,
    market_type,
    capture_version,
    count(*) AS rows,
    count(*) FILTER (WHERE price_complete) AS price_complete,
    count(*) FILTER (WHERE trades_complete) AS trades_complete,
    count(*) FILTER (WHERE open_interest_complete) AS open_interest_complete,
    count(*) FILTER (WHERE open_interest IS NOT NULL) AS native_oi_present,
    count(*) FILTER (WHERE open_interest_event_at IS NOT NULL) AS oi_event_at_present,
    count(*) FILTER (WHERE open_interest_observed_at IS NOT NULL) AS oi_observed_at_present,
    count(*) FILTER (WHERE open_interest_value IS NOT NULL) AS oi_value_present,
    count(*) FILTER (
        WHERE last_bid_price > 0 AND last_ask_price > 0
          AND last_ask_price >= last_bid_price
    ) AS bid_ask_valid,
    count(*) FILTER (
        WHERE buy_total_notional_usd + sell_total_notional_usd > 0
    ) AS positive_flow,
    count(*) FILTER (
        WHERE created_at <= bucket_start + INTERVAL 5 MINUTE
    ) AS finalized_within_5m
FROM read_parquet(?, filename=true)
WHERE bucket_start >= ? AND bucket_start < ?
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3, 4, 5
"""


def audit_directory(out_dir: Path, *, start: date, end: date) -> InputAudit:
    """Verify every UTC day, then read only input availability from its Parquet."""
    import duckdb

    verified = tuple(_verified_input(out_dir, day) for day in _days(start, end))
    paths = [str(path.resolve()) for path, _ in verified]
    window_start = datetime(start.year, start.month, start.day, tzinfo=UTC)
    window_end = datetime(end.year, end.month, end.day, tzinfo=UTC)
    connection = duckdb.connect()
    try:
        rows = connection.execute(_COVERAGE_SQL, [paths, window_start, window_end]).fetchall()
    finally:
        connection.close()
    coverage = tuple(
        VenueDayCoverage(
            day=str(row[1]),
            file_name=Path(str(row[0])).name,
            exchange=str(row[2]),
            market_type=str(row[3]),
            capture_version=str(row[4]),
            rows=int(row[5]),
            price_complete=int(row[6]),
            trades_complete=int(row[7]),
            open_interest_complete=int(row[8]),
            native_oi_present=int(row[9]),
            oi_event_at_present=int(row[10]),
            oi_observed_at_present=int(row[11]),
            oi_value_present=int(row[12]),
            bid_ask_valid=int(row[13]),
            positive_flow=int(row[14]),
            finalized_within_5m=int(row[15]),
        )
        for row in rows
    )
    for _, manifest in verified:
        file_rows = tuple(row for row in coverage if row.file_name == manifest.file_name)
        observed = sum(row.rows for row in file_rows)
        if observed != manifest.row_count:
            raise ValueError(
                f"input row count mismatch for {manifest.file_name}: "
                f"manifest={manifest.row_count}, read={observed}"
            )
        if any(row.day != manifest.day for row in file_rows):
            raise ValueError(f"{manifest.file_name}: rows outside manifest day {manifest.day}")
    return InputAudit(
        audit_version=AUDIT_VERSION,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        days=tuple(
            DayInput(
                day=manifest.day,
                file_name=manifest.file_name,
                row_count=manifest.row_count,
                sha256=manifest.sha256,
                source_fingerprint=manifest.source_fingerprint or "",
            )
            for _, manifest in verified
        ),
        coverage=coverage,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    sys.stdout.write(
        audit_directory(args.cold_bars_dir, start=args.start_day, end=args.end_day).to_json()
    )


if __name__ == "__main__":
    main()
