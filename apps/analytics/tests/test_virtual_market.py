from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_entry_challengers import challenger_path_bounds
from schurfer_analytics.virtual_market import (
    fetch_entry_challenger_paths,
    fetch_market_paths,
)


def _episode(
    exchange: str = "binance",
    *,
    event_id: int = 42,
    base: str = "ERA",
) -> ReplayEpisode:
    ts = datetime(2026, 7, 26, 12, 1, tzinfo=UTC)
    decision = ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=ts - timedelta(minutes=1),
        event_closed_at=ts + timedelta(hours=1),
        ts=ts,
        base=base,
        exchange=exchange,
        action="skipped",
        reason="score",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={"config": {"signal_position_usd": 50}},
        liquidity={"status": "sampled"},
        outcomes=(),
    )
    return ReplayEpisode(event_id, base, f"base:{base}", (decision,), ())


async def test_fetch_market_paths_uses_exact_anchor_and_closes_owned_client() -> None:
    exchange = AsyncMock()
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ) as fetch:
        paths = await fetch_market_paths((_episode(),), {"binance": lambda: exchange})

    assert paths[0].status == "complete"
    assert paths[0].exchange == "binance"
    assert paths[0].candles == (candle,)
    fetch.assert_awaited_once()
    exchange.close.assert_awaited_once()


async def test_fetch_market_paths_reuses_one_client_for_same_exchange() -> None:
    exchange = AsyncMock()
    factory = Mock(return_value=exchange)
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ) as fetch:
        paths = await fetch_market_paths(
            (
                _episode(event_id=42, base="ERA"),
                _episode(event_id=43, base="BANK"),
            ),
            {"binance": factory},
        )

    assert len(paths) == 2
    assert factory.call_count == 1
    assert fetch.await_count == 2
    exchange.close.assert_awaited_once()


async def test_fetch_market_paths_reports_unsupported_exchange_without_fallback() -> None:
    paths = await fetch_market_paths((_episode("unknown"),), {})

    assert paths[0].status == "unsupported_exchange"
    assert paths[0].candles == ()
    assert "exact anchor" in (paths[0].error or "")


async def test_fetch_market_paths_contains_exchange_failure_per_episode() -> None:
    exchange = AsyncMock()

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(side_effect=TimeoutError("slow venue")),
    ):
        paths = await fetch_market_paths((_episode(),), {"binance": lambda: exchange})

    assert paths[0].status == "fetch_failed"
    assert paths[0].error == "slow venue"
    exchange.close.assert_awaited_once()


async def test_fetch_entry_challenger_paths_uses_registered_broad_bounds() -> None:
    exchange = AsyncMock()
    episode = _episode()
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ) as fetch:
        paths = await fetch_entry_challenger_paths(
            (episode,),
            {"binance": lambda: exchange},
        )

    start_ms, end_ms = challenger_path_bounds(episode.decisions[0])
    fetch.assert_awaited_once_with(exchange, "ERA", start_ms, end_ms)
    assert paths[0].candles == (candle,)
    exchange.close.assert_awaited_once()
