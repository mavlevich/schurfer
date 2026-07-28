"""CCXT adapter for exact-anchor virtual-strategy candle paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .candle_anomaly_features import candle_anomaly_path_bounds
from .ohlcv import fetch_candles
from .virtual_entry_challengers import challenger_path_bounds
from .virtual_strategy import (
    MarketPath,
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


@dataclass(frozen=True)
class DecisionMarketPath:
    decision_id: str
    path: MarketPath

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("decision market path requires a decision id")


def decision_market_path_fingerprint(paths: tuple[DecisionMarketPath, ...]) -> str:
    payload = [
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
    ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _fetch_market_paths(
    episodes: tuple[ReplayEpisode, ...],
    factories: dict[str, ExchangeFactory],
    bounds: PathBounds,
) -> tuple[MarketPath, ...]:
    selections = [(episode, select_episode_decision(episode)) for episode in episodes]
    required_exchanges = {
        selection.decision.exchange
        for _, selection in selections
        if selection.decision.exchange in factories
    }
    exchanges = {name: factories[name]() for name in sorted(required_exchanges)}
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

    async def fetch_one(
        episode: ReplayEpisode,
        exchange_name: str,
        base: str,
    ) -> MarketPath:
        exchange = exchanges.get(exchange_name)
        if exchange is None:
            return MarketPath(
                pump_event_id=episode.pump_event_id,
                exchange=exchange_name,
                base=base,
                status="unsupported_exchange",
                candles=(),
                error=f"unsupported exact anchor exchange: {exchange_name or '<empty>'}",
            )
        selection = select_episode_decision(episode)
        start_ms, end_ms = bounds(selection.decision)
        try:
            async with semaphore:
                candles = await fetch_candles(exchange, base, start_ms, end_ms)
        except Exception as exc:
            return MarketPath(
                pump_event_id=episode.pump_event_id,
                exchange=exchange_name,
                base=base,
                status="fetch_failed",
                candles=(),
                error=str(exc)[:1000],
            )
        return MarketPath(
            pump_event_id=episode.pump_event_id,
            exchange=exchange_name,
            base=base,
            status="complete",
            candles=tuple(candles),
        )

    try:
        paths = await asyncio.gather(
            *[
                fetch_one(
                    episode,
                    selection.decision.exchange,
                    selection.decision.base,
                )
                for episode, selection in selections
            ]
        )
    finally:
        await asyncio.gather(
            *[exchange.close() for exchange in exchanges.values()],
            return_exceptions=True,
        )
    return tuple(paths)


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

    required_exchanges = {
        decision.exchange for decision in decisions if decision.exchange in factories
    }
    exchanges = {name: factories[name]() for name in sorted(required_exchanges)}
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

    async def fetch_one(
        decision_id: str,
        decision: ReplayDecision,
    ) -> DecisionMarketPath:
        exchange = exchanges.get(decision.exchange)
        if exchange is None:
            path = MarketPath(
                pump_event_id=decision.pump_event_id or 0,
                exchange=decision.exchange,
                base=decision.base,
                status="unsupported_exchange",
                candles=(),
                error=f"unsupported exact anchor exchange: {decision.exchange or '<empty>'}",
            )
            return DecisionMarketPath(decision_id, path)
        start_ms, end_ms = expected_path_bounds(decision)
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

    try:
        paths = await asyncio.gather(
            *(fetch_one(decision_id, decision) for decision_id, decision in selected)
        )
    finally:
        await asyncio.gather(
            *(exchange.close() for exchange in exchanges.values()),
            return_exceptions=True,
        )
    return tuple(paths)
