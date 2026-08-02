"""Resolve strategy-agnostic forward outcomes for every recorded trade decision."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog

from .config import Config
from .ohlcv import (
    TIMEFRAME_MINUTES,
    Candle,
    closed_candles,
    fetch_candles,
    finite_float,
    window_bounds,
)
from .outcome_models import Decision, Outcome

if TYPE_CHECKING:
    from .outcome_repository import OutcomeStore

log = structlog.get_logger()

EXTENDED_HORIZONS_MINUTES = (20_160, 30_240, 40_320)
EXTENDED_HORIZON_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
HORIZONS_MINUTES = (
    15,
    30,
    60,
    240,
    480,
    1_440,
    4_320,
    10_080,
    *EXTENDED_HORIZONS_MINUTES,
)
RESOLVER_VERSION = "forward_v1"
EXACT_OUTCOME_STATUSES = ("complete",)
FALLBACK_OUTCOME_STATUSES = (
    "complete_fallback",
    "complete_fallback_unsupported",
)
MEASURABLE_OUTCOME_STATUSES = EXACT_OUTCOME_STATUSES + FALLBACK_OUTCOME_STATUSES
_COMPLETE_COVERAGE = 1.0
# LBank exposes current perpetual market data but no supported historical perpetual
# OHLCV path. Its CCXT fetchOHLCV implementation is spot-only, so calling it with the
# scanner's swap symbols fails deterministically with "Invalid Trading Pair".
_UNSUPPORTED_PERPETUAL_OHLCV_EXCHANGES = frozenset({"lbank"})
_RETRYABLE_STATUSES = (
    "fetch_failed",
    "missing_ohlcv",
    "partial",
    "unsupported_exchange",
    "complete_fallback",
)


@dataclass(frozen=True)
class OutcomeConfig:
    db_url: str
    exchanges: tuple[str, ...]
    poll_interval_seconds: int = 300
    retry_after_seconds: int = 900
    batch_size: int = 50
    max_attempts: int = 8

    def __post_init__(self) -> None:
        positive_values = {
            "OUTCOME_POLL_INTERVAL": self.poll_interval_seconds,
            "OUTCOME_RETRY_AFTER": self.retry_after_seconds,
            "OUTCOME_BATCH_SIZE": self.batch_size,
            "OUTCOME_MAX_ATTEMPTS": self.max_attempts,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_env(cls) -> OutcomeConfig:
        base = Config()
        if not base.db_url:
            raise ValueError("DATABASE_URL is required for outcome-resolver")
        return cls(
            db_url=base.db_url,
            exchanges=tuple(base.exchanges),
            poll_interval_seconds=int(os.getenv("OUTCOME_POLL_INTERVAL", "300")),
            retry_after_seconds=int(os.getenv("OUTCOME_RETRY_AFTER", "900")),
            batch_size=int(os.getenv("OUTCOME_BATCH_SIZE", "50")),
            max_attempts=int(os.getenv("OUTCOME_MAX_ATTEMPTS", "8")),
        )


def _candidate_rows(features: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(features, dict):
        return []
    rows = features.get("candidate_exchanges")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _ranked_candidate_rows(features: dict[str, Any] | None) -> list[dict[str, Any]]:
    def change_pct(row: dict[str, Any]) -> float:
        parsed = finite_float(row.get("change_pct"))
        return parsed if parsed is not None else -math.inf

    return sorted(_candidate_rows(features), key=change_pct, reverse=True)


def price_anchor_exchange(decision: Decision) -> str | None:
    """Recover the venue that defined the decision price, independently of support."""
    rows = _ranked_candidate_rows(decision.features)
    if decision.exchange:
        return decision.exchange
    return next(
        (name for row in rows if isinstance(name := row.get("exchange"), str) and name),
        None,
    )


def exchange_candidates(decision: Decision, allowed: set[str]) -> list[str]:
    """Return supported price-anchor and measured fallback venues in priority order."""
    rows = _ranked_candidate_rows(decision.features)
    anchor = price_anchor_exchange(decision)
    result = [anchor] if anchor in allowed else []
    for row in rows:
        name = row.get("exchange")
        if isinstance(name, str) and name in allowed and name not in result:
            result.append(name)
    return result


def compute_outcome(
    decision: Decision,
    horizon_minutes: int,
    candles: list[Candle],
    *,
    anchor: str | None,
    source: str | None,
) -> Outcome:
    start_ms, end_ms, expected = window_bounds(decision.ts, horizon_minutes)
    window = closed_candles(candles, start_ms, end_ms)
    coverage = len(window) / expected if expected else None

    if decision.price is None or decision.price <= 0:
        return Outcome.unavailable(
            decision,
            horizon_minutes,
            anchor_exchange=anchor,
            source_exchange=source,
            status="missing_price",
            bars_count=len(window),
            expected_bars=expected,
            coverage_ratio=coverage,
        )
    if not window:
        return Outcome.unavailable(
            decision,
            horizon_minutes,
            anchor_exchange=anchor,
            source_exchange=source,
            status="missing_ohlcv",
            expected_bars=expected,
            coverage_ratio=coverage,
        )

    entry = decision.price
    forward = window[-1].close
    mfe = max(0.0, (entry - min(candle.low for candle in window)) / entry * 100)
    mae = max(0.0, (max(candle.high for candle in window) - entry) / entry * 100)
    short_return = (entry - forward) / entry * 100
    if coverage is not None and coverage < _COMPLETE_COVERAGE:
        status = "partial"
    elif source != anchor:
        status = "complete_fallback"
    else:
        status = "complete"
    return Outcome(
        decision_id=decision.decision_id,
        horizon_minutes=horizon_minutes,
        anchor_exchange=anchor,
        source_exchange=source,
        entry_price=entry,
        forward_price=forward,
        mfe_pct=mfe,
        mae_pct=mae,
        short_return_pct=short_return,
        bars_count=len(window),
        expected_bars=expected,
        coverage_ratio=coverage,
        status=status,
    )


def _failure_outcomes(
    decision: Decision,
    anchor: str | None,
    status: str,
    error: str,
) -> list[Outcome]:
    return [
        Outcome.unavailable(
            decision,
            horizon,
            anchor_exchange=anchor,
            status=status,
            expected_bars=window_bounds(decision.ts, horizon)[2],
            coverage_ratio=0.0,
            error=error[:1000],
        )
        for horizon in decision.horizons
    ]


def _best_outcome_for_horizon(
    decision: Decision,
    horizon: int,
    candidates: list[str],
    candles_by_exchange: dict[str, list[Candle]],
    anchor: str | None,
) -> Outcome:
    outcomes = [
        compute_outcome(
            decision,
            horizon,
            candles_by_exchange[name],
            anchor=anchor,
            source=name,
        )
        for name in candidates
        if name in candles_by_exchange
    ]
    exact_complete = next(
        (
            outcome
            for outcome in outcomes
            if outcome.source_exchange == anchor and outcome.status == "complete"
        ),
        None,
    )
    if exact_complete is not None:
        return exact_complete
    return max(
        outcomes,
        key=lambda outcome: (
            outcome.coverage_ratio or 0.0,
            outcome.source_exchange == anchor,
        ),
    )


async def _resolve_decision(
    decision: Decision,
    exchanges: dict[str, Any],
) -> list[Outcome]:
    candidates = exchange_candidates(decision, set(exchanges))
    anchor = price_anchor_exchange(decision)
    if decision.price is None or decision.price <= 0:
        return [
            compute_outcome(decision, horizon, [], anchor=anchor, source=None)
            for horizon in decision.horizons
        ]
    if not candidates:
        return _failure_outcomes(
            decision,
            anchor,
            "unsupported_exchange",
            "no supported candidate exchange",
        )

    fetch_candidates = [
        name for name in candidates if name not in _UNSUPPORTED_PERPETUAL_OHLCV_EXCHANGES
    ]
    if not fetch_candidates:
        unavailable = ", ".join(candidates)
        return _failure_outcomes(
            decision,
            anchor,
            "market_path_unavailable",
            f"perpetual OHLCV unsupported for candidate exchanges: {unavailable}",
        )

    start_ms, _, _ = window_bounds(decision.ts, min(decision.horizons))
    _, end_ms, expected = window_bounds(decision.ts, max(decision.horizons))
    expected = max(1, expected)
    candles_by_exchange: dict[str, list[Candle]] = {}
    errors: list[str] = []
    for name in fetch_candidates:
        try:
            fetched = await fetch_candles(exchanges[name], decision.base, start_ms, end_ms)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if not fetched:
            errors.append(f"{name}: no candles")
            continue
        candles_by_exchange[name] = fetched
        coverage = len(fetched) / expected
        if coverage >= _COMPLETE_COVERAGE:
            break
        errors.append(f"{name}: partial coverage {coverage:.1%}")

    if not candles_by_exchange:
        return _failure_outcomes(
            decision,
            anchor,
            "fetch_failed",
            "; ".join(errors) or "OHLCV fetch failed",
        )
    outcomes = [
        _best_outcome_for_horizon(
            decision,
            horizon,
            fetch_candidates,
            candles_by_exchange,
            anchor,
        )
        for horizon in decision.horizons
    ]
    if anchor in _UNSUPPORTED_PERPETUAL_OHLCV_EXCHANGES:
        return [
            replace(outcome, status="complete_fallback_unsupported")
            if outcome.status == "complete_fallback"
            else outcome
            for outcome in outcomes
        ]
    return outcomes


async def resolve_once(
    cfg: OutcomeConfig,
    exchanges: dict[str, Any],
    store: OutcomeStore,
) -> int:
    decisions = await store.load_due_decisions(
        horizons=HORIZONS_MINUTES,
        resolver_version=RESOLVER_VERSION,
        retryable_statuses=_RETRYABLE_STATUSES,
        max_attempts=cfg.max_attempts,
        retry_after_seconds=cfg.retry_after_seconds,
        batch_size=cfg.batch_size,
        extended_horizons=EXTENDED_HORIZONS_MINUTES,
        extended_strategy_versions=EXTENDED_HORIZON_STRATEGY_VERSIONS,
    )
    resolved: list[Outcome] = []
    for decision in decisions:
        resolved.extend(await _resolve_decision(decision, exchanges))

    await store.persist_outcomes(
        resolved,
        resolver_version=RESOLVER_VERSION,
        timeframe_minutes=TIMEFRAME_MINUTES,
    )
    if resolved:
        counts: dict[str, int] = {}
        for outcome in resolved:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        log.info("outcomes.resolved", count=len(resolved), statuses=counts)
    return len(resolved)
