"""Bounded exact-symbol one-minute paths for source-lead candidates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .ohlcv import ONE_MINUTE_MS, ONE_MINUTE_TIMEFRAME, fetch_symbol_candles
from .source_lead import (
    SourceLeadCandidate,
    SourceLeadPath,
    source_lead_path_bounds,
)

ExchangeFactory = Callable[[], Any]
_MAX_CONCURRENT_FETCHES = 6


async def fetch_source_lead_paths(
    candidates: tuple[SourceLeadCandidate, ...],
    factories: dict[str, ExchangeFactory],
    *,
    on_exchange: Callable[[str, int, int], None] | None = None,
) -> tuple[SourceLeadPath, ...]:
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("source-lead candidates must have unique ids")

    missing = object()
    results: list[SourceLeadPath | object] = [missing] * len(candidates)
    grouped: dict[str, list[tuple[int, SourceLeadCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        exchange = candidate.execution_exchange
        if exchange not in factories:
            results[index] = SourceLeadPath(
                candidate_id=candidate.candidate_id,
                event_id=candidate.event_id,
                exchange=exchange,
                symbol=candidate.exact_symbol,
                status="unsupported_exchange",
                candles=(),
                error=f"unsupported execution exchange: {exchange}",
            )
            continue
        grouped.setdefault(exchange, []).append((index, candidate))

    exchange_names = sorted(grouped)
    for exchange_index, exchange_name in enumerate(exchange_names, start=1):
        if on_exchange:
            on_exchange(exchange_name, exchange_index, len(exchange_names))
        exchange = factories[exchange_name]()
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

        async def fetch_one(
            candidate: SourceLeadCandidate,
            *,
            exchange_client: Any = exchange,
            concurrency: asyncio.Semaphore = semaphore,
        ) -> SourceLeadPath:
            start_ms, end_ms = source_lead_path_bounds(candidate)
            try:
                async with concurrency:
                    candles = await fetch_symbol_candles(
                        exchange_client,
                        candidate.exact_symbol,
                        start_ms,
                        end_ms,
                        timeframe=ONE_MINUTE_TIMEFRAME,
                        timeframe_ms=ONE_MINUTE_MS,
                    )
            except Exception as exc:
                return SourceLeadPath(
                    candidate.candidate_id,
                    candidate.event_id,
                    candidate.execution_exchange,
                    candidate.exact_symbol,
                    "fetch_failed",
                    (),
                    str(exc)[:1000],
                )
            return SourceLeadPath(
                candidate.candidate_id,
                candidate.event_id,
                candidate.execution_exchange,
                candidate.exact_symbol,
                "complete" if candles else "no_data",
                tuple(candles),
                None if candles else "no one-minute candles returned",
            )

        indexed = grouped[exchange_name]
        try:
            for offset in range(0, len(indexed), _MAX_CONCURRENT_FETCHES):
                batch = indexed[offset : offset + _MAX_CONCURRENT_FETCHES]
                fetched_paths = await asyncio.gather(
                    *(fetch_one(candidate) for _, candidate in batch)
                )
                for (index, _), path in zip(batch, fetched_paths, strict=True):
                    results[index] = path
        finally:
            await asyncio.gather(exchange.close(clean_instance_data=True), return_exceptions=True)

    if any(result is missing for result in results):
        raise RuntimeError("source-lead path loading left a candidate unresolved")
    paths: list[SourceLeadPath] = []
    for result in results:
        if not isinstance(result, SourceLeadPath):
            raise RuntimeError("source-lead path loading produced an invalid result")
        paths.append(result)
    return tuple(paths)
