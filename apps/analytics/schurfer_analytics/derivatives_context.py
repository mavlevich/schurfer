"""Bounded, read-only probes for recoverable derivatives context."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .derivatives_history import (
    DEFAULT_MAX_PAGES,
    DerivativesHistoryMethod,
    effective_timeframe,
    fetch_derivatives_history,
    measure_regular_window,
    source_timestamp_ms,
    timeframe_ms,
)

DERIVATIVES_CONTEXT_PROBE_VERSION = "derivatives_context_probe_v2"
DEFAULT_BEFORE_MINUTES = 240
DEFAULT_AFTER_MINUTES = 480
DEFAULT_FETCH_TIMEOUT_SECONDS = 15.0
MAX_WINDOW_MINUTES = 10_080

DeclaredSupport = bool | Literal["emulated"]
ProbeStatus = Literal[
    "sampled",
    "partial",
    "incomplete",
    "window_mismatch",
    "unsupported",
    "no_target",
    "symbol_unavailable",
    "client_init_failed",
    "load_markets_failed",
    "fetch_failed",
    "invalid_response",
    "no_data",
]


@dataclass(frozen=True)
class DerivativesContextTarget:
    event_id: int
    exchange: str
    base: str
    unified_symbol: str
    market_id: str | None
    identity_key: str | None
    anchor_at: datetime


@dataclass(frozen=True)
class DerivativesContextProbeResult:
    exchange: str
    method: str
    capability: str
    declared_support: DeclaredSupport
    status: ProbeStatus
    event_id: int | None
    base: str | None
    unified_symbol: str | None
    market_id: str | None
    identity_key: str | None
    anchor_at: datetime | None
    requested_since: datetime | None
    requested_until: datetime | None
    fetched_at: datetime
    returned_rows: int
    valid_timestamp_rows: int
    in_window_rows: int
    invalid_rows: int
    first_source_at: datetime | None
    last_source_at: datetime | None
    effective_timeframe: str | None = None
    request_count: int = 0
    expected_rows: int | None = None
    coverage_ratio: float | None = None
    covers_start: bool | None = None
    covers_end: bool | None = None
    missing_rows: int | None = None
    duplicate_rows: int = 0
    max_gap_minutes: float | None = None
    pagination_exhausted: bool = False
    error: str | None = None


ExchangeFactory = Callable[[], Any]


def target_fingerprint(targets: tuple[DerivativesContextTarget, ...]) -> str:
    payload = [
        asdict(target)
        for target in sorted(
            targets,
            key=lambda target: (
                target.exchange,
                target.anchor_at,
                target.event_id,
            ),
        )
    ]
    return _fingerprint(payload)


def result_fingerprint(results: tuple[DerivativesContextProbeResult, ...]) -> str:
    payload = [
        asdict(result)
        for result in sorted(
            results,
            key=lambda result: (result.exchange, result.method),
        )
    ]
    return _fingerprint(payload)


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _empty_result(
    exchange: str,
    method: DerivativesHistoryMethod,
    status: ProbeStatus,
    *,
    fetched_at: datetime,
    target: DerivativesContextTarget | None = None,
    declared_support: DeclaredSupport = False,
    requested_since: datetime | None = None,
    requested_until: datetime | None = None,
    effective_timeframe: str | None = None,
    error: str | None = None,
) -> DerivativesContextProbeResult:
    return DerivativesContextProbeResult(
        exchange=exchange,
        method=method.name,
        capability=method.capability,
        declared_support=declared_support,
        status=status,
        event_id=target.event_id if target else None,
        base=target.base if target else None,
        unified_symbol=target.unified_symbol if target else None,
        market_id=target.market_id if target else None,
        identity_key=target.identity_key if target else None,
        anchor_at=target.anchor_at if target else None,
        requested_since=requested_since,
        requested_until=requested_until,
        fetched_at=fetched_at,
        returned_rows=0,
        valid_timestamp_rows=0,
        in_window_rows=0,
        invalid_rows=0,
        first_source_at=None,
        last_source_at=None,
        effective_timeframe=effective_timeframe,
        error=error,
    )


async def _probe_method(
    exchange_name: str,
    exchange: Any,
    target: DerivativesContextTarget,
    method: DerivativesHistoryMethod,
    *,
    requested_since: datetime,
    requested_until: datetime,
    limit: int,
    max_pages: int,
    timeout_seconds: float,
) -> DerivativesContextProbeResult:
    timeframe = effective_timeframe(exchange_name, method)
    capabilities = getattr(exchange, "has", None)
    raw_support = capabilities.get(method.capability) if isinstance(capabilities, dict) else None
    declared_support: DeclaredSupport = (
        "emulated" if raw_support == "emulated" else raw_support is True
    )
    if not declared_support:
        return _empty_result(
            exchange_name,
            method,
            "unsupported",
            fetched_at=datetime.now(UTC),
            target=target,
            requested_since=requested_since,
            requested_until=requested_until,
            effective_timeframe=timeframe,
        )

    since_ms = int(requested_since.timestamp() * 1000)
    until_ms = int(requested_until.timestamp() * 1000)
    page_fetch = await fetch_derivatives_history(
        exchange,
        method,
        target.unified_symbol,
        timeframe=timeframe,
        since_ms=since_ms,
        until_ms=until_ms,
        limit=limit,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    response = page_fetch.rows
    timestamps = [source_timestamp_ms(row, method.row_kind) for row in response]
    valid_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    raw_in_window = [
        timestamp for timestamp in valid_timestamps if since_ms <= timestamp < until_ms
    ]
    invalid_rows = len(response) - len(valid_timestamps)
    step_ms = timeframe_ms(timeframe)
    if method.series_kind == "regular":
        if step_ms is None:
            raise ValueError(f"regular derivatives method {method.name} requires a timeframe")
        coverage = measure_regular_window(
            raw_in_window,
            since_ms=since_ms,
            until_ms=until_ms,
            step_ms=step_ms,
        )
        in_window = list(coverage.timestamps)
        expected_rows = coverage.expected_rows
        coverage_ratio = coverage.coverage_ratio
        covers_start = coverage.covers_start
        covers_end = coverage.covers_end
        missing_rows = coverage.missing_rows
        duplicate_rows = coverage.duplicate_rows
        max_gap_minutes = coverage.max_gap_minutes
    else:
        # Distinct events can legitimately share the same millisecond timestamp.
        in_window = sorted(raw_in_window)
        expected_rows = None
        coverage_ratio = None
        covers_start = None
        covers_end = None
        missing_rows = None
        duplicate_rows = 0
        max_gap_minutes = None

    error: str | None = None
    status: ProbeStatus
    if page_fetch.error_status is not None:
        status = page_fetch.error_status
        error = page_fetch.error
    elif not response:
        status = "no_data"
    elif not valid_timestamps:
        status = "invalid_response"
        error = "response contained no valid unified millisecond timestamps"
    elif not in_window:
        status = "window_mismatch"
        error = "valid rows did not overlap the requested window"
    elif invalid_rows:
        status = "partial"
        error = "response contained invalid unified timestamps"
    elif page_fetch.pagination_exhausted:
        status = "incomplete"
        error = page_fetch.error
    elif method.series_kind == "regular" and (
        missing_rows != 0
        or not covers_start
        or not covers_end
        or (
            max_gap_minutes is not None
            and step_ms is not None
            and max_gap_minutes > step_ms / 60_000
        )
    ):
        status = "incomplete"
        error = "regular series did not cover the complete requested window"
    else:
        status = "sampled"
    return DerivativesContextProbeResult(
        exchange=exchange_name,
        method=method.name,
        capability=method.capability,
        declared_support=declared_support,
        status=status,
        event_id=target.event_id,
        base=target.base,
        unified_symbol=target.unified_symbol,
        market_id=target.market_id,
        identity_key=target.identity_key,
        anchor_at=target.anchor_at,
        requested_since=requested_since,
        requested_until=requested_until,
        fetched_at=datetime.now(UTC),
        returned_rows=len(response),
        valid_timestamp_rows=len(valid_timestamps),
        in_window_rows=len(in_window),
        invalid_rows=invalid_rows,
        first_source_at=(
            datetime.fromtimestamp(min(in_window) / 1000, tz=UTC) if in_window else None
        ),
        last_source_at=(
            datetime.fromtimestamp(max(in_window) / 1000, tz=UTC) if in_window else None
        ),
        effective_timeframe=timeframe,
        request_count=page_fetch.request_count,
        expected_rows=expected_rows,
        coverage_ratio=coverage_ratio,
        covers_start=covers_start,
        covers_end=covers_end,
        missing_rows=missing_rows,
        duplicate_rows=duplicate_rows,
        max_gap_minutes=max_gap_minutes,
        pagination_exhausted=page_fetch.pagination_exhausted,
        error=error,
    )


async def probe_derivatives_context(
    targets: tuple[DerivativesContextTarget, ...],
    factories: dict[str, ExchangeFactory],
    methods: tuple[DerivativesHistoryMethod, ...],
    *,
    before_minutes: int,
    after_minutes: int,
    limit: int,
    timeout_seconds: float,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[DerivativesContextProbeResult, ...]:
    """Probe one recent point-in-time target per exchange."""
    if before_minutes < 0 or after_minutes <= 0:
        raise ValueError("probe window must have non-negative before and positive after")
    if not 1 <= limit <= 1000:
        raise ValueError("page limit must be between 1 and 1000")
    if not 1 <= max_pages <= 50:
        raise ValueError("max pages must be between 1 and 50")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("timeout must be in (0, 120]")
    target_exchanges = tuple(target.exchange for target in targets)
    if len(target_exchanges) != len(set(target_exchanges)):
        raise ValueError("derivatives context targets must be unique by exchange")
    method_names = tuple(method.name for method in methods)
    if len(method_names) != len(set(method_names)):
        raise ValueError("derivatives context methods must be unique")
    unknown_target_exchanges = set(target_exchanges).difference(factories)
    if unknown_target_exchanges:
        raise ValueError("every derivatives context target must have an exchange factory")
    target_by_exchange = {target.exchange: target for target in targets}

    async def probe_exchange(
        exchange_name: str,
        factory: ExchangeFactory,
    ) -> tuple[DerivativesContextProbeResult, ...]:
        target = target_by_exchange.get(exchange_name)
        if target is None:
            fetched_at = datetime.now(UTC)
            return tuple(
                _empty_result(
                    exchange_name,
                    method,
                    "no_target",
                    fetched_at=fetched_at,
                    effective_timeframe=effective_timeframe(exchange_name, method),
                )
                for method in methods
            )
        requested_since = target.anchor_at - timedelta(minutes=before_minutes)
        requested_until = target.anchor_at + timedelta(minutes=after_minutes)
        try:
            exchange = factory()
        except Exception as exc:
            fetched_at = datetime.now(UTC)
            return tuple(
                _empty_result(
                    exchange_name,
                    method,
                    "client_init_failed",
                    fetched_at=fetched_at,
                    target=target,
                    requested_since=requested_since,
                    requested_until=requested_until,
                    effective_timeframe=effective_timeframe(exchange_name, method),
                    error=f"client initialization failed: {str(exc)[:900]}",
                )
                for method in methods
            )
        try:
            try:
                await asyncio.wait_for(
                    exchange.load_markets(),
                    timeout=timeout_seconds,
                )
            except Exception as exc:
                fetched_at = datetime.now(UTC)
                return tuple(
                    _empty_result(
                        exchange_name,
                        method,
                        "load_markets_failed",
                        fetched_at=fetched_at,
                        target=target,
                        requested_since=requested_since,
                        requested_until=requested_until,
                        effective_timeframe=effective_timeframe(exchange_name, method),
                        error=str(exc)[:1000],
                    )
                    for method in methods
                )
            markets = getattr(exchange, "markets", None)
            if not isinstance(markets, dict) or target.unified_symbol not in markets:
                fetched_at = datetime.now(UTC)
                return tuple(
                    _empty_result(
                        exchange_name,
                        method,
                        "symbol_unavailable",
                        fetched_at=fetched_at,
                        target=target,
                        requested_since=requested_since,
                        requested_until=requested_until,
                        effective_timeframe=effective_timeframe(exchange_name, method),
                        error="recorded unified symbol is absent from current markets",
                    )
                    for method in methods
                )
            rows: list[DerivativesContextProbeResult] = []
            for method in methods:
                rows.append(
                    await _probe_method(
                        exchange_name,
                        exchange,
                        target,
                        method,
                        requested_since=requested_since,
                        requested_until=requested_until,
                        limit=limit,
                        max_pages=max_pages,
                        timeout_seconds=timeout_seconds,
                    )
                )
            return tuple(rows)
        finally:
            with suppress(Exception):
                await exchange.close()

    results = await asyncio.gather(
        *(probe_exchange(exchange_name, factory) for exchange_name, factory in factories.items())
    )
    return tuple(row for exchange_rows in results for row in exchange_rows)
