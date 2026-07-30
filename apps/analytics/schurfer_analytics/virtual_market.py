"""CCXT adapter for exact-anchor virtual-strategy candle paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast

from .candle_anomaly_features import candle_anomaly_path_bounds
from .ohlcv import (
    ONE_MINUTE_MS,
    ONE_MINUTE_TIMEFRAME,
    TIMEFRAME,
    TIMEFRAME_MS,
    ceil_to_timeframe,
    fetch_candles,
    next_timeframe_after,
)
from .virtual_entry_challengers import challenger_path_bounds
from .virtual_strategy import (
    EpisodeSelection,
    MarketPath,
    exit_parameters,
    exit_policy_family_path_bounds,
    expected_path_bounds,
    select_episode_decision,
)

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode

ExchangeFactory = Callable[[], Any]
PathBounds = Callable[["ReplayDecision"], tuple[int, int]]
_MAX_CONCURRENT_FETCHES = 6
DECISION_MARKET_PATH_VERSION = "ccxt_5m_exact_decision_anchor_v1"
MAKER_MARKET_PATH_VERSION = "ccxt_1m_primary_5m_fallback_exact_anchor_v1"
MAKER_FILL_TIMEOUT_MINUTES = 15


@dataclass(frozen=True)
class DecisionMarketPath:
    decision_id: str
    path: MarketPath

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("decision market path requires a decision id")


@dataclass(frozen=True)
class MakerDecisionPaths:
    decision_id: str
    one_minute: MarketPath
    five_minute: MarketPath

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("maker decision paths require a decision id")


def _fingerprint_payloads(payloads: Iterable[dict[str, Any]]) -> str:
    """Hash a canonical JSON array without materializing the full payload twice."""
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, payload in enumerate(payloads):
        if index:
            digest.update(b",")
        digest.update(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )
    digest.update(b"]")
    return digest.hexdigest()


async def _process_by_exchange[InputT, OutputT](
    items: tuple[InputT, ...],
    factories: dict[str, ExchangeFactory],
    *,
    exchange_for: Callable[[InputT], str],
    unsupported: Callable[[InputT], OutputT],
    process: Callable[
        [Any, asyncio.Semaphore, InputT],
        Awaitable[OutputT],
    ],
) -> tuple[OutputT, ...]:
    """Process one venue at a time while preserving the original item order."""
    missing = object()
    results: list[OutputT | object] = [missing] * len(items)
    grouped: dict[str, list[tuple[int, InputT]]] = {}
    for index, item in enumerate(items):
        exchange_name = exchange_for(item)
        if exchange_name not in factories:
            results[index] = unsupported(item)
            continue
        grouped.setdefault(exchange_name, []).append((index, item))

    for exchange_name in sorted(grouped):
        exchange = factories[exchange_name]()
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
        indexed_items = grouped[exchange_name]
        try:
            exchange_results = await asyncio.gather(
                *(process(exchange, semaphore, item) for _, item in indexed_items)
            )
        finally:
            await asyncio.gather(
                exchange.close(clean_instance_data=True),
                return_exceptions=True,
            )
        for (index, _), result in zip(indexed_items, exchange_results, strict=True):
            results[index] = result

    if any(result is missing for result in results):
        raise RuntimeError("exchange processing left an item unresolved")
    return cast("tuple[OutputT, ...]", tuple(results))


def decision_market_path_fingerprint(paths: tuple[DecisionMarketPath, ...]) -> str:
    payloads = (
        {
            "decision_id": item.decision_id,
            "path": {
                "pump_event_id": item.path.pump_event_id,
                "exchange": item.path.exchange,
                "base": item.path.base,
                "status": item.path.status,
                "error": item.path.error,
                "candles": [asdict(candle) for candle in item.path.candles],
            },
        }
        for item in sorted(paths, key=lambda item: item.decision_id)
    )
    return _fingerprint_payloads(payloads)


def maker_market_path_fingerprint(paths: tuple[MakerDecisionPaths, ...]) -> str:
    payloads = (
        {
            "decision_id": item.decision_id,
            "one_minute": {
                "pump_event_id": item.one_minute.pump_event_id,
                "exchange": item.one_minute.exchange,
                "base": item.one_minute.base,
                "status": item.one_minute.status,
                "error": item.one_minute.error,
                "candles": [asdict(candle) for candle in item.one_minute.candles],
            },
            "five_minute": {
                "pump_event_id": item.five_minute.pump_event_id,
                "exchange": item.five_minute.exchange,
                "base": item.five_minute.base,
                "status": item.five_minute.status,
                "error": item.five_minute.error,
                "candles": [asdict(candle) for candle in item.five_minute.candles],
            },
        }
        for item in sorted(paths, key=lambda item: item.decision_id)
    )
    return _fingerprint_payloads(payloads)


def maker_path_bounds(
    decision: ReplayDecision,
    timeframe_ms: int,
) -> tuple[int, int]:
    """Cover the fill timeout plus the longest baseline exit after the last fill bar."""
    decision_ms = int(decision.ts.timestamp() * 1000)
    fill_start = next_timeframe_after(decision_ms, timeframe_ms)
    maximum_hold = exit_parameters(decision.pump_pct).max_hold_min
    end_ms = fill_start + (
        MAKER_FILL_TIMEOUT_MINUTES * 60 * 1000 + timeframe_ms + maximum_hold * 60 * 1000
    )
    if timeframe_ms == TIMEFRAME_MS:
        return min(ceil_to_timeframe(decision_ms), fill_start), end_ms
    return fill_start, end_ms


async def fetch_maker_decision_paths(
    decisions: tuple[ReplayDecision, ...],
    factories: dict[str, ExchangeFactory],
) -> tuple[MakerDecisionPaths, ...]:
    """Fetch exact-venue 1m primary and 5m fallback paths with shared clients."""
    selected: list[tuple[str, ReplayDecision]] = []
    for decision in decisions:
        if not decision.decision_id:
            raise ValueError("selected decisions must have decision ids")
        selected.append((decision.decision_id, decision))
    decision_ids = [decision_id for decision_id, _ in selected]
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("selected decisions must have unique decision ids")

    def missing_path(decision: ReplayDecision, status: str, error: str) -> MarketPath:
        return MarketPath(
            pump_event_id=decision.pump_event_id or 0,
            exchange=decision.exchange,
            base=decision.base,
            status=status,
            candles=(),
            error=error,
        )

    async def fetch_timeframe(
        exchange: Any,
        semaphore: asyncio.Semaphore,
        decision: ReplayDecision,
        *,
        timeframe: str,
        timeframe_ms: int,
    ) -> MarketPath:
        start_ms, end_ms = maker_path_bounds(decision, timeframe_ms)
        try:
            async with semaphore:
                candles = await fetch_candles(
                    exchange,
                    decision.base,
                    start_ms,
                    end_ms,
                    timeframe=timeframe,
                    timeframe_ms=timeframe_ms,
                )
        except Exception as exc:
            return missing_path(decision, "fetch_failed", str(exc)[:1000])
        if not candles:
            return missing_path(decision, "no_data", f"no {timeframe} candles returned")
        return MarketPath(
            pump_event_id=decision.pump_event_id or 0,
            exchange=decision.exchange,
            base=decision.base,
            status="complete",
            candles=tuple(candles),
        )

    async def fetch_one(
        exchange: Any,
        semaphore: asyncio.Semaphore,
        item: tuple[str, ReplayDecision],
    ) -> MakerDecisionPaths:
        decision_id, decision = item
        one_minute, five_minute = await asyncio.gather(
            fetch_timeframe(
                exchange,
                semaphore,
                decision,
                timeframe=ONE_MINUTE_TIMEFRAME,
                timeframe_ms=ONE_MINUTE_MS,
            ),
            fetch_timeframe(
                exchange,
                semaphore,
                decision,
                timeframe=TIMEFRAME,
                timeframe_ms=TIMEFRAME_MS,
            ),
        )
        return MakerDecisionPaths(decision_id, one_minute, five_minute)

    def unsupported(item: tuple[str, ReplayDecision]) -> MakerDecisionPaths:
        decision_id, decision = item
        error = f"unsupported exact anchor exchange: {decision.exchange or '<empty>'}"
        unavailable = missing_path(decision, "unsupported_exchange", error)
        return MakerDecisionPaths(decision_id, unavailable, unavailable)

    return await _process_by_exchange(
        tuple(selected),
        factories,
        exchange_for=lambda item: item[1].exchange,
        unsupported=unsupported,
        process=fetch_one,
    )


async def _fetch_market_paths(
    episodes: tuple[ReplayEpisode, ...],
    factories: dict[str, ExchangeFactory],
    bounds: PathBounds,
) -> tuple[MarketPath, ...]:
    selections = [(episode, select_episode_decision(episode)) for episode in episodes]

    async def fetch_one(
        exchange: Any,
        semaphore: asyncio.Semaphore,
        item: tuple[ReplayEpisode, EpisodeSelection],
    ) -> MarketPath:
        episode, selection = item
        decision = selection.decision
        start_ms, end_ms = bounds(selection.decision)
        try:
            async with semaphore:
                candles = await fetch_candles(
                    exchange,
                    decision.base,
                    start_ms,
                    end_ms,
                )
        except Exception as exc:
            return MarketPath(
                pump_event_id=episode.pump_event_id,
                exchange=decision.exchange,
                base=decision.base,
                status="fetch_failed",
                candles=(),
                error=str(exc)[:1000],
            )
        return MarketPath(
            pump_event_id=episode.pump_event_id,
            exchange=decision.exchange,
            base=decision.base,
            status="complete",
            candles=tuple(candles),
        )

    def unsupported(item: tuple[ReplayEpisode, EpisodeSelection]) -> MarketPath:
        episode, selection = item
        decision = selection.decision
        return MarketPath(
            pump_event_id=episode.pump_event_id,
            exchange=decision.exchange,
            base=decision.base,
            status="unsupported_exchange",
            candles=(),
            error=f"unsupported exact anchor exchange: {decision.exchange or '<empty>'}",
        )

    return await _process_by_exchange(
        tuple(selections),
        factories,
        exchange_for=lambda item: item[1].decision.exchange,
        unsupported=unsupported,
        process=fetch_one,
    )


async def fetch_market_paths(
    episodes: tuple[ReplayEpisode, ...],
    factories: dict[str, ExchangeFactory],
) -> tuple[MarketPath, ...]:
    """Fetch one complete baseline exact-venue path per episode."""
    return await _fetch_market_paths(episodes, factories, expected_path_bounds)


async def fetch_entry_challenger_paths(
    episodes: tuple[ReplayEpisode, ...],
    factories: dict[str, ExchangeFactory],
) -> tuple[MarketPath, ...]:
    """Fetch pre-decision context and delayed-entry exits for all entry variants."""
    return await _fetch_market_paths(episodes, factories, challenger_path_bounds)


async def fetch_candle_anomaly_paths(
    episodes: tuple[ReplayEpisode, ...],
    factories: dict[str, ExchangeFactory],
) -> tuple[MarketPath, ...]:
    """Fetch pre-decision feature context and baseline exits in one exact path."""
    return await _fetch_market_paths(episodes, factories, candle_anomaly_path_bounds)


async def fetch_exit_policy_paths(
    episodes: tuple[ReplayEpisode, ...],
    factories: dict[str, ExchangeFactory],
) -> tuple[MarketPath, ...]:
    """Fetch the longest exact-venue path required by the registered exit family."""
    return await _fetch_market_paths(episodes, factories, exit_policy_family_path_bounds)


async def fetch_decision_market_paths(
    decisions: tuple[ReplayDecision, ...],
    factories: dict[str, ExchangeFactory],
    *,
    bounds: PathBounds = expected_path_bounds,
) -> tuple[DecisionMarketPath, ...]:
    """Fetch exact-venue exit paths for explicitly selected decisions.

    One exchange client is shared across every selected decision on that venue. A
    decision id is the path key because one pump episode can select a different venue
    at each registered threshold.
    """
    selected: list[tuple[str, ReplayDecision]] = []
    for decision in decisions:
        decision_id = decision.decision_id
        if not decision_id:
            raise ValueError("selected decisions must have decision ids")
        selected.append((decision_id, decision))
    decision_ids = [decision_id for decision_id, _ in selected]
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("selected decisions must have unique decision ids")

    async def fetch_one(
        exchange: Any,
        semaphore: asyncio.Semaphore,
        item: tuple[str, ReplayDecision],
    ) -> DecisionMarketPath:
        decision_id, decision = item
        start_ms, end_ms = bounds(decision)
        try:
            async with semaphore:
                candles = await fetch_candles(exchange, decision.base, start_ms, end_ms)
        except Exception as exc:
            path = MarketPath(
                pump_event_id=decision.pump_event_id or 0,
                exchange=decision.exchange,
                base=decision.base,
                status="fetch_failed",
                candles=(),
                error=str(exc)[:1000],
            )
        else:
            path = MarketPath(
                pump_event_id=decision.pump_event_id or 0,
                exchange=decision.exchange,
                base=decision.base,
                status="complete",
                candles=tuple(candles),
            )
        return DecisionMarketPath(decision_id, path)

    def unsupported(item: tuple[str, ReplayDecision]) -> DecisionMarketPath:
        decision_id, decision = item
        path = MarketPath(
            pump_event_id=decision.pump_event_id or 0,
            exchange=decision.exchange,
            base=decision.base,
            status="unsupported_exchange",
            candles=(),
            error=f"unsupported exact anchor exchange: {decision.exchange or '<empty>'}",
        )
        return DecisionMarketPath(decision_id, path)

    return await _process_by_exchange(
        tuple(selected),
        factories,
        exchange_for=lambda item: item[1].exchange,
        unsupported=unsupported,
        process=fetch_one,
    )
