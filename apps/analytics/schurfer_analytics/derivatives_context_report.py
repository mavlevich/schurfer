"""Read-only coverage report for recoverable derivatives history."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import ccxt

from .derivatives_context import (
    DEFAULT_AFTER_MINUTES,
    DEFAULT_BEFORE_MINUTES,
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    DERIVATIVES_CONTEXT_PROBE_VERSION,
    MAX_WINDOW_MINUTES,
    DeclaredSupport,
    DerivativesContextProbeResult,
    DerivativesContextTarget,
    probe_derivatives_context,
    result_fingerprint,
    target_fingerprint,
)
from .derivatives_history import DEFAULT_FETCH_LIMIT, DEFAULT_MAX_PAGES, METHOD_BY_NAME
from .exchange_registry import DEFAULT_EXCHANGES, EXCHANGE_FACTORIES
from .reporting import (
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)

DERIVATIVES_CONTEXT_REPORT_VERSION = "derivatives_context_report_v2"
DEFAULT_DATASET_LOOKBACK_DAYS = 14


class ProbeConfigurationError(ValueError):
    """Raised for an invalid bounded-probe configuration."""


@dataclass(frozen=True)
class DerivativesContextFilters:
    since: datetime
    until: datetime
    exchanges: tuple[str, ...]
    methods: tuple[str, ...]
    before_minutes: int
    after_minutes: int
    fetch_limit: int
    max_pages: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.since >= self.until:
            raise ProbeConfigurationError("--since must be earlier than --until")
        if not self.exchanges:
            raise ProbeConfigurationError("at least one exchange is required")
        if len(self.exchanges) != len(set(self.exchanges)):
            raise ProbeConfigurationError("exchanges must be unique")
        if any(exchange not in EXCHANGE_FACTORIES for exchange in self.exchanges):
            raise ProbeConfigurationError("unknown exchange")
        if not self.methods:
            raise ProbeConfigurationError("at least one method is required")
        if len(self.methods) != len(set(self.methods)):
            raise ProbeConfigurationError("methods must be unique")
        if any(method not in METHOD_BY_NAME for method in self.methods):
            raise ProbeConfigurationError("unknown derivatives context method")
        if self.before_minutes < 0 or self.after_minutes <= 0:
            raise ProbeConfigurationError(
                "--before-minutes must be non-negative and --after-minutes positive"
            )
        if self.before_minutes > MAX_WINDOW_MINUTES or self.after_minutes > MAX_WINDOW_MINUTES:
            raise ProbeConfigurationError(
                f"probe windows cannot exceed {MAX_WINDOW_MINUTES} minutes per side"
            )
        if not 1 <= self.fetch_limit <= 1000:
            raise ProbeConfigurationError("--limit must be between 1 and 1000")
        if not 1 <= self.max_pages <= 50:
            raise ProbeConfigurationError("--max-pages must be between 1 and 50")
        if not 0 < self.timeout_seconds <= 120:
            raise ProbeConfigurationError("--timeout-seconds must be in (0, 120]")


@dataclass(frozen=True)
class DerivativesContextManifest:
    report_version: str
    probe_version: str
    ccxt_version: str
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    filters: DerivativesContextFilters
    target_fingerprint: str
    result_fingerprint: str
    scope: str = "read_only_capability_and_recoverability_probe"


@dataclass(frozen=True)
class StatusCount:
    status: str
    count: int


@dataclass(frozen=True)
class MethodCoverage:
    method: str
    exchanges: int
    targets: int
    declared_supported: int
    sampled: int
    partial: int
    incomplete: int
    window_mismatch: int
    no_data: int
    unsupported: int
    failures: int


@dataclass(frozen=True)
class DerivativesContextReport:
    manifest: DerivativesContextManifest
    target_count: int
    statuses: tuple[StatusCount, ...]
    methods: tuple[MethodCoverage, ...]
    targets: tuple[DerivativesContextTarget, ...]
    results: tuple[DerivativesContextProbeResult, ...]


def _support_label(value: DeclaredSupport) -> str:
    if value == "emulated":
        return "emulated"
    return "yes" if value else "no"


def _method_coverage(
    method: str,
    results: tuple[DerivativesContextProbeResult, ...],
) -> MethodCoverage:
    selected = tuple(result for result in results if result.method == method)
    return MethodCoverage(
        method=method,
        exchanges=len(selected),
        targets=sum(result.event_id is not None for result in selected),
        declared_supported=sum(result.declared_support is not False for result in selected),
        sampled=sum(result.status == "sampled" for result in selected),
        partial=sum(result.status == "partial" for result in selected),
        incomplete=sum(result.status == "incomplete" for result in selected),
        window_mismatch=sum(result.status == "window_mismatch" for result in selected),
        no_data=sum(result.status == "no_data" for result in selected),
        unsupported=sum(result.status == "unsupported" for result in selected),
        failures=sum(
            result.status
            in {
                "symbol_unavailable",
                "client_init_failed",
                "load_markets_failed",
                "fetch_failed",
                "invalid_response",
                "window_mismatch",
            }
            for result in selected
        ),
    )


def build_report(
    filters: DerivativesContextFilters,
    targets: tuple[DerivativesContextTarget, ...],
    results: tuple[DerivativesContextProbeResult, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> DerivativesContextReport:
    normalized_revision = normalize_code_revision(code_revision)
    expected_pairs = {
        (exchange, method) for exchange in filters.exchanges for method in filters.methods
    }
    actual_pairs = {(result.exchange, result.method) for result in results}
    if actual_pairs != expected_pairs or len(results) != len(expected_pairs):
        raise ValueError("probe results must contain one row per exchange and method")
    status_counts = Counter(result.status for result in results)
    return DerivativesContextReport(
        manifest=DerivativesContextManifest(
            report_version=DERIVATIVES_CONTEXT_REPORT_VERSION,
            probe_version=DERIVATIVES_CONTEXT_PROBE_VERSION,
            ccxt_version=ccxt.__version__,
            generated_at=generated_at,
            code_revision=normalized_revision,
            working_tree_dirty=working_tree_dirty,
            filters=filters,
            target_fingerprint=target_fingerprint(targets),
            result_fingerprint=result_fingerprint(results),
        ),
        target_count=len(targets),
        statuses=tuple(
            StatusCount(status, count)
            for status, count in sorted(
                status_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ),
        methods=tuple(_method_coverage(method, results) for method in filters.methods),
        targets=targets,
        results=results,
    )


def render_json(report: DerivativesContextReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: DerivativesContextReport) -> str:
    manifest = report.manifest
    filters = manifest.filters
    lines = [
        "# Derivatives Context Coverage Probe",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"CCXT: `{manifest.ccxt_version}`",
        f"Probe contract: `{manifest.probe_version}`",
        f"Target fingerprint: `{manifest.target_fingerprint}`",
        f"Result fingerprint: `{manifest.result_fingerprint}`",
        (f"Dataset scope: {filters.since.isoformat()} <= anchor < {filters.until.isoformat()}"),
        (
            f"Probe window: anchor - {filters.before_minutes}m"
            f" through anchor + {filters.after_minutes}m"
        ),
        "",
        (
            "> Read-only capability and recoverability probe. A declared CCXT "
            "capability is not evidence until the bounded request returns valid "
            "timestamped rows."
        ),
        "",
        "## Coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Configured exchanges", len(filters.exchanges)),
                ("Selected targets", report.target_count),
                ("Methods", len(filters.methods)),
                ("Page limit per method", filters.fetch_limit),
                ("Maximum pages per method", filters.max_pages),
                ("Timeout per request", f"{filters.timeout_seconds:g}s"),
            ],
        )
    )
    lines.extend(["", "## Statuses", ""])
    lines.extend(
        markdown_table(
            ("Status", "Rows"),
            [(row.status, row.count) for row in report.statuses],
        )
    )
    lines.extend(["", "## Method coverage", ""])
    lines.extend(
        markdown_table(
            (
                "Method",
                "Exchanges",
                "Targets",
                "Declared",
                "Sampled",
                "Partial",
                "Incomplete",
                "Window mismatch",
                "No data",
                "Unsupported",
                "Failures",
            ),
            [
                (
                    row.method,
                    row.exchanges,
                    row.targets,
                    row.declared_supported,
                    row.sampled,
                    row.partial,
                    row.incomplete,
                    row.window_mismatch,
                    row.no_data,
                    row.unsupported,
                    row.failures,
                )
                for row in report.methods
            ],
        )
    )
    lines.extend(["", "## Probe results", ""])
    lines.extend(
        markdown_table(
            (
                "Exchange",
                "Method",
                "Target",
                "Status",
                "Declared",
                "TF",
                "Requests",
                "Rows",
                "In window",
                "Coverage",
                "Bounds",
                "Missing / dupes",
                "Max gap",
                "First",
                "Last",
                "Error",
            ),
            [
                (
                    row.exchange,
                    row.method,
                    (f"{row.base} (event {row.event_id})" if row.event_id is not None else "none"),
                    row.status,
                    _support_label(row.declared_support),
                    row.effective_timeframe or "event",
                    row.request_count,
                    row.returned_rows,
                    row.in_window_rows,
                    (
                        f"{row.in_window_rows}/{row.expected_rows} ({row.coverage_ratio:.1%})"
                        if row.expected_rows is not None and row.coverage_ratio is not None
                        else "event"
                    ),
                    (
                        f"{'yes' if row.covers_start else 'no'}/{'yes' if row.covers_end else 'no'}"
                        if row.covers_start is not None and row.covers_end is not None
                        else "n/a"
                    ),
                    (
                        f"{row.missing_rows if row.missing_rows is not None else 'n/a'}"
                        f" / {row.duplicate_rows}"
                    ),
                    (f"{row.max_gap_minutes:g}m" if row.max_gap_minutes is not None else "n/a"),
                    row.first_source_at.isoformat() if row.first_source_at else "",
                    row.last_source_at.isoformat() if row.last_source_at else "",
                    row.error or "",
                )
                for row in report.results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe recoverable historical derivatives context")
    parser.add_argument("--since", type=parse_utc_datetime)
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument(
        "--exchange",
        action="append",
        choices=DEFAULT_EXCHANGES,
        dest="exchanges",
    )
    parser.add_argument(
        "--method",
        action="append",
        choices=tuple(METHOD_BY_NAME),
        dest="methods",
    )
    parser.add_argument("--before-minutes", type=int, default=DEFAULT_BEFORE_MINUTES)
    parser.add_argument("--after-minutes", type=int, default=DEFAULT_AFTER_MINUTES)
    parser.add_argument("--limit", type=int, default=DEFAULT_FETCH_LIMIT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_FETCH_TIMEOUT_SECONDS,
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .derivatives_context_repository import DerivativesContextRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ProbeConfigurationError("DATABASE_URL is required for derivatives-context-report")
    try:
        code_revision = normalize_code_revision(args.code_revision or "")
    except ValueError as exc:
        raise ProbeConfigurationError("--code-revision or SCHURFER_GIT_SHA is required") from exc
    generated_at = datetime.now(UTC)
    until = args.until or generated_at
    if until > generated_at:
        raise ProbeConfigurationError("--until cannot be in the future")
    filters = DerivativesContextFilters(
        since=args.since or (until - timedelta(days=DEFAULT_DATASET_LOOKBACK_DAYS)),
        until=until,
        exchanges=tuple(args.exchanges or DEFAULT_EXCHANGES),
        methods=tuple(args.methods or METHOD_BY_NAME),
        before_minutes=args.before_minutes,
        after_minutes=args.after_minutes,
        fetch_limit=args.limit,
        max_pages=args.max_pages,
        timeout_seconds=args.timeout_seconds,
    )
    repository = DerivativesContextRepository.from_url(db_url)
    try:
        targets = await repository.load_latest_targets(
            filters.exchanges,
            since=filters.since,
            until=filters.until,
            after_minutes=filters.after_minutes,
        )
    finally:
        await repository.close()
    selected_factories = {exchange: EXCHANGE_FACTORIES[exchange] for exchange in filters.exchanges}
    selected_methods = tuple(METHOD_BY_NAME[method] for method in filters.methods)
    results = await probe_derivatives_context(
        targets,
        selected_factories,
        selected_methods,
        before_minutes=filters.before_minutes,
        after_minutes=filters.after_minutes,
        limit=filters.fetch_limit,
        max_pages=filters.max_pages,
        timeout_seconds=filters.timeout_seconds,
    )
    report = build_report(
        filters,
        targets,
        results,
        generated_at=generated_at,
        code_revision=code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ProbeConfigurationError as exc:
        parser.error(str(exc))
    sys.stdout.write(output)
