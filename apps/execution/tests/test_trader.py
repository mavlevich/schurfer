import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.config import Config
from schurfer_execution.trader import (
    _SEEN_TTL_SKIP,
    _SEEN_TTL_TRADED,
    _fetch_score,
    _pick_exchange,
    _tick,
)


def _cfg(*, score_threshold: int = 6) -> Config:
    cfg = object.__new__(Config)
    cfg.score_threshold = score_threshold
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.max_positions = 5
    cfg.max_position_usd = 500.0
    cfg.daily_loss_limit_usd = 200.0
    cfg.dry_run = False
    cfg.db_url = None
    cfg.telegram_bot_token = None
    cfg.telegram_chat_id = None
    return cfg


def _pumps(
    *bases: str,
    exchange: str = "bybit",
    volume: float = 1_000_000.0,
) -> bytes:
    return json.dumps(
        {
            "pumps": [
                {
                    "base": base,
                    "max_change_pct": 50.0,
                    "exchanges": [{"exchange": exchange, "volume_24h_usd": volume}],
                }
                for base in bases
            ]
        }
    ).encode()


def _rdb(
    *,
    pumps_raw: bytes | None = None,
    seen: bool = False,
    signal_score: int | None = None,
    signal_age_seconds: float = 0.0,
) -> MagicMock:
    rdb = MagicMock()

    async def _get(key: str) -> bytes | None:
        if key == "pumps:latest":
            return pumps_raw
        if key.startswith("signals:") and signal_score is not None:
            payload = json.dumps(
                {
                    "score": signal_score,
                    "verdict": "short_setup",
                    "computed_at": time.time() - signal_age_seconds,
                }
            )
            return payload.encode()
        return b"1" if seen else None

    rdb.get = _get
    rdb.set = AsyncMock()
    return rdb


# --- _pick_exchange ---


def test_pick_exchange_returns_configured_exchange() -> None:
    exchanges = [
        {"exchange": "bybit", "volume_24h_usd": 1_000_000},
        {"exchange": "mexc", "volume_24h_usd": 500_000},
    ]
    assert _pick_exchange(exchanges, {"bybit": ..., "bingx": ...}) == "bybit"


def test_pick_exchange_prefers_highest_volume() -> None:
    exchanges = [
        {"exchange": "bybit", "volume_24h_usd": 500_000},
        {"exchange": "bingx", "volume_24h_usd": 2_000_000},
    ]
    assert _pick_exchange(exchanges, {"bybit": ..., "bingx": ...}) == "bingx"


def test_pick_exchange_returns_none_when_no_match() -> None:
    result = _pick_exchange([{"exchange": "mexc", "volume_24h_usd": 1_000_000}], {"bybit": ...})
    assert result is None


def test_pick_exchange_returns_none_on_empty_list() -> None:
    assert _pick_exchange([], {"bybit": ...}) is None


# --- _fetch_score ---


async def test_fetch_score_returns_score_from_redis() -> None:
    rdb = _rdb(signal_score=7)
    assert await _fetch_score("BEAT", rdb) == 7


async def test_fetch_score_returns_zero_when_key_missing() -> None:
    rdb = _rdb()
    assert await _fetch_score("BEAT", rdb) == 0


async def test_fetch_score_returns_zero_on_invalid_json() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"not-json")
    assert await _fetch_score("BEAT", rdb) == 0


async def test_fetch_score_returns_zero_when_stale() -> None:
    rdb = _rdb(signal_score=8, signal_age_seconds=120.0)
    assert await _fetch_score("BEAT", rdb) == 0


async def test_fetch_score_returns_score_when_fresh() -> None:
    rdb = _rdb(signal_score=8, signal_age_seconds=30.0)
    assert await _fetch_score("BEAT", rdb) == 8


# --- Config validation ---


def test_config_validation_raises_when_auto_trade_and_bad_position_usd() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 0.0
    cfg.signal_leverage = 3
    with pytest.raises(ValueError, match="SIGNAL_POSITION_USD"):
        cfg.__post_init__()


def test_config_validation_raises_when_auto_trade_and_bad_leverage() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 0
    with pytest.raises(ValueError, match="SIGNAL_LEVERAGE"):
        cfg.__post_init__()


def test_config_validation_skips_when_auto_trade_false() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = False
    cfg.dry_run = False
    cfg.signal_position_usd = 0.0  # would fail if auto_trade=True
    cfg.signal_leverage = 0  # would fail if auto_trade=True
    cfg.__post_init__()  # must not raise


def test_config_validation_raises_when_auto_trade_and_dry_run_both_set() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = True
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    with pytest.raises(ValueError, match="mutually exclusive"):
        cfg.__post_init__()


# --- _tick ---


async def test_tick_does_nothing_when_no_pumps_key() -> None:
    rdb = _rdb(pumps_raw=None)
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({}, rdb, _cfg())
        mock_order.assert_not_called()


async def test_tick_skips_already_seen_token() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), seen=True)
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": MagicMock()}, rdb, _cfg())
        mock_order.assert_not_called()


async def test_tick_skips_when_no_configured_exchange() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"))
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({}, rdb, _cfg())
        mock_order.assert_not_called()
    rdb.set.assert_called_once()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


async def test_tick_skips_when_score_below_threshold() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": MagicMock()}, rdb, _cfg(score_threshold=6))
        mock_order.assert_not_called()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


async def test_tick_places_short_when_score_sufficient() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-123"},
    ) as mock_order:
        await _tick({"bybit": MagicMock()}, rdb, _cfg(score_threshold=6))

    mock_order.assert_called_once()
    kw = mock_order.call_args.kwargs
    assert kw["base"] == "BEAT"
    assert kw["exchange"] == "bybit"
    assert kw["side"] == "short"
    assert kw["size_usd"] == 50.0
    assert kw["leverage"] == 3


async def test_tick_sets_long_ttl_after_successful_trade() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-123"},
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg())

    seen_calls = [c for c in rdb.set.call_args_list if c.args[0] == "trader:seen:BEAT"]
    assert len(seen_calls) == 1
    assert seen_calls[0].kwargs["ex"] == _SEEN_TTL_TRADED


async def test_tick_sets_short_ttl_when_order_blocked() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": False, "reason": "max positions reached"},
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg())

    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


async def test_tick_picks_highest_volume_exchange() -> None:
    payload = json.dumps(
        {
            "pumps": [
                {
                    "base": "BEAT",
                    "max_change_pct": 50.0,
                    "exchanges": [
                        {"exchange": "bybit", "volume_24h_usd": 500_000},
                        {"exchange": "bingx", "volume_24h_usd": 2_000_000},
                    ],
                }
            ]
        }
    ).encode()
    rdb = _rdb(pumps_raw=payload, signal_score=8)

    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-x"},
    ) as mock_order:
        await _tick({"bybit": MagicMock(), "bingx": MagicMock()}, rdb, _cfg())

    assert mock_order.call_args.kwargs["exchange"] == "bingx"
