from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from schurfer_analytics.candle_anomaly_features import candle_anomaly_path_bounds
from schurfer_analytics.market_path_cache import (
    MarketPathCacheCorruptError,
    MarketPathCacheWriteError,
)
from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_entry_challengers import challenger_path_bounds
from schurfer_analytics.virtual_market import (
    DecisionMarketPath,
    MakerDecisionPaths,
    decision_market_path_fingerprint,
    fetch_candle_anomaly_paths,
    fetch_decision_market_paths,
    fetch_entry_challenger_paths,
    fetch_maker_decision_paths,
    fetch_market_paths,
    maker_market_path_fingerprint,
    maker_path_bounds,
)
from schurfer_analytics.virtual_strategy import MarketPath

if TYPE_CHECKING:
    from pathlib import Path


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


class _ExchangeClientTracker:
    def __init__(self) -> None:
        self.active_clients = 0
        self.maximum_active_clients = 0
        self.factories: dict[str, Mock] = {}
        self.clean_instance_data: list[bool] = []

    def factory_for(self, exchange_name: str) -> Mock:
        def build() -> AsyncMock:
            exchange = AsyncMock(name=exchange_name)
            self.active_clients += 1
            self.maximum_active_clients = max(
                self.maximum_active_clients,
                self.active_clients,
            )

            async def close(*, clean_instance_data: bool = False) -> None:
                self.active_clients -= 1
                self.clean_instance_data.append(clean_instance_data)

            exchange.close.side_effect = close
            return exchange

        factory = Mock(side_effect=build)
        self.factories[exchange_name] = factory
        return factory


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


async def test_fetch_market_paths_bounds_live_exchange_clients() -> None:
    tracker = _ExchangeClientTracker()
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ):
        paths = await fetch_market_paths(
            (
                _episode("bybit", event_id=42, base="ERA"),
                _episode("binance", event_id=43, base="BANK"),
            ),
            {
                "binance": tracker.factory_for("binance"),
                "bybit": tracker.factory_for("bybit"),
            },
        )

    assert [item.pump_event_id for item in paths] == [42, 43]
    assert tracker.maximum_active_clients == 1
    assert tracker.active_clients == 0
    assert tracker.clean_instance_data == [True, True]
    assert tracker.factories["binance"].call_count == 1
    assert tracker.factories["bybit"].call_count == 1


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
    fetch.assert_awaited_once_with(exchange, "ERA", start_ms, end_ms, use_cache=True)
    assert paths[0].candles == (candle,)
    exchange.close.assert_awaited_once()


async def test_fetch_candle_anomaly_paths_combines_context_and_exit_bounds() -> None:
    exchange = AsyncMock()
    episode = _episode()
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ) as fetch:
        paths = await fetch_candle_anomaly_paths(
            (episode,),
            {"binance": lambda: exchange},
        )

    start_ms, end_ms = candle_anomaly_path_bounds(episode.decisions[0])
    fetch.assert_awaited_once_with(exchange, "ERA", start_ms, end_ms, use_cache=True)
    assert paths[0].candles == (candle,)
    exchange.close.assert_awaited_once()


async def test_fetch_decision_paths_reuses_client_and_keys_paths_by_decision() -> None:
    exchange = AsyncMock()
    factory = Mock(return_value=exchange)
    first = _episode(event_id=42, base="ERA").decisions[0]
    second = _episode(event_id=43, base="BANK").decisions[0]
    second = replace(
        second,
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
    )
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ) as fetch:
        paths = await fetch_decision_market_paths(
            (first, second),
            {"binance": factory},
        )

    assert [item.decision_id for item in paths] == [first.decision_id, second.decision_id]
    assert all(item.path.status == "complete" for item in paths)
    assert factory.call_count == 1
    assert fetch.await_count == 2
    exchange.close.assert_awaited_once()


async def test_fetch_decision_paths_bounds_live_exchange_clients() -> None:
    tracker = _ExchangeClientTracker()
    progress = Mock()
    bybit = _episode("bybit", event_id=42, base="ERA").decisions[0]
    binance = replace(
        _episode("binance", event_id=43, base="BANK").decisions[0],
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
    )
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ):
        paths = await fetch_decision_market_paths(
            (bybit, binance),
            {
                "binance": tracker.factory_for("binance"),
                "bybit": tracker.factory_for("bybit"),
            },
            on_exchange=progress,
        )

    assert [item.decision_id for item in paths] == [bybit.decision_id, binance.decision_id]
    assert tracker.maximum_active_clients == 1
    assert tracker.active_clients == 0
    assert tracker.clean_instance_data == [True, True]
    assert tracker.factories["binance"].call_count == 1
    assert tracker.factories["bybit"].call_count == 1
    assert progress.call_args_list == [
        call("binance", 1, 2),
        call("bybit", 2, 2),
    ]


async def test_fetch_decision_paths_rejects_duplicate_decision_ids() -> None:
    decision = _episode().decisions[0]

    with pytest.raises(ValueError, match="unique decision ids"):
        await fetch_decision_market_paths((decision, decision), {})


async def test_fetch_maker_paths_reuses_client_and_requests_both_timeframes() -> None:
    exchange = AsyncMock()
    factory = Mock(return_value=exchange)
    decision = _episode().decisions[0]
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ) as fetch:
        paths = await fetch_maker_decision_paths(
            (decision,),
            {"binance": factory},
        )

    assert factory.call_count == 1
    assert fetch.await_count == 2
    assert paths[0].decision_id == decision.decision_id
    assert paths[0].one_minute.status == "complete"
    assert paths[0].five_minute.status == "complete"
    requested = {
        (
            call.kwargs["timeframe"],
            call.kwargs["timeframe_ms"],
            call.args[2],
            call.args[3],
        )
        for call in fetch.await_args_list
    }
    assert requested == {
        ("1m", 60_000, *maker_path_bounds(decision, 60_000)),
        ("5m", 300_000, *maker_path_bounds(decision, 300_000)),
    }
    exchange.close.assert_awaited_once()


async def test_fetch_maker_paths_bounds_live_exchange_clients() -> None:
    tracker = _ExchangeClientTracker()
    bybit = _episode("bybit", event_id=42, base="ERA").decisions[0]
    binance = replace(
        _episode("binance", event_id=43, base="BANK").decisions[0],
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
    )
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)

    with patch(
        "schurfer_analytics.virtual_market.fetch_candles",
        AsyncMock(return_value=[candle]),
    ):
        paths = await fetch_maker_decision_paths(
            (bybit, binance),
            {
                "binance": tracker.factory_for("binance"),
                "bybit": tracker.factory_for("bybit"),
            },
        )

    assert [item.decision_id for item in paths] == [bybit.decision_id, binance.decision_id]
    assert tracker.maximum_active_clients == 1
    assert tracker.active_clients == 0
    assert tracker.clean_instance_data == [True, True]
    assert tracker.factories["binance"].call_count == 1
    assert tracker.factories["bybit"].call_count == 1


def test_decision_path_fingerprint_is_order_independent_and_includes_decision_id() -> None:
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)
    path = MarketPath(42, "binance", "ERA", "complete", (candle,))
    first = DecisionMarketPath("decision-a", path)
    second = DecisionMarketPath("decision-b", path)

    assert decision_market_path_fingerprint((first, second)) == decision_market_path_fingerprint(
        (second, first)
    )
    assert decision_market_path_fingerprint((first,)) != decision_market_path_fingerprint((second,))


def test_decision_path_fingerprint_preserves_canonical_json_contract() -> None:
    candle = Candle(1785067500000, 100, 101, 99, 100, 1)
    item = DecisionMarketPath(
        "decision-a",
        MarketPath(42, "binance", "ERA", "complete", (candle,)),
    )
    payload = [
        {
            "decision_id": item.decision_id,
            "path": {
                "pump_event_id": item.path.pump_event_id,
                "exchange": item.path.exchange,
                "base": item.path.base,
                "status": item.path.status,
                "error": item.path.error,
                "candles": [asdict(value) for value in item.path.candles],
            },
        }
    ]
    expected = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()

    assert decision_market_path_fingerprint((item,)) == expected


def test_maker_path_fingerprint_is_order_independent_and_includes_both_timeframes() -> None:
    one = MarketPath(
        42,
        "binance",
        "ERA",
        "complete",
        (Candle(1785067500000, 100, 101, 99, 100, 1),),
    )
    five = MarketPath(
        42,
        "binance",
        "ERA",
        "complete",
        (Candle(1785067500000, 100, 102, 98, 101, 1),),
    )
    first = MakerDecisionPaths("decision-a", one, five)
    second = MakerDecisionPaths("decision-b", one, five)

    assert maker_market_path_fingerprint((first, second)) == maker_market_path_fingerprint(
        (second, first)
    )
    assert maker_market_path_fingerprint((first,)) != maker_market_path_fingerprint(
        (replace(first, five_minute=one),)
    )


def test_decision_path_requires_nonempty_key() -> None:
    path = MarketPath(42, "binance", "ERA", "complete", ())

    with pytest.raises(ValueError, match="requires a decision id"):
        DecisionMarketPath("", path)


def _synthetic_fetch_ohlcv(
    _symbol: str, timeframe: str, since: int, limit: int
) -> list[list[float]]:
    """A fake exchange that always returns a full, gapless run of `limit`
    bars starting at `since` rounded down to the nearest grid boundary --
    good enough to satisfy fetch_symbol_candles's cursor loop in one page
    regardless of the exact window a caller's own bounds function asks for,
    without this test needing to hand-compute those bounds itself."""
    timeframe_ms = 60_000 if timeframe == "1m" else 300_000
    aligned_since = (since // timeframe_ms) * timeframe_ms
    return [[aligned_since + i * timeframe_ms, 100, 101, 99, 100, 1] for i in range(limit)]


async def test_fetch_market_paths_propagates_cache_corruption_and_stops_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The colleague-review regression case: a corrupt cache entry must
    stop the whole report through the real fetch_market_paths call path,
    not just when calling fetch_symbol_candles directly."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    exchange = AsyncMock(id="binance")
    exchange.fetch_ohlcv = AsyncMock(side_effect=_synthetic_fetch_ohlcv)

    # First run populates the cache cleanly.
    await fetch_market_paths((_episode(),), {"binance": lambda: exchange})
    cache_files = list(tmp_path.rglob("*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text("{not valid json")

    with pytest.raises(MarketPathCacheCorruptError):
        await fetch_market_paths((_episode(),), {"binance": lambda: exchange})


async def test_fetch_market_paths_raises_on_a_cache_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unwritable = tmp_path / "read-only-cache-root"
    unwritable.mkdir(mode=0o500)
    try:
        monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(unwritable / "cache"))
        exchange = AsyncMock(id="binance")
        exchange.fetch_ohlcv = AsyncMock(side_effect=_synthetic_fetch_ohlcv)

        with pytest.raises(MarketPathCacheWriteError):
            await fetch_market_paths((_episode(),), {"binance": lambda: exchange})
    finally:
        unwritable.chmod(0o700)


async def test_fetch_maker_paths_propagates_cache_corruption_and_stops_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same pattern, for the maker-entry path (fetch_maker_decision_paths),
    which wires the cache through a structurally different function than
    fetch_market_paths/fetch_decision_market_paths."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    exchange = AsyncMock(id="binance")
    exchange.fetch_ohlcv = AsyncMock(side_effect=_synthetic_fetch_ohlcv)

    decision = _episode().decisions[0]
    await fetch_maker_decision_paths((decision,), {"binance": lambda: exchange})
    cache_files = list(tmp_path.rglob("*.json"))
    assert len(cache_files) == 2  # one_minute + five_minute
    cache_files[0].write_text("{not valid json")

    with pytest.raises(MarketPathCacheCorruptError):
        await fetch_maker_decision_paths((decision,), {"binance": lambda: exchange})
