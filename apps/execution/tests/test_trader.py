import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.config import Config
from schurfer_execution.trader import (
    _SEEN_TTL_SKIP,
    _SEEN_TTL_TRADED,
    _fetch_funding_rate_pct,
    _fetch_score,
    _pick_exchange,
    _tick,
)


def _cfg(
    *,
    score_threshold: int = 6,
    signal_leverage: int = 3,
    min_funding_rate_pct: float = -0.1,
    require_funding_rate: bool = False,
) -> Config:
    cfg = object.__new__(Config)
    cfg.score_threshold = score_threshold
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = signal_leverage
    cfg.max_positions = 5
    cfg.max_position_usd = 500.0
    cfg.daily_loss_limit_usd = 200.0
    cfg.liquidation_buffer_pct = 20.0
    cfg.min_funding_rate_pct = min_funding_rate_pct
    cfg.require_funding_rate = require_funding_rate
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
    cfg.liquidation_buffer_pct = 20.0
    with pytest.raises(ValueError, match="SIGNAL_POSITION_USD"):
        cfg.__post_init__()


def test_config_validation_raises_when_auto_trade_and_bad_leverage() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 0
    cfg.liquidation_buffer_pct = 20.0
    with pytest.raises(ValueError, match="SIGNAL_LEVERAGE"):
        cfg.__post_init__()


def test_config_validation_skips_when_auto_trade_false() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = False
    cfg.dry_run = False
    cfg.signal_position_usd = 0.0  # would fail if auto_trade=True
    cfg.signal_leverage = 0  # would fail if auto_trade=True
    cfg.liquidation_buffer_pct = -99.0  # would fail if auto_trade=True
    cfg.__post_init__()  # must not raise


def test_config_validation_raises_when_auto_trade_and_dry_run_both_set() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = True
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = 20.0
    with pytest.raises(ValueError, match="mutually exclusive"):
        cfg.__post_init__()


def test_config_validation_raises_when_liquidation_buffer_negative() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = -20.0
    with pytest.raises(ValueError, match="LIQUIDATION_BUFFER_PCT"):
        cfg.__post_init__()


def test_config_validation_raises_when_liquidation_buffer_gte_100() -> None:
    cfg = object.__new__(Config)
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = 100.0
    with pytest.raises(ValueError, match="LIQUIDATION_BUFFER_PCT"):
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


async def test_tick_writes_decision_on_score_skip() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock),
        patch("schurfer_execution.trader.decisions.write_decision") as mock_write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg(score_threshold=6))

    mock_write.assert_called_once()
    kw = mock_write.call_args.kwargs
    assert kw["base"] == "BEAT"
    assert kw["action"] == "skipped"
    assert "score" in kw["reason"]


async def test_tick_writes_decision_on_successful_open() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    with (
        patch(
            "schurfer_execution.trader.place_order",
            new_callable=AsyncMock,
            return_value={"allowed": True, "order_id": "ord-1"},
        ),
        patch("schurfer_execution.trader.decisions.write_decision") as mock_write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg())

    mock_write.assert_called_once()
    kw = mock_write.call_args.kwargs
    assert kw["action"] == "opened"
    assert kw["base"] == "BEAT"


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


async def test_tick_skips_when_sl_too_close_to_liquidation() -> None:
    # pump_pct=50 → initial_sl=10%, leverage=20x → max_safe=4% → blocked
    payload = json.dumps(
        {
            "pumps": [
                {
                    "base": "BEAT",
                    "max_change_pct": 50.0,
                    "exchanges": [{"exchange": "bybit", "volume_24h_usd": 1_000_000}],
                }
            ]
        }
    ).encode()
    rdb = _rdb(pumps_raw=payload, signal_score=8)

    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": MagicMock()}, rdb, _cfg(signal_leverage=20))
        mock_order.assert_not_called()

    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


# --- _fetch_funding_rate_pct ---

_8H_MS = 8 * 3600 * 1000


async def test_fetch_funding_rate_pct_returns_percentage_no_interval() -> None:
    # No fundingInterval → assume 8h, just convert fraction to %
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(return_value={"fundingRate": 0.0001})
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result == pytest.approx(0.01)


async def test_fetch_funding_rate_pct_normalizes_4h_interval() -> None:
    # 4h interval: rate per 4h * 2 = 8h-equivalent
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(
        return_value={"fundingRate": 0.0001, "fundingInterval": _8H_MS // 2}
    )
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result == pytest.approx(0.02)  # 0.01% * 2


async def test_fetch_funding_rate_pct_normalizes_1h_interval() -> None:
    # 1h interval: rate per 1h * 8 = 8h-equivalent
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(
        return_value={"fundingRate": 0.0001, "fundingInterval": _8H_MS // 8}
    )
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result == pytest.approx(0.08)


async def test_fetch_funding_rate_pct_standard_8h_interval_unchanged() -> None:
    # 8h interval: normalization factor = 1
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(
        return_value={"fundingRate": 0.0001, "fundingInterval": _8H_MS}
    )
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result == pytest.approx(0.01)


async def test_fetch_funding_rate_pct_negative_rate() -> None:
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(return_value={"fundingRate": -0.0005})
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result == pytest.approx(-0.05)


async def test_fetch_funding_rate_pct_returns_none_when_key_missing() -> None:
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(return_value={})
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result is None


async def test_fetch_funding_rate_pct_returns_none_on_exception() -> None:
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(side_effect=RuntimeError("not supported"))
    result = await _fetch_funding_rate_pct(ex, "BEAT")
    assert result is None


# --- funding rate filter in _tick ---


def _exchange_mock(funding_rate: float | None = 0.0001) -> MagicMock:
    """Exchange mock that returns a funding rate or raises on None."""
    ex = MagicMock()
    if funding_rate is None:
        ex.fetch_funding_rate = AsyncMock(side_effect=RuntimeError("not supported"))
    else:
        ex.fetch_funding_rate = AsyncMock(return_value={"fundingRate": funding_rate})
    return ex


async def test_tick_skips_when_funding_rate_below_threshold() -> None:
    # funding rate = -0.002 → -0.2%/8h, threshold = -0.1% → blocked
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": _exchange_mock(-0.002)}, rdb, _cfg(min_funding_rate_pct=-0.1))
        mock_order.assert_not_called()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


async def test_tick_writes_decision_on_funding_rate_skip() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock),
        patch("schurfer_execution.trader.decisions.write_decision") as mock_write,
    ):
        await _tick({"bybit": _exchange_mock(-0.002)}, rdb, _cfg(min_funding_rate_pct=-0.1))

    mock_write.assert_called_once()
    kw = mock_write.call_args.kwargs
    assert kw["action"] == "skipped"
    assert "funding_rate" in kw["reason"]
    assert "shorts paying too much" in kw["reason"]


async def test_tick_proceeds_when_funding_rate_above_threshold() -> None:
    # funding rate = -0.0005 → -0.05%/8h, threshold = -0.1% → ok
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-1"},
    ) as mock_order:
        await _tick({"bybit": _exchange_mock(-0.0005)}, rdb, _cfg(min_funding_rate_pct=-0.1))

    mock_order.assert_called_once()


async def test_tick_proceeds_when_funding_rate_fetch_fails() -> None:
    # Fail-open: if we can't fetch funding rate, don't block the trade
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-2"},
    ) as mock_order:
        await _tick({"bybit": _exchange_mock(None)}, rdb, _cfg(min_funding_rate_pct=-0.1))

    mock_order.assert_called_once()


async def test_tick_includes_funding_rate_in_setup_context() -> None:
    # funding_rate_pct stored in setup_context → journal.open_trade receives it
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with (
        patch(
            "schurfer_execution.trader.place_order",
            new_callable=AsyncMock,
            return_value={"allowed": True, "order_id": "ord-3", "price": 1.0},
        ),
        patch(
            "schurfer_execution.trader.journal.open_trade", new_callable=AsyncMock
        ) as mock_journal,
    ):
        cfg = _cfg()
        cfg.db_url = "postgres://fake"
        await _tick({"bybit": _exchange_mock(0.0001)}, rdb, cfg)

    ctx = mock_journal.call_args.kwargs["setup_context"]
    assert ctx["funding_rate_pct"] == pytest.approx(0.01)


async def test_tick_skips_when_require_funding_rate_and_fetch_fails() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick(
            {"bybit": _exchange_mock(None)},
            rdb,
            _cfg(require_funding_rate=True),
        )
        mock_order.assert_not_called()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


async def test_tick_writes_decision_on_funding_rate_unavailable_skip() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock),
        patch("schurfer_execution.trader.decisions.write_decision") as mock_write,
    ):
        await _tick(
            {"bybit": _exchange_mock(None)},
            rdb,
            _cfg(require_funding_rate=True),
        )

    mock_write.assert_called_once()
    kw = mock_write.call_args.kwargs
    assert kw["action"] == "skipped"
    assert kw["reason"] == "funding_rate_unavailable"


async def test_tick_proceeds_when_require_funding_rate_false_and_fetch_fails() -> None:
    # Default fail-open: require_funding_rate=False → trade proceeds even if fetch fails
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-x"},
    ) as mock_order:
        await _tick(
            {"bybit": _exchange_mock(None)},
            rdb,
            _cfg(require_funding_rate=False),
        )
    mock_order.assert_called_once()
