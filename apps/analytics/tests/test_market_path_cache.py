from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.market_path_cache import (
    CACHE_CONTRACT_VERSION,
    CacheWriteOutcome,
    MarketPathCacheCorruptError,
    read_cached_candles,
    write_cached_candles,
)
from schurfer_analytics.ohlcv import Candle

if TYPE_CHECKING:
    from pathlib import Path

_CANDLES = [
    Candle(ts_ms=1_000, open=1.0, high=1.1, low=0.9, close=1.05, volume=10.0),
    Candle(ts_ms=61_000, open=1.05, high=1.2, low=1.0, close=1.1, volume=20.0),
]


def test_round_trip_returns_the_exact_written_candles(tmp_path: Path) -> None:
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    result = read_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        directory=tmp_path,
    )
    assert result == _CANDLES


def test_miss_on_unwritten_key_returns_none(tmp_path: Path) -> None:
    assert (
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )
        is None
    )


def test_different_key_dimensions_each_get_a_distinct_entry(tmp_path: Path) -> None:
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    # A different exchange, symbol, timeframe, or window is a different key
    # entirely -- none of these should collide with the entry above.
    assert (
        read_cached_candles(
            exchange_id="bybit",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )
        is None
    )
    assert (
        read_cached_candles(
            exchange_id="binance",
            symbol="OTHER/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )
        is None
    )
    assert (
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="1m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )
        is None
    )
    assert (
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=2_000,
            end_ms=121_000,
            directory=tmp_path,
        )
        is None
    )
    assert (
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=122_000,
            directory=tmp_path,
        )
        is None
    )


def test_corrupt_json_raises_instead_of_being_treated_as_a_miss(tmp_path: Path) -> None:
    """A cache entry that exists but is corrupt must be a hard failure, not
    a silent fall-through to a live re-fetch -- see this module's own
    docstring on why masking it would reintroduce the exact reproducibility
    hazard this cache exists to close."""
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    cache_files = list(tmp_path.rglob("*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text("{not valid json")

    with pytest.raises(MarketPathCacheCorruptError):
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )


def test_cache_key_mismatch_inside_the_file_raises(tmp_path: Path) -> None:
    """A hand-edited or hash-colliding file must never be trusted just
    because it happens to sit at the expected path."""
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    cache_files = list(tmp_path.rglob("*.json"))
    payload = json.loads(cache_files[0].read_text())
    payload["cache_key"] = "not-the-real-key"
    cache_files[0].write_text(json.dumps(payload))

    with pytest.raises(MarketPathCacheCorruptError):
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )


def test_content_hash_mismatch_raises(tmp_path: Path) -> None:
    """The stored content_sha256 is defense in depth on top of the cache_key
    check: a modified candle payload under an otherwise-intact key must
    still be caught."""
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    cache_files = list(tmp_path.rglob("*.json"))
    payload = json.loads(cache_files[0].read_text())
    payload["candles"][0]["close"] = 999.0
    cache_files[0].write_text(json.dumps(payload))

    with pytest.raises(MarketPathCacheCorruptError):
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )


def test_write_is_atomic_no_leftover_temp_files(tmp_path: Path) -> None:
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    all_files = list(tmp_path.rglob("*"))
    tmp_leftovers = [f for f in all_files if f.name.startswith(".tmp-")]
    assert tmp_leftovers == []


def test_write_returns_created_on_success(tmp_path: Path) -> None:
    outcome = write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    assert outcome is CacheWriteOutcome.CREATED


def test_first_writer_wins_not_last_writer(tmp_path: Path) -> None:
    """Two concurrent runs racing to cache the same window must not let the
    second one overwrite the first's already-persisted result."""
    first_candles = [Candle(ts_ms=1_000, open=1.0, high=1.1, low=0.9, close=1.05, volume=10.0)]
    second_candles = [Candle(ts_ms=1_000, open=2.0, high=2.1, low=1.9, close=2.05, volume=99.0)]

    first_outcome = write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=first_candles,
        directory=tmp_path,
    )
    second_outcome = write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=second_candles,
        directory=tmp_path,
    )

    assert first_outcome is CacheWriteOutcome.CREATED
    assert second_outcome is CacheWriteOutcome.ALREADY_EXISTS
    result = read_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        directory=tmp_path,
    )
    assert result == first_candles


def test_write_returns_write_failed_on_an_unwritable_directory(tmp_path: Path) -> None:
    unwritable = tmp_path / "no-such-parent-permission-denied"
    unwritable.mkdir(mode=0o400)
    try:
        # Must not raise even though the directory cannot be written into --
        # only report failure through the return value.
        outcome = write_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            candles=_CANDLES,
            directory=unwritable / "cache",
        )
        assert outcome is CacheWriteOutcome.WRITE_FAILED
    finally:
        unwritable.chmod(0o700)


def test_empty_candle_list_round_trips(tmp_path: Path) -> None:
    """A legitimate empty/partial result (retention limit, young
    instrument) must cache and replay correctly too, not just a full one."""
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=[],
        directory=tmp_path,
    )
    assert (
        read_cached_candles(
            exchange_id="binance",
            symbol="DAM/USDT:USDT",
            timeframe="5m",
            start_ms=1_000,
            end_ms=121_000,
            directory=tmp_path,
        )
        == []
    )


def test_contract_version_is_recorded_on_disk(tmp_path: Path) -> None:
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe="5m",
        start_ms=1_000,
        end_ms=121_000,
        candles=_CANDLES,
        directory=tmp_path,
    )
    cache_files = list(tmp_path.rglob("*.json"))
    payload = json.loads(cache_files[0].read_text())
    assert payload["contract_version"] == CACHE_CONTRACT_VERSION
