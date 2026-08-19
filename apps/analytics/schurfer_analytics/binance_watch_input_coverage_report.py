"""Binance WATCH input-coverage report (ROADMAP item 8, PR4 of the
Binance-remediation sequence: feat/momentum-trade-price-source-v1 ->
fix/binance-oi-poll-scheduler-v1 -> analysis/binance-watch-input-coverage-v1
-> feat/binance-momentum-watch-v2, conditional).

Descriptive only: no threshold tuning, no outcomes, no decision to
re-enable momentum-watch-binance/momentum-paper-binance made by this
script -- a human reads the numbers below and decides. See
docs/research/binance-watch-input-readiness-v1.md and
docs/research/binance-oi-poll-scheduler-v1.md for the two root causes
this report measures the aftermath of.

momentum-watch-binance itself is deliberately kept stopped on prod (see
those two docs' own "What's next"), so no momentum_flow_watch_evaluations_1m
rows exist to read for this window -- there is nothing live to query.
This report instead REPLAYS the exact same, real
momentum_flow_watch_evaluator.prepare_symbol_evaluation against
already-captured bars for every bucket in the window, using
BINANCE_WATCH_CONTRACT unchanged. This is a deliberate choice over a
hand-rolled SQL approximation of the quality gate: prepare_symbol_evaluation
IS the frozen v1 contract, so a report answering "would WATCH have
produced a real decision here" has to run the real function, not a
similar-looking reimplementation that could quietly drift from it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from .momentum_flow_watch_contract import BINANCE_WATCH_CONTRACT, WatchContract
from .momentum_flow_watch_evaluator import QualityReason, prepare_symbol_evaluation
from .reporting import json_ready as _json_ready
from .reporting import markdown_table as _table
from .reporting import parse_utc_datetime

parse_datetime = parse_utc_datetime

# due_buckets' own real-time decision-delay margin (see that method's own
# doc comment in momentum_flow_watch_repository.py) is not reproducible
# for a bucket already hours or days in the past -- there is no "now" to
# be a fixed distance behind. DEFAULT_DECISION_DELAY_SECONDS instead fixes
# evaluator_started_at at a constant offset after each bucket_start, close
# to what a live worker's own cohort-catch-up cadence would give a bucket
# under normal (not backlogged) operation: 60s for the bucket to close
# plus a modest margin. Override via --decision-delay-seconds if a
# different assumption is worth comparing.
DEFAULT_DECISION_DELAY_SECONDS = 90

# When --until is omitted, defaulting straight to datetime.now(UTC) (a
# code-review finding) would let the trailing 1-2 buckets get replayed
# before capture has actually finished writing/completing them -- not a
# real quality-gate failure, just real time not having caught up yet,
# which would deflate quality_ready_pct for the newest hour and mislead
# the human reading this report. WRITER_MATURITY_BUFFER_SECONDS pads
# past decision_delay_seconds for ordinary writer flush latency
# (writerFlushInterval is 5s in both capture binaries, this is a
# generous multiple of that) before a bucket is considered mature enough
# to replay.
WRITER_MATURITY_BUFFER_SECONDS = 30

# list_bucket_starts_in_window has no limit of its own (unlike
# due_buckets, always called with an explicit one) -- a code-review
# finding: load_bucket is called once per bucket_start, sequentially,
# each re-querying the full lookback window from scratch, so an
# unbounded multi-day window turns an ad hoc report into a slow,
# DB-hammering job. DEFAULT_MAX_BUCKETS comfortably covers this report's
# own stated 24-48h use case (2880 one-minute buckets = 48h) with
# headroom; a wider window must pass --max-buckets explicitly rather
# than silently truncating and mislabeling what was actually covered.
DEFAULT_MAX_BUCKETS = 3000


@dataclass(frozen=True)
class CoverageWindow:
    since: datetime
    until: datetime
    decision_delay_seconds: int

    def __post_init__(self) -> None:
        if self.since >= self.until:
            raise ValueError("since must be earlier than until")
        if self.decision_delay_seconds <= 0:
            raise ValueError("decision_delay_seconds must be positive")


@dataclass(frozen=True)
class HourlyRow:
    hour_start: datetime
    evaluations: int
    quality_ready: int
    quality_ready_pct: float


@dataclass(frozen=True)
class ReasonRow:
    reason: str
    count: int
    pct: float


@dataclass(frozen=True)
class BinanceWatchInputCoverageReport:
    generated_at: datetime
    window: CoverageWindow
    buckets_evaluated: int
    total_evaluations: int
    quality_ready: int
    quality_ready_pct: float
    reasons: tuple[ReasonRow, ...]
    hourly: tuple[HourlyRow, ...]


def build_report(
    window: CoverageWindow,
    buckets_evaluated: int,
    quality_ready_flags: list[bool],
    reason_tuples: list[tuple[QualityReason, ...]],
    hour_starts: list[datetime],
) -> BinanceWatchInputCoverageReport:
    """Pure aggregation: every list here is the same length (one entry per
    (symbol, bucket) evaluation), so this has no I/O and is unit-testable
    without a database. reason_tuples entries may contain more than one
    reason (prepare_symbol_evaluation itself never short-circuits after
    the first failing check -- see its own doc comment), so a single
    evaluation can be counted in more than one reason row; the reason
    percentages therefore do not have to sum to 100%."""
    total = len(quality_ready_flags)
    ready = sum(quality_ready_flags)

    reason_counts: Counter[str] = Counter()
    for reasons in reason_tuples:
        reason_counts.update(reasons)
    reason_rows = tuple(
        sorted(
            (
                ReasonRow(reason=reason, count=count, pct=(count / total * 100 if total else 0.0))
                for reason, count in reason_counts.items()
            ),
            key=lambda row: (-row.count, row.reason),
        )
    )

    hourly_totals: Counter[datetime] = Counter()
    hourly_ready: Counter[datetime] = Counter()
    for hour_start, ready_flag in zip(hour_starts, quality_ready_flags, strict=True):
        hourly_totals[hour_start] += 1
        if ready_flag:
            hourly_ready[hour_start] += 1
    hourly_rows = tuple(
        HourlyRow(
            hour_start=hour_start,
            evaluations=hourly_totals[hour_start],
            quality_ready=hourly_ready[hour_start],
            quality_ready_pct=(
                hourly_ready[hour_start] / hourly_totals[hour_start] * 100
                if hourly_totals[hour_start]
                else 0.0
            ),
        )
        for hour_start in sorted(hourly_totals)
    )

    return BinanceWatchInputCoverageReport(
        generated_at=datetime.now(UTC),
        window=window,
        buckets_evaluated=buckets_evaluated,
        total_evaluations=total,
        quality_ready=ready,
        quality_ready_pct=(ready / total * 100 if total else 0.0),
        reasons=reason_rows,
        hourly=hourly_rows,
    )


def render_json(report: BinanceWatchInputCoverageReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: BinanceWatchInputCoverageReport) -> str:
    lines = [
        "# Binance WATCH Input-Coverage Report",
        "",
        "Descriptive only -- no threshold tuning, no outcomes, no re-enable",
        "decision made by this report.",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Window: {report.window.since.isoformat()} to {report.window.until.isoformat()}",
        f"Decision delay assumed: {report.window.decision_delay_seconds}s after each bucket_start",
        "",
        f"Buckets evaluated: {report.buckets_evaluated}",
        f"(symbol, bucket) evaluations: {report.total_evaluations}",
        f"quality_ready: {report.quality_ready} ({report.quality_ready_pct:.1f}%)",
        "",
        "## Quality-reject reasons",
        "",
        "(An evaluation can carry more than one reason; percentages need not",
        "sum to 100%.)",
        "",
    ]
    lines.extend(
        _table(
            ("Reason", "Count", "% of evaluations"),
            [(row.reason, row.count, f"{row.pct:.1f}%") for row in report.reasons],
        )
    )
    lines.extend(["", "## Hourly quality_ready rate", ""])
    lines.extend(
        _table(
            ("Hour (UTC)", "Evaluations", "quality_ready", "%"),
            [
                (
                    row.hour_start.isoformat(),
                    row.evaluations,
                    row.quality_ready,
                    f"{row.quality_ready_pct:.1f}%",
                )
                for row in report.hourly
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay prepare_symbol_evaluation against already-captured Binance bars "
            "to descriptively measure WATCH input coverage -- momentum-watch-binance "
            "itself stays stopped; this report does not start it."
        )
    )
    parser.add_argument(
        "--since", type=parse_datetime, required=True, help="inclusive UTC ISO-8601"
    )
    parser.add_argument(
        "--until",
        type=parse_datetime,
        help=(
            "exclusive UTC ISO-8601; default now minus decision-delay-seconds plus "
            f"{WRITER_MATURITY_BUFFER_SECONDS}s, so the trailing buckets are never "
            "replayed before capture has actually finished writing them"
        ),
    )
    parser.add_argument(
        "--decision-delay-seconds",
        type=int,
        default=DEFAULT_DECISION_DELAY_SECONDS,
        help=f"default {DEFAULT_DECISION_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--max-buckets",
        type=int,
        default=DEFAULT_MAX_BUCKETS,
        help=f"default {DEFAULT_MAX_BUCKETS}; fails loudly rather than silently truncating",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def resolve_until(
    explicit_until: datetime | None, decision_delay_seconds: int, now: datetime
) -> datetime:
    """Only the auto-default (explicit_until is None) gets the maturity
    margin; an explicit --until is trusted as-is (the caller may
    deliberately want a window that ends mid-incident, before any margin
    would apply). Pure so the maturity-margin arithmetic is unit-testable
    without a real clock or a database."""
    if explicit_until is not None:
        return explicit_until
    return now - timedelta(seconds=decision_delay_seconds + WRITER_MATURITY_BUFFER_SECONDS)


def check_bucket_count(bucket_count: int, max_buckets: int) -> None:
    """Fails loudly rather than silently truncating -- see
    DEFAULT_MAX_BUCKETS's own doc comment for why an unbounded window is
    a real problem, not just a slow one."""
    if bucket_count > max_buckets:
        raise ValueError(
            f"window contains {bucket_count} buckets, over --max-buckets={max_buckets}; "
            "narrow --since/--until or raise --max-buckets explicitly rather than "
            "silently evaluating a truncated window"
        )


async def _run(args: argparse.Namespace) -> str:
    from .momentum_flow_watch_repository import MomentumFlowWatchRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for binance-watch-input-coverage-report")

    until = resolve_until(args.until, args.decision_delay_seconds, datetime.now(UTC))
    window = CoverageWindow(
        since=args.since, until=until, decision_delay_seconds=args.decision_delay_seconds
    )
    contract: WatchContract = BINANCE_WATCH_CONTRACT

    repository = MomentumFlowWatchRepository.from_url(db_url)
    try:
        bucket_starts = await repository.list_bucket_starts_in_window(
            contract=contract, since=window.since, until=window.until
        )
        check_bucket_count(len(bucket_starts), args.max_buckets)
        quality_ready_flags: list[bool] = []
        reason_tuples: list[tuple[QualityReason, ...]] = []
        hour_starts: list[datetime] = []
        for bucket_start in bucket_starts:
            bucket_input = await repository.load_bucket(
                contract=contract, bucket_start=bucket_start
            )
            if bucket_input is None:
                continue
            evaluator_started_at = bucket_start + timedelta(seconds=window.decision_delay_seconds)
            hour_start = bucket_start.replace(minute=0, second=0, microsecond=0)
            for symbol in bucket_input.symbols:
                prepared = prepare_symbol_evaluation(
                    symbol=symbol,
                    bucket_start=bucket_start,
                    bars=bucket_input.bars_by_symbol[symbol],
                    evaluator_started_at=evaluator_started_at,
                    contract=contract,
                )
                quality_ready_flags.append(prepared.quality_ready)
                reason_tuples.append(prepared.quality_reasons)
                hour_starts.append(hour_start)
        report = build_report(
            window, len(bucket_starts), quality_ready_flags, reason_tuples, hour_starts
        )
    finally:
        await repository.close()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
