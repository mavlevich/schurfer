"""Bounded, read-only probes for recoverable derivatives context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

DERIVATIVES_CONTEXT_PROBE_VERSION = "derivatives_context_probe_v1"
DEFAULT_BEFORE_MINUTES = 240
DEFAULT_AFTER_MINUTES = 480
DEFAULT_FETCH_LIMIT = 200
DEFAULT_FETCH_TIMEOUT_SECONDS = 15.0
MAX_WINDOW_MINUTES = 10_080
TIMEFRAME = "5m"

DeclaredSupport = bool | Literal["emulated"]
ProbeStatus = Literal[
    "sampled",
    "partial",
    "unsupported",
    "no_target",
    "symbol_unavailable",
    "client_init_failed",
    "load_markets_failed",
    "fetch_failed",
    "invalid_response",
    "no_data",
]
RowKind = Literal["object", "ohlcv"]


@dataclass(frozen=True)
class DerivativesContextMethod:
    name: str
    capability: str
    callable_name: str
    row_kind: RowKind
    timeframe: str | None = None


METHODS: tuple[DerivativesContextMethod, ...] = (
    DerivativesContextMethod(
        "funding_rate_history",
        "fetchFundingRateHistory",
        "fetch_funding_rate_history",
        "object",
    ),
    DerivativesContextMethod(
        "open_interest_history",
        "fetchOpenInterestHistory",
        "fetch_open_interest_history",
        "object",
        TIMEFRAME,
    ),
    DerivativesContextMethod(
        "mark_ohlcv",
        "fetchMarkOHLCV",
        "fetch_mark_ohlcv",
        "ohlcv",
        TIMEFRAME,
    ),
    DerivativesContextMethod(
        "index_ohlcv",
        "fetchIndexOHLCV",
        "fetch_index_ohlcv",
        "ohlcv",
        TIMEFRAME,
    ),
    DerivativesContextMethod(
        "premium_index_ohlcv",
        "fetchPremiumIndexOHLCV",
        "fetch_premium_index_ohlcv",
        "ohlcv",
        TIMEFRAME,
    ),
    DerivativesContextMethod(
        "long_short_ratio_history",
        "fetchLongShortRatioHistory",
        "fetch_long_short_ratio_history",
        "object",
        TIMEFRAME,
    ),
    DerivativesContextMethod(
        "liquidations",
        "fetchLiquidations",
        "fetch_liquidations",
        "object",
    ),
)
METHOD_BY_NAME = {method.name: method for method in METHODS}


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


def _source_timestamp_ms(row: Any, row_kind: RowKind) -> int | None:
    raw: Any
    if row_kind == "ohlcv":
        if not isinstance(row, list | tuple) or not row:
            return None
        raw = row[0]
    else:
        if not isinstance(row, dict):
            return None
        raw = row.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if not math.isfinite(float(raw)):
        return None
    timestamp_ms = int(raw)
    # Unified CCXT timestamps are milliseconds. Do not silently repair seconds here:
    # detecting that parser defect is one purpose of the conformance probe.
    if timestamp_ms < 946684800000:
        return None
    return timestamp_ms


def _empty_result(
    exchange: str,
    method: DerivativesContextMethod,
    status: ProbeStatus,
    *,
    fetched_at: datetime,
    target: DerivativesContextTarget | None = None,
    declared_support: DeclaredSupport = False,
    requested_since: datetime | None = None,
    requested_until: datetime | None = None,
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
        error=error,
    )


async def _call_method(
    exchange: Any,
    method: DerivativesContextMethod,
    symbol: str,
    since_ms: int,
    limit: int,
) -> Any:
    fetcher = getattr(exchange, method.callable_name)
    if method.timeframe is None:
        return await fetcher(symbol, since_ms, limit)
    return await fetcher(symbol, method.timeframe, since_ms, limit)


async def _probe_method(
    exchange_name: str,
    exchange: Any,
    target: DerivativesContextTarget,
    method: DerivativesContextMethod,
    *,
    requested_since: datetime,
    requested_until: datetime,
    limit: int,
    timeout_seconds: float,
) -> DerivativesContextProbeResult:
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
        )

    since_ms = int(requested_since.timestamp() * 1000)
    until_ms = int(requested_until.timestamp() * 1000)
    try:
        response = await asyncio.wait_for(
            _call_method(
                exchange,
                method,
                target.unified_symbol,
                since_ms,
                limit,
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return _empty_result(
            exchange_name,
            method,
            "fetch_failed",
            fetched_at=datetime.now(UTC),
            target=target,
            declared_support=declared_support,
            requested_since=requested_since,
            requested_until=requested_until,
            error=str(exc)[:1000],
        )
    if not isinstance(response, list):
        return _empty_result(
            exchange_name,
            method,
            "invalid_response",
            fetched_at=datetime.now(UTC),
            target=target,
            declared_support=declared_support,
            requested_since=requested_since,
            requested_until=requested_until,
            error=f"expected list response, got {type(response).__name__}",
        )

    timestamps = [_source_timestamp_ms(row, method.row_kind) for row in response]
    valid_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    in_window = [timestamp for timestamp in valid_timestamps if since_ms <= timestamp < until_ms]
    invalid_rows = len(response) - len(valid_timestamps)
    error: str | None = None
    if not response:
        status: ProbeStatus = "no_data"
    elif not valid_timestamps:
        status = "invalid_response"
        error = "response contained no valid unified millisecond timestamps"
    elif invalid_rows:
        status = "partial"
        error = "response contained invalid unified timestamps"
    elif not in_window:
        status = "no_data"
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
        error=error,
    )


async def probe_derivatives_context(
    targets: tuple[DerivativesContextTarget, ...],
    factories: dict[str, ExchangeFactory],
    methods: tuple[DerivativesContextMethod, ...],
    *,
    before_minutes: int,
    after_minutes: int,
    limit: int,
    timeout_seconds: float,
) -> tuple[DerivativesContextProbeResult, ...]:
    """Probe one recent point-in-time target per exchange."""
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
