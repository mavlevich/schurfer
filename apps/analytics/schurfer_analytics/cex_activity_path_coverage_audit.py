"""Read-only audit of why HYP-016's frozen result
(research/cex-activity-discovery-result-v1) marked most signal and control
paths `incomplete_24h_path`.

Scope, agreed with colleague review before this module was written: uses
ONLY the `request_id`/`symbol`/`entry_at` values already present in the
frozen artifact (fingerprint `_AUDITED_ARTIFACT_FINGERPRINT` below) -- no
new candidate detection, no threshold changes, no outcome-label
re-derivation, and no write path at all. Runs exactly one read-only query
per unresolved request against the same `timeseries.bybit_momentum_bars_1m`
table `cex_activity_discovery_repository.fetch_exact_paths` already reads,
but WITHOUT that query's own completeness/positive-price filters, so every
expected minute in `[entry_at, entry_at + 1440m)` can be classified instead
of only counted.

`ExactPricePath.unresolved_reason` (`cex_activity_discovery.py`) already
tells us THAT a path is `incomplete_24h_path`; this module answers WHY, by
attributing each of that path's missing minutes to one of:

- `row_absent` -- no row at all for that (symbol, bucket_start).
- `price_incomplete_or_null` -- a row exists but `price_complete` is not
  `true` (`false` or, for rows written before migration 0030, `NULL`).
- `invalid_or_missing_ohlc` -- a row exists with `price_complete = true`
  but open/high/low/close is missing or not strictly positive (checked
  independently of `price_complete` as a defense-in-depth category; not
  observed to ever actually fire with `price_complete = true` in the real
  audited data, but a real, nameable category rather than an assumption).

A wrong-`capture_version` category was deliberately NOT implemented as a
per-minute check: a direct count confirmed exactly one `capture_version`
(`'v1'`) exists across the whole `2026-08-17`..`2026-08-29` span this
audit's requests fall in, so that category is empty by construction here
and checking it per-minute would be pure overhead. If this module is ever
reused for a different artifact where that no longer holds, add it back
before trusting the result.

Findings from the one real run this module was built to produce (full
numbers in `docs/research/discovery-ledger.md`'s HYP-016 row and
`docs/research/cex-activity-path-coverage-audit-v1.md`): missingness is
real (not a resolver bug -- spot-checked five requests' independently
recomputed `observed_minutes` against the frozen artifact's own value,
exact match every time), clusters heavily by symbol rather than being
uniform noise, is dominated by isolated single-minute gaps with a smaller
population of genuine 8-12-minute outages (several of which hit many
symbols at the same minute), and shows no evidence of delisting or
universe exit (every audited symbol's own last available bar is from
today, long after this window).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_capture_contract import BYBIT_MOMENTUM_MARKET_TYPE
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence

# The one artifact this audit was built against -- see the module
# docstring. Not a general-purpose parameter: a different artifact could
# have a different capture_version landscape (see the module docstring's
# note on that), so reusing this module elsewhere needs a fresh look, not
# just a new fingerprint value passed in.
_AUDITED_ARTIFACT_FINGERPRINT = "382ac208890119447c09e2945b7869c29711ca826e054cbe8676b71f6d74bbb1"
_DEFAULT_ARTIFACT_PATH = Path(
    f"/runtime/research-dataset-artifacts/38/{_AUDITED_ARTIFACT_FINGERPRINT}/data.json"
)

_EXCHANGE = "bybit"
_CAPTURE_VERSION = "v1"
_OUTCOME_HORIZON_MINUTES = 1440
_GLOBAL_OUTAGE_MIN_SYMBOLS = 5

_RAW_MINUTES_SQL = text("""
    SELECT bucket_start, price_complete, open_price, high_price, low_price, close_price
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = :exchange AND market_type = :market_type
      AND capture_version = :capture_version AND symbol = :symbol
      AND bucket_start >= :entry_at
      AND bucket_start < :entry_at + make_interval(mins => :horizon)
""")

_SYMBOL_BOUNDS_SQL = text("""
    SELECT min(bucket_start), max(bucket_start)
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = :exchange AND market_type = :market_type
      AND capture_version = :capture_version AND symbol = :symbol
""")


@dataclass(frozen=True)
class _RawMinute:
    price_complete: bool | None
    open_price: float | None
    high_price: float | None
    low_price: float | None
    close_price: float | None

    @property
    def is_bad(self) -> bool:
        if self.price_complete is not True:
            return True
        return not all(
            value is not None and value > 0
            for value in (self.open_price, self.high_price, self.low_price, self.close_price)
        )

    @property
    def reason(self) -> str | None:
        if self.price_complete is not True:
            return "price_incomplete_or_null"
        if self.is_bad:
            return "invalid_or_missing_ohlc"
        return None


@dataclass(frozen=True)
class UnresolvedPathRequest:
    """One `incomplete_24h_path` request already present in the frozen
    artifact -- this audit never invents a request, only re-reads the ones
    the real freeze already produced."""

    kind: str  # "signal" or "control"
    direction: str
    symbol: str
    entry_at: datetime
    request_id: str


@dataclass(frozen=True)
class PathCoverageAuditRow:
    request: UnresolvedPathRequest
    reason_counts: dict[str, int]
    missing_total: int
    longest_consecutive_gap: int
    symbol_first_bar: datetime | None
    symbol_last_bar: datetime | None


@dataclass(frozen=True)
class PathCoverageAuditReport:
    rows: tuple[PathCoverageAuditRow, ...]
    reason_totals: dict[str, int]
    reason_totals_by_kind: dict[str, dict[str, int]]
    longest_gap_histogram: dict[int, int]
    missing_minutes_by_symbol: dict[str, int]
    candidate_global_outage_minutes: dict[datetime, int]
    possible_delisting_requests: tuple[str, ...] = field(default_factory=tuple)


def _classify_row(rows: dict[datetime, _RawMinute], minute: datetime) -> _RawMinute | None:
    return rows.get(minute)


def audit_one_request(
    rows: dict[datetime, _RawMinute], entry_at: datetime
) -> tuple[dict[str, int], int]:
    """Returns (reason_counts excluding a clean minute, longest_consecutive_gap).
    Pure function, no I/O -- the query happens once per request in
    `audit_path_coverage`, this only classifies what it already fetched."""
    reason_counts: Counter[str] = Counter()
    longest = current = 0
    for i in range(_OUTCOME_HORIZON_MINUTES):
        minute = entry_at + timedelta(minutes=i)
        row = _classify_row(rows, minute)
        if row is None:
            reason_counts["row_absent"] += 1
            current += 1
        else:
            reason = row.reason
            if reason is not None:
                reason_counts[reason] += 1
            if row.is_bad:
                current += 1
            else:
                current = 0
                continue
        longest = max(longest, current)
    return dict(reason_counts), longest


def _unresolved_requests_from_artifact(
    episodes: Sequence[dict[str, Any]],
) -> list[UnresolvedPathRequest]:
    requests: list[UnresolvedPathRequest] = []
    for episode in episodes:
        signal_path = episode["signal_path"]
        if signal_path.get("unresolved_reason") == "incomplete_24h_path":
            requests.append(
                UnresolvedPathRequest(
                    kind="signal",
                    direction=episode["direction"],
                    symbol=signal_path["symbol"],
                    entry_at=datetime.fromisoformat(signal_path["entry_at"]),
                    request_id=signal_path["request_id"],
                )
            )
        for control_path in episode.get("control_paths", []):
            if control_path.get("unresolved_reason") == "incomplete_24h_path":
                requests.append(
                    UnresolvedPathRequest(
                        kind="control",
                        direction=episode["direction"],
                        symbol=control_path["symbol"],
                        entry_at=datetime.fromisoformat(control_path["entry_at"]),
                        request_id=control_path["request_id"],
                    )
                )
    return requests


async def audit_path_coverage(
    engine: AsyncEngine,
    *,
    artifact_path: Path = _DEFAULT_ARTIFACT_PATH,
    market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
) -> PathCoverageAuditReport:
    with artifact_path.open() as handle:
        episodes = json.load(handle)
    requests = _unresolved_requests_from_artifact(episodes)

    audit_rows: list[PathCoverageAuditRow] = []
    reason_totals: Counter[str] = Counter()
    reason_totals_by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    missing_minutes_by_symbol: Counter[str] = Counter()
    bad_minute_symbols: dict[datetime, set[str]] = defaultdict(set)
    symbol_bounds: dict[str, tuple[datetime | None, datetime | None]] = {}

    async with engine.connect() as connection:
        # Read-only for the whole audit -- this module never writes
        # anything, this just makes that guarantee explicit at the
        # transaction level too, matching every other production-touching
        # path in this codebase.
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        for request in requests:
            result = await connection.execute(
                _RAW_MINUTES_SQL,
                {
                    "exchange": _EXCHANGE,
                    "market_type": market_type,
                    "capture_version": _CAPTURE_VERSION,
                    "symbol": request.symbol,
                    "entry_at": request.entry_at,
                    "horizon": _OUTCOME_HORIZON_MINUTES,
                },
            )
            rows = {
                row.bucket_start: _RawMinute(
                    price_complete=row.price_complete,
                    open_price=row.open_price,
                    high_price=row.high_price,
                    low_price=row.low_price,
                    close_price=row.close_price,
                )
                for row in result
            }
            reason_counts, longest_gap = audit_one_request(rows, request.entry_at)
            missing_total = sum(reason_counts.values())

            if request.symbol not in symbol_bounds:
                bounds_row = (
                    await connection.execute(
                        _SYMBOL_BOUNDS_SQL,
                        {
                            "exchange": _EXCHANGE,
                            "market_type": market_type,
                            "capture_version": _CAPTURE_VERSION,
                            "symbol": request.symbol,
                        },
                    )
                ).one()
                symbol_bounds[request.symbol] = (bounds_row[0], bounds_row[1])
            first_bar, last_bar = symbol_bounds[request.symbol]

            audit_rows.append(
                PathCoverageAuditRow(
                    request=request,
                    reason_counts=reason_counts,
                    missing_total=missing_total,
                    longest_consecutive_gap=longest_gap,
                    symbol_first_bar=first_bar,
                    symbol_last_bar=last_bar,
                )
            )
            reason_totals.update(reason_counts)
            reason_totals_by_kind[request.kind].update(reason_counts)
            missing_minutes_by_symbol[request.symbol] += missing_total
            for i in range(_OUTCOME_HORIZON_MINUTES):
                minute = request.entry_at + timedelta(minutes=i)
                row = rows.get(minute)
                if row is None or row.is_bad:
                    bad_minute_symbols[minute].add(request.symbol)

    longest_gap_histogram: Counter[int] = Counter(row.longest_consecutive_gap for row in audit_rows)
    candidate_global_outage_minutes = {
        minute: len(symbols)
        for minute, symbols in bad_minute_symbols.items()
        if len(symbols) >= _GLOBAL_OUTAGE_MIN_SYMBOLS
    }
    possible_delisting = tuple(
        row.request.request_id
        for row in audit_rows
        if row.symbol_last_bar is not None
        and row.symbol_last_bar < row.request.entry_at + timedelta(minutes=_OUTCOME_HORIZON_MINUTES)
    )

    return PathCoverageAuditReport(
        rows=tuple(audit_rows),
        reason_totals=dict(reason_totals),
        reason_totals_by_kind={k: dict(v) for k, v in reason_totals_by_kind.items()},
        longest_gap_histogram=dict(sorted(longest_gap_histogram.items())),
        missing_minutes_by_symbol=dict(missing_minutes_by_symbol),
        candidate_global_outage_minutes=candidate_global_outage_minutes,
        possible_delisting_requests=possible_delisting,
    )


def render_markdown(
    report: PathCoverageAuditReport, *, code_revision: str, working_tree_dirty: bool
) -> str:
    lines = [
        "# CEX Activity Path Coverage Audit",
        "",
        f"Audited artifact fingerprint: `{_AUDITED_ARTIFACT_FINGERPRINT}`",
        f"Audit code revision: `{code_revision}`{' (dirty)' if working_tree_dirty else ''}",
        f"Unresolved requests audited: {len(report.rows)}",
        "",
        "## Reason totals",
        "",
        "| Reason | Count |",
        "| --- | --- |",
    ]
    for reason, count in sorted(report.reason_totals.items()):
        lines.append(f"| {reason} | {count} |")
    lines += [
        "",
        "## Reason totals by kind",
        "",
        "| Kind | Reason | Count |",
        "| --- | --- | --- |",
    ]
    for kind, reasons in sorted(report.reason_totals_by_kind.items()):
        for reason, count in sorted(reasons.items()):
            lines.append(f"| {kind} | {reason} | {count} |")
    lines += [
        "",
        "## Longest-consecutive-gap distribution",
        "",
        "| Longest gap (minutes) | Requests |",
        "| --- | --- |",
    ]
    for gap, count in report.longest_gap_histogram.items():
        lines.append(f"| {gap} | {count} |")
    lines += [
        "",
        "## Per-symbol missing-minute totals (top 20)",
        "",
        "| Symbol | Missing minutes | First bar (all-time) | Last bar (all-time) |",
        "| --- | --- | --- | --- |",
    ]
    symbol_bars = {
        row.request.symbol: (row.symbol_first_bar, row.symbol_last_bar) for row in report.rows
    }
    top_symbols = sorted(report.missing_minutes_by_symbol.items(), key=lambda item: -item[1])[:20]
    for symbol, total in top_symbols:
        first_bar, last_bar = symbol_bars.get(symbol, (None, None))
        lines.append(f"| {symbol} | {total} | {first_bar} | {last_bar} |")
    lines += [
        "",
        "## Candidate global-outage minutes "
        f"(>= {_GLOBAL_OUTAGE_MIN_SYMBOLS} distinct symbols affected)",
        "",
        f"Total candidate minutes: {len(report.candidate_global_outage_minutes)}",
        "",
        "## Possible delisting/universe-exit signal",
        "",
        f"Requests whose symbol's own last bar falls before the request's own "
        f"window end: {len(report.possible_delisting_requests)}",
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-path", type=Path, default=_DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    return parser


def main() -> None:
    import os

    args = build_parser().parse_args()
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(async_database_url(database_url), pool_pre_ping=True)
    try:
        report = asyncio.run(audit_path_coverage(engine, artifact_path=args.artifact_path))
    finally:
        asyncio.run(engine.dispose())
    sys.stdout.write(
        render_markdown(
            report, code_revision=args.code_revision, working_tree_dirty=args.working_tree_dirty
        )
    )


if __name__ == "__main__":
    main()
