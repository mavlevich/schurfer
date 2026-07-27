"""Bounded durable recovery of high-value derivatives context around pump episodes."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import ccxt
import structlog

from .derivatives_context import (
    DEFAULT_AFTER_MINUTES,
    DEFAULT_BEFORE_MINUTES,
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    MAX_WINDOW_MINUTES,
    DeclaredSupport,
    DerivativesContextObservation,
    DerivativesContextProbeResult,
    DerivativesContextTarget,
    DerivativesContextWork,
    ProbeStatus,
    collect_derivatives_context_target,
)
from .derivatives_history import (
    DEFAULT_FETCH_LIMIT,
    DEFAULT_MAX_PAGES,
    METHOD_BY_NAME,
    effective_limit,
    effective_timeframe,
)

if TYPE_CHECKING:
    from .derivatives_context_repository import DerivativesContextStore

log = structlog.get_logger()

DERIVATIVES_CONTEXT_RESOLVER_VERSION = "derivatives_context_v2"
DEFAULT_COHORT_START = datetime(2026, 7, 27, tzinfo=UTC)
RETRYABLE_STATUSES = (
    "fetch_failed",
    "invalid_response",
    "no_data",
    "partial",
    "incomplete",
    "window_mismatch",
    "symbol_unavailable",
    "load_markets_failed",
)

# Production persistence starts only with venue/method pairs that returned valid,
# timestamped data in the locked v2 probe. Price-like mark/index/premium candles stay
# recoverable on demand and are not duplicated into Postgres.
PERSISTED_METHODS_BY_EXCHANGE: dict[str, tuple[str, ...]] = {
    "binance": (
        "funding_rate_history",
        "open_interest_history",
        "long_short_ratio_history",
    ),
    "bybit": ("funding_rate_history", "open_interest_history"),
    "okx": ("funding_rate_history", "open_interest_history"),
    "gate": ("funding_rate_history",),
    "bitget": ("funding_rate_history",),
    "mexc": ("funding_rate_history",),
    "coinex": ("funding_rate_history",),
    "htx": (
        "funding_rate_history",
        "open_interest_history",
        "liquidations",
    ),
    "xt": ("funding_rate_history",),
    "blofin": ("funding_rate_history",),
}


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _parse_utc(name: str, default: datetime) -> datetime:
    raw = os.getenv(name)
    if raw is None:
        return default
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class DerivativesContextResolverConfig:
    enabled: bool = True
    cohort_start: datetime = DEFAULT_COHORT_START
    before_minutes: int = DEFAULT_BEFORE_MINUTES
    after_minutes: int = DEFAULT_AFTER_MINUTES
    fetch_limit: int = DEFAULT_FETCH_LIMIT
    max_pages: int = DEFAULT_MAX_PAGES
    timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS
    retry_after_seconds: int = 900
    batch_size: int = 8
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if self.cohort_start.tzinfo is None:
            raise ValueError("DERIVATIVES_CONTEXT_SINCE must include a timezone")
        if self.before_minutes < 0 or self.after_minutes <= 0:
            raise ValueError("derivatives context window is invalid")
        if self.before_minutes > MAX_WINDOW_MINUTES or self.after_minutes > MAX_WINDOW_MINUTES:
            raise ValueError(
                f"derivatives context windows cannot exceed {MAX_WINDOW_MINUTES} minutes"
            )
        if not 1 <= self.fetch_limit <= 1000:
            raise ValueError("DERIVATIVES_CONTEXT_FETCH_LIMIT must be between 1 and 1000")
        if not 1 <= self.max_pages <= 50:
            raise ValueError("DERIVATIVES_CONTEXT_MAX_PAGES must be between 1 and 50")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("DERIVATIVES_CONTEXT_TIMEOUT must be in (0, 120]")
        if self.retry_after_seconds <= 0:
            raise ValueError("DERIVATIVES_CONTEXT_RETRY_AFTER must be positive")
        if self.batch_size <= 0:
            raise ValueError("DERIVATIVES_CONTEXT_BATCH_SIZE must be positive")
        if self.max_attempts <= 0:
            raise ValueError("DERIVATIVES_CONTEXT_MAX_ATTEMPTS must be positive")

    @classmethod
    def from_env(cls) -> DerivativesContextResolverConfig:
        return cls(
            enabled=_parse_bool("DERIVATIVES_CONTEXT_ENABLED", True),
            cohort_start=_parse_utc("DERIVATIVES_CONTEXT_SINCE", DEFAULT_COHORT_START),
            before_minutes=int(
                os.getenv("DERIVATIVES_CONTEXT_BEFORE_MINUTES", str(DEFAULT_BEFORE_MINUTES))
            ),
            after_minutes=int(
                os.getenv("DERIVATIVES_CONTEXT_AFTER_MINUTES", str(DEFAULT_AFTER_MINUTES))
            ),
            fetch_limit=int(os.getenv("DERIVATIVES_CONTEXT_FETCH_LIMIT", str(DEFAULT_FETCH_LIMIT))),
            max_pages=int(os.getenv("DERIVATIVES_CONTEXT_MAX_PAGES", str(DEFAULT_MAX_PAGES))),
            timeout_seconds=float(
                os.getenv(
                    "DERIVATIVES_CONTEXT_TIMEOUT",
                    str(DEFAULT_FETCH_TIMEOUT_SECONDS),
                )
            ),
            retry_after_seconds=int(os.getenv("DERIVATIVES_CONTEXT_RETRY_AFTER", "900")),
            batch_size=int(os.getenv("DERIVATIVES_CONTEXT_BATCH_SIZE", "8")),
            max_attempts=int(os.getenv("DERIVATIVES_CONTEXT_MAX_ATTEMPTS", "8")),
        )

    def supported_pairs(self, exchanges: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        return tuple(
            (exchange, method)
            for exchange in exchanges
            for method in PERSISTED_METHODS_BY_EXCHANGE.get(exchange, ())
        )


def _failed_observations(
    target: DerivativesContextTarget,
    methods: tuple[str, ...],
    exchange: Any,
    *,
    cfg: DerivativesContextResolverConfig,
    status: ProbeStatus,
    error: str,
) -> tuple[DerivativesContextObservation, ...]:
    requested_since = target.anchor_at - timedelta(minutes=cfg.before_minutes)
    requested_until = target.anchor_at + timedelta(minutes=cfg.after_minutes)
    fetched_at = datetime.now(UTC)

    def declared_support(method_name: str) -> DeclaredSupport:
        capabilities = getattr(exchange, "has", None)
        raw = (
            capabilities.get(METHOD_BY_NAME[method_name].capability)
            if isinstance(capabilities, dict)
            else None
        )
        return "emulated" if raw == "emulated" else raw is True

    return tuple(
        DerivativesContextObservation(
            result=DerivativesContextProbeResult(
                exchange=target.exchange,
                method=method_name,
                capability=METHOD_BY_NAME[method_name].capability,
                declared_support=declared_support(method_name),
                status=status,
                event_id=target.event_id,
                base=target.base,
                unified_symbol=target.unified_symbol,
                market_id=target.market_id,
                identity_key=target.identity_key,
                anchor_at=target.anchor_at,
                requested_since=requested_since,
                requested_until=requested_until,
                fetched_at=fetched_at,
                returned_rows=0,
                valid_timestamp_rows=0,
                in_window_rows=0,
                invalid_rows=0,
                first_source_at=None,
                last_source_at=None,
                effective_timeframe=effective_timeframe(
                    target.exchange,
                    METHOD_BY_NAME[method_name],
                ),
                effective_limit=effective_limit(
                    target.exchange,
                    METHOD_BY_NAME[method_name],
                    cfg.fetch_limit,
                ),
                error=error[:1000],
            ),
            samples=(),
        )
        for method_name in methods
    )


def _group_work(
    work: tuple[DerivativesContextWork, ...],
) -> tuple[tuple[DerivativesContextTarget, tuple[str, ...]], ...]:
    grouped: dict[DerivativesContextTarget, list[str]] = defaultdict(list)
    for item in work:
        grouped[item.target].append(item.method)
    return tuple(
        (target, tuple(methods))
        for target, methods in sorted(
            grouped.items(),
            key=lambda item: (
                item[0].anchor_at,
                item[0].event_id,
                item[0].exchange,
            ),
        )
    )


async def resolve_derivatives_context_once(
    cfg: DerivativesContextResolverConfig,
    exchanges: dict[str, Any],
    store: DerivativesContextStore,
) -> int:
    """Resolve and atomically persist one bounded work batch."""
    supported_pairs = cfg.supported_pairs(tuple(exchanges))
    work = await store.load_due_work(
        supported_pairs=supported_pairs,
        resolver_version=DERIVATIVES_CONTEXT_RESOLVER_VERSION,
        cohort_start=cfg.cohort_start,
        after_minutes=cfg.after_minutes,
        retryable_statuses=RETRYABLE_STATUSES,
        max_attempts=cfg.max_attempts,
        retry_after_seconds=cfg.retry_after_seconds,
        batch_size=cfg.batch_size,
    )
    observations: list[DerivativesContextObservation] = []
    loaded_exchanges: set[str] = set()
    load_failures: dict[str, str] = {}
    for target, method_names in _group_work(work):
        exchange = exchanges[target.exchange]
        if target.exchange not in loaded_exchanges and target.exchange not in load_failures:
            try:
                await asyncio.wait_for(
                    exchange.load_markets(),
                    timeout=cfg.timeout_seconds,
                )
            except Exception as exc:
                load_failures[target.exchange] = str(exc)
            else:
                loaded_exchanges.add(target.exchange)
        if target.exchange in load_failures:
            observations.extend(
                _failed_observations(
                    target,
                    method_names,
                    exchange,
                    cfg=cfg,
                    status="load_markets_failed",
                    error=load_failures[target.exchange],
                )
            )
            continue
        observations.extend(
            await collect_derivatives_context_target(
                target.exchange,
                exchange,
                target,
                tuple(METHOD_BY_NAME[name] for name in method_names),
                before_minutes=cfg.before_minutes,
                after_minutes=cfg.after_minutes,
                limit=cfg.fetch_limit,
                max_pages=cfg.max_pages,
                timeout_seconds=cfg.timeout_seconds,
            )
        )

    await store.persist_observations(
        tuple(observations),
        resolver_version=DERIVATIVES_CONTEXT_RESOLVER_VERSION,
        ccxt_version=ccxt.__version__,
    )
    if observations:
        statuses: dict[str, int] = defaultdict(int)
        for observation in observations:
            statuses[observation.result.status] += 1
        log.info(
            "derivatives_context.resolved",
            work_count=len(work),
            sample_count=sum(len(observation.samples) for observation in observations),
            statuses=dict(statuses),
        )
    return len(work)
