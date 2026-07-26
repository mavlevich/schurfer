"""CCXT adapter for exact-anchor virtual-strategy candle paths."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .ohlcv import fetch_candles
from .virtual_entry_challengers import challenger_path_bounds
from .virtual_strategy import MarketPath, expected_path_bounds, select_episode_decision

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode

ExchangeFactory = Callable[[], Any]
PathBounds = Callable[["ReplayDecision"], tuple[int, int]]
_MAX_CONCURRENT_FETCHES = 6


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
