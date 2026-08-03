import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.config import Config
from schurfer_execution.order_lock import OrderLockLostError
from schurfer_execution.trader import (
    _SEEN_TTL_ENTRY_WAIT,
    _SEEN_TTL_MEASUREMENT_RECHECK,
    _SEEN_TTL_SCORE_RECHECK,
    _SEEN_TTL_SIGNAL_RETRY,
    _SEEN_TTL_SKIP,
    _SEEN_TTL_TRADED,
    _SIGNAL_READINESS_KEY,
    _SIGNAL_READINESS_TTL,
    SignalResult,
    _decision_price,
    _effective_entry_floor,
    _fetch_entry_candles,
    _fetch_equity_usd,
    _fetch_funding_rate_pct,
    _fetch_score,
    _fetch_signal,
    _pick_exchange,
    _pump_event_id,
    _tick,
)


def _cfg(
    *,
    score_threshold: int = 6,
    signal_leverage: int = 3,
    min_funding_rate_pct: float = -0.1,
    require_funding_rate: bool = False,
    risk_per_trade_pct: float = 0.0,
    require_red_candle: bool = False,
    min_retrace_pct: float = 0.0,
    require_market_quality: bool = False,
    entry_min_pct: float = 30.0,
) -> Config:
    cfg = object.__new__(Config)
    cfg.score_threshold = score_threshold
    cfg.strategy_version = "pump_short_v1"
    cfg.measurement_strategy_version = "pump_short_measurement_v1"
    cfg.entry_min_pct = entry_min_pct
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = signal_leverage
    cfg.max_positions = 5
    cfg.max_position_usd = 500.0
    cfg.daily_loss_limit_usd = 200.0
    cfg.liquidation_buffer_pct = 20.0
    cfg.min_funding_rate_pct = min_funding_rate_pct
    cfg.require_funding_rate = require_funding_rate
    cfg.require_market_quality = require_market_quality
    cfg.max_spread_bps = 50.0
    cfg.max_liquidity_impact_bps = 50.0
    cfg.liquidity_depth_multiplier = 2.0
    cfg.risk_per_trade_pct = risk_per_trade_pct
    cfg.require_red_candle = require_red_candle
    cfg.min_retrace_pct = min_retrace_pct
    cfg.dry_run = False
    cfg.db_url = None
    cfg.telegram_bot_token = None
    cfg.telegram_chat_id = None
    return cfg


def _healthy_liquidity_snapshot() -> dict[str, Any]:
    return {
        "ts": int(time.time()),
        "best_bid": 99.9,
        "best_ask": 100.1,
        "mid": 100.0,
        "spread_bps": 20.0,
        "depth_targets_usd": [100.0, 500.0, 1000.0],
        "bid_impact_bps": {"100": 10.0, "500": 10.0, "1000": 10.0},
        "ask_impact_bps": {"100": 10.0, "500": 10.0, "1000": 10.0},
    }


def _pumps(
    *bases: str,
    exchange: str = "bybit",
    volume: float = 1_000_000.0,
    pump_event_id: int | None = 42,
) -> bytes:
    return json.dumps(
        {
            "pumps": [
                {
                    "base": base,
                    "pump_event_id": pump_event_id,
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
        if key in {"pumps:measurement", "pumps:latest"}:
            return pumps_raw
        if key.startswith("signals:") and signal_score is not None:
            payload = json.dumps(
                {
                    "score": signal_score,
                    "verdict": "short_setup",
                    "computed_at": time.time() - signal_age_seconds,
                    "episode": {
                        "id": 42,
                        "entry_qualified_at": int(time.time()) - 60,
                    },
                }
            )
            return payload.encode()
        return b"1" if seen else None

    rdb.get = _get
    rdb.set = AsyncMock()
    rdb.hset = AsyncMock()
    rdb.expire = AsyncMock()
    rdb.xadd = AsyncMock()

    # write_decision runs its XADD + SET seen as one Lua script (rdb.eval). Simulate the
    # script's effect on the same AsyncMocks so tests can still assert on rdb.set/xadd.
    # Close the returned coroutine to avoid "never awaited" noise.
    def _record(mock: AsyncMock, *a: object, **k: object) -> None:
        mock(*a, **k).close()

    async def _eval(_script: str, _numkeys: int, *args: object) -> int:
        stream, seen_key, payload, ttl = args[0], args[1], args[2], args[3]
        _record(rdb.xadd, stream, {"data": payload})
        _record(rdb.set, seen_key, "1", ex=int(ttl))  # type: ignore[arg-type]
        return 1

    rdb.eval = _eval
    return rdb


@pytest.mark.parametrize(
    ("published", "configured", "expected", "valid"),
    [
        (None, 30.0, 30.0, True),
        (20.0, 30.0, 30.0, True),
        (40.0, 30.0, 40.0, True),
        ("invalid", 30.0, 30.0, False),
        (float("nan"), 30.0, 30.0, False),
        (True, 30.0, 30.0, False),
    ],
)
def test_effective_entry_floor_is_independent_and_fail_closed(
    published: object,
    configured: float,
    expected: float,
    valid: bool,
) -> None:
    assert _effective_entry_floor(published, configured) == (expected, valid)


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


@pytest.mark.parametrize("unavailable_volume", [None, "unknown", 0, float("nan")])
def test_pick_exchange_survives_unavailable_volume(unavailable_volume: object) -> None:
    # An unavailable volume must not raise (which would abort the whole tick); the
    # entry sorts to the bottom and the valid one wins.
    exchanges = [
        {"exchange": "bybit", "volume_24h_usd": unavailable_volume},
        {"exchange": "bingx", "volume_24h_usd": 2_000_000},
    ]
    assert _pick_exchange(exchanges, {"bybit": ..., "bingx": ...}) == "bingx"


# --- _decision_price ---


def test_decision_price_uses_chosen_exchange() -> None:
    pump = {
        "exchanges": [
            {"exchange": "bybit", "change_pct": 40.0, "price": "1.5"},
            {"exchange": "mexc", "change_pct": 80.0, "price": "1.7"},
        ]
    }
    assert _decision_price(pump, "bybit") == 1.5


def test_decision_price_falls_back_to_top_mover_when_no_exchange() -> None:
    pump = {
        "exchanges": [
            {"exchange": "bybit", "change_pct": 40.0, "price": "1.5"},
            {"exchange": "mexc", "change_pct": 80.0, "price": "1.7"},
        ]
    }
    # No chosen exchange (no_configured_exchange): use the top-moving one's price.
    assert _decision_price(pump, None) == 1.7


def test_decision_price_none_when_no_exchanges() -> None:
    assert _decision_price({"exchanges": []}, "bybit") is None


def test_decision_price_none_on_unparseable_or_nonpositive() -> None:
    assert _decision_price({"exchanges": [{"exchange": "bybit", "price": "abc"}]}, "bybit") is None
    assert _decision_price({"exchanges": [{"exchange": "bybit", "price": "0"}]}, "bybit") is None


def test_decision_price_survives_corrupt_change_pct() -> None:
    # A non-numeric change_pct in the fallback path must not raise (which would abort
    # the whole trader tick); the entry just sorts to the bottom.
    pump = {
        "exchanges": [
            {"exchange": "bybit", "change_pct": "unknown", "price": "1.5"},
            {"exchange": "mexc", "change_pct": 80.0, "price": "1.7"},
        ]
    }
    assert _decision_price(pump, None) == 1.7


def test_decision_price_non_finite_change_pct_does_not_win() -> None:
    # float() accepts "inf"/"nan", so a non-finite change_pct must not win the max()
    # and steal the fallback pick from a genuinely top-moving exchange.
    pump = {
        "exchanges": [
            {"exchange": "bybit", "change_pct": "inf", "price": "9.9"},
            {"exchange": "mexc", "change_pct": 80.0, "price": "1.7"},
        ]
    }
    assert _decision_price(pump, None) == 1.7


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


async def test_fetch_signal_preserves_real_computed_zero() -> None:
    result = await _fetch_signal("BEAT", _rdb(signal_score=0), expected_pump_event_id=42)

    assert result.score == 0
    assert result.status == "ok"


async def test_fetch_signal_rejects_signal_from_previous_pump_episode() -> None:
    result = await _fetch_signal("BEAT", _rdb(signal_score=8), expected_pump_event_id=99)

    assert result.score is None
    assert result.status == "signal_episode_mismatch"
    assert result.payload is not None
    assert result.payload["episode"]["id"] == 42


async def test_fetch_signal_requires_recomputed_entry_qualified_anchor() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(
        return_value=json.dumps(
            {
                "score": 8,
                "computed_at": time.time(),
                "episode": {"id": 42, "entry_qualified_at": None},
            }
        ).encode()
    )

    result = await _fetch_signal(
        "BEAT",
        rdb,
        expected_pump_event_id=42,
        require_entry_qualified=True,
    )

    assert result.score is None
    assert result.status == "signal_entry_not_qualified"


# --- _fetch_signal: malformed-but-valid JSON must not raise ---


async def test_fetch_signal_marks_null_score_invalid() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b'{"score": null, "computed_at": 0}')
    result = await _fetch_signal("BEAT", rdb)
    assert result.score is None
    assert result.status == "signal_invalid_score"
    assert result.payload == {"score": None, "computed_at": 0}


async def test_fetch_signal_marks_non_object_payload_invalid() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"[1, 2, 3]")
    result = await _fetch_signal("BEAT", rdb)
    assert result.score is None
    assert result.status == "signal_invalid_payload"
    assert result.payload is None


async def test_fetch_signal_marks_non_integer_score_invalid() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b'{"score": "high"}')
    result = await _fetch_signal("BEAT", rdb)
    assert result.score is None
    assert result.status == "signal_invalid_score"


async def test_fetch_signal_fails_closed_on_invalid_computed_at() -> None:
    # A junk computed_at must not raise, and freshness cannot be verified, so it must
    # fail closed (score 0 = skip) rather than trade on a signal of unknown age.
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b'{"score": 9, "computed_at": "bad"}')
    result = await _fetch_signal("BEAT", rdb)
    assert result.score is None
    assert result.status == "signal_invalid_timestamp"
    assert result.payload == {"score": 9, "computed_at": "bad"}


async def test_fetch_signal_fails_closed_on_future_timestamp() -> None:
    rdb = MagicMock()
    future = time.time() + 3600
    rdb.get = AsyncMock(return_value=f'{{"score": 9, "computed_at": {future}}}'.encode())
    result = await _fetch_signal("BEAT", rdb)
    assert result.score is None
    assert result.status == "signal_future_timestamp"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(42, 42), (None, None), (0, None), (-1, None), (True, None), ("42", None), (1.5, None)],
)
def test_pump_event_id_accepts_only_positive_json_integers(
    value: object, expected: int | None
) -> None:
    assert _pump_event_id({"pump_event_id": value}) == expected


# --- Config validation ---


def _bare_validation_config() -> Config:
    cfg = object.__new__(Config)
    cfg.entry_min_pct = 30.0
    cfg.measurement_strategy_version = "pump_short_measurement_v1"
    return cfg


def test_config_validation_raises_when_auto_trade_and_bad_position_usd() -> None:
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 0.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = 20.0
    cfg.db_url = "postgresql://test"
    cfg.require_market_quality = True
    with pytest.raises(ValueError, match="SIGNAL_POSITION_USD"):
        cfg.__post_init__()


def test_config_validation_raises_when_auto_trade_and_bad_leverage() -> None:
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 0
    cfg.liquidation_buffer_pct = 20.0
    cfg.db_url = "postgresql://test"
    cfg.require_market_quality = True
    with pytest.raises(ValueError, match="SIGNAL_LEVERAGE"):
        cfg.__post_init__()


def test_config_validation_skips_when_auto_trade_false() -> None:
    cfg = _bare_validation_config()
    cfg.auto_trade = False
    cfg.dry_run = False
    cfg.signal_position_usd = 0.0  # would fail if auto_trade=True
    cfg.signal_leverage = 0  # would fail if auto_trade=True
    cfg.liquidation_buffer_pct = -99.0  # would fail if auto_trade=True
    cfg.__post_init__()  # must not raise


def test_config_validation_raises_when_auto_trade_and_dry_run_both_set() -> None:
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = True
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = 20.0
    with pytest.raises(ValueError, match="mutually exclusive"):
        cfg.__post_init__()


def test_config_validation_raises_when_auto_trade_and_no_db_url() -> None:
    """Regression: without a journal DB, the daily-loss circuit breaker
    degrades to unrealized-only and forgets every closed trade's PnL —
    AUTO_TRADE must refuse to start without DATABASE_URL configured."""
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.db_url = None
    with pytest.raises(ValueError, match="DATABASE_URL"):
        cfg.__post_init__()


def test_config_validation_raises_when_liquidation_buffer_negative() -> None:
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = -20.0
    cfg.db_url = "postgresql://test"
    cfg.require_market_quality = True
    with pytest.raises(ValueError, match="LIQUIDATION_BUFFER_PCT"):
        cfg.__post_init__()


def test_config_validation_raises_when_liquidation_buffer_gte_100() -> None:
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = 100.0
    cfg.db_url = "postgresql://test"
    cfg.require_market_quality = True
    with pytest.raises(ValueError, match="LIQUIDATION_BUFFER_PCT"):
        cfg.__post_init__()


def _valid_live_config() -> Config:
    cfg = _bare_validation_config()
    cfg.auto_trade = True
    cfg.dry_run = False
    cfg.db_url = "postgresql://test"
    cfg.require_market_quality = True
    cfg.signal_position_usd = 50.0
    cfg.signal_leverage = 3
    cfg.liquidation_buffer_pct = 20.0
    cfg.risk_per_trade_pct = 0.0
    cfg.min_retrace_pct = 0.0
    cfg.max_spread_bps = 50.0
    cfg.max_liquidity_impact_bps = 50.0
    cfg.liquidity_depth_multiplier = 2.0
    return cfg


def test_config_validation_requires_market_quality_for_live_trading() -> None:
    cfg = _valid_live_config()
    cfg.require_market_quality = False

    with pytest.raises(ValueError, match="REQUIRE_MARKET_QUALITY"):
        cfg.__post_init__()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("max_spread_bps", 0.0, "MAX_SPREAD_BPS"),
        ("max_liquidity_impact_bps", float("nan"), "MAX_LIQUIDITY_IMPACT_BPS"),
        ("liquidity_depth_multiplier", 0.99, "LIQUIDITY_DEPTH_MULTIPLIER"),
        ("liquidity_depth_multiplier", 10.01, "LIQUIDITY_DEPTH_MULTIPLIER"),
    ],
)
def test_config_validation_rejects_invalid_market_quality_policy(
    field: str,
    value: float,
    expected: str,
) -> None:
    cfg = _valid_live_config()
    setattr(cfg, field, value)

    with pytest.raises(ValueError, match=expected):
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
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
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
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SCORE_RECHECK


@pytest.mark.parametrize(
    "status",
    [
        "signal_missing",
        "signal_stale",
        "signal_episode_mismatch",
        "ok",
    ],
)
async def test_tick_defers_unready_signal_without_durable_decision(status: str) -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"))
    with (
        patch(
            "schurfer_execution.trader._fetch_signal",
            new_callable=AsyncMock,
            return_value=SignalResult(None, None, status),
        ),
        patch(
            "schurfer_execution.trader.decisions.write_decision",
            new_callable=AsyncMock,
        ) as mock_write,
        patch(
            "schurfer_execution.trader.liquidity.snapshot",
            new_callable=AsyncMock,
        ) as mock_snapshot,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg(score_threshold=6))

    mock_write.assert_not_awaited()
    mock_snapshot.assert_not_awaited()
    rdb.set.assert_awaited_once_with(
        "trader:seen:BEAT",
        "1",
        ex=_SEEN_TTL_SIGNAL_RETRY,
    )
    readiness = rdb.hset.await_args.kwargs["mapping"]
    assert readiness["pump_count"] == 1
    assert readiness["evaluated"] == 1
    assert readiness["ready"] == 0
    assert readiness["deferred"] == 1
    expected_reason = status if status != "ok" else "signal_invalid_ready_state"
    assert json.loads(readiness["reasons"]) == {expected_reason: 1}
    rdb.expire.assert_awaited_once_with(_SIGNAL_READINESS_KEY, _SIGNAL_READINESS_TTL)


async def test_tick_signal_readiness_distinguishes_ready_deferred_and_seen() -> None:
    rdb = _rdb(pumps_raw=_pumps("READY", "DEFERRED", "SEEN"))

    async def get(key: str) -> bytes | None:
        if key == "pumps:latest":
            return _pumps("READY", "DEFERRED", "SEEN")
        if key == "trader:seen:SEEN":
            return b"1"
        return None

    async def signal(
        base: str,
        _rdb: Any,
        *,
        expected_pump_event_id: int | None,
        require_entry_qualified: bool,
    ) -> SignalResult:
        assert expected_pump_event_id == 42
        assert require_entry_qualified is True
        if base == "READY":
            return SignalResult(3, {"score": 3}, "ok")
        return SignalResult(None, None, "signal_missing")

    rdb.get = get
    with (
        patch("schurfer_execution.trader._fetch_signal", side_effect=signal),
        patch(
            "schurfer_execution.trader.decisions.write_decision",
            new_callable=AsyncMock,
        ),
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg())

    readiness = rdb.hset.await_args.kwargs["mapping"]
    assert readiness["pump_count"] == 3
    assert readiness["evaluated"] == 2
    assert readiness["ready"] == 1
    assert readiness["deferred"] == 1
    assert json.loads(readiness["reasons"]) == {"signal_missing": 1}


async def test_tick_writes_decision_on_score_skip() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock),
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg(score_threshold=6))

    mock_write.assert_called_once()
    kw = mock_write.call_args.kwargs
    assert kw["base"] == "BEAT"
    assert kw["action"] == "skipped"
    assert "score" in kw["reason"]


async def test_tick_records_below_floor_without_reaching_order_path() -> None:
    payload = json.dumps(
        {
            "entry_min_change_pct": 30.0,
            "pumps": [
                {
                    "base": "MEASURE",
                    "pump_event_id": 42,
                    "max_change_pct": 25.0,
                    "exchanges": [
                        {
                            "exchange": "bybit",
                            "change_pct": 25.0,
                            "price": "1.5",
                            "volume_24h_usd": 1_000_000.0,
                        }
                    ],
                }
            ],
        }
    ).encode()
    rdb = _rdb(pumps_raw=payload, signal_score=10)
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order,
        patch(
            "schurfer_execution.trader.decisions.write_decision",
            new_callable=AsyncMock,
        ) as mock_write,
        patch(
            "schurfer_execution.trader.liquidity.snapshot",
            new_callable=AsyncMock,
            return_value=_healthy_liquidity_snapshot(),
        ),
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg(entry_min_pct=30.0))

    mock_order.assert_not_awaited()
    mock_write.assert_awaited_once()
    decision = mock_write.await_args.kwargs
    assert decision["reason"] == "pump_below_entry_floor"
    assert decision["score"] == 10
    assert decision["strategy_version"] == "pump_short_measurement_v1"
    assert decision["seen_ttl"] == _SEEN_TTL_MEASUREMENT_RECHECK
    assert decision["features"]["measurement_only"] is True
    assert decision["features"]["config"]["entry_min_pct"] == 30.0
    assert decision["liquidity"]["status"] == "sampled"


async def test_tick_writes_decision_on_successful_open() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    with (
        patch(
            "schurfer_execution.trader.place_order",
            new_callable=AsyncMock,
            return_value={"allowed": True, "order_id": "ord-1"},
        ),
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg())

    mock_write.assert_called_once()
    kw = mock_write.call_args.kwargs
    assert kw["action"] == "opened"
    assert kw["base"] == "BEAT"


async def test_tick_decision_captures_features_and_liquidity_status() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock),
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg(score_threshold=6))

    kw = mock_write.call_args.kwargs
    assert kw["decision_id"]  # a uuid string is present
    assert kw["strategy_version"] == "pump_short_v1"
    assert kw["pump_event_id"] == 42
    assert kw["features"]["signal_status"] == "ok"
    assert kw["features"]["signal"]["score"] == 3
    assert kw["features"]["config"]["score_threshold"] == 6
    assert kw["features"]["config"]["max_spread_bps"] == 50.0
    assert kw["features"]["config"]["max_liquidity_impact_bps"] == 50.0
    assert kw["features"]["config"]["liquidity_depth_multiplier"] == 2.0
    # liquidity is never null-ambiguous: it always carries a status
    assert kw["liquidity"]["status"] in {"sampled", "fetch_failed", "no_exchange"}


async def test_tick_decision_liquidity_status_sampled_on_good_book() -> None:
    # A working order book must yield status "sampled" with real numbers, not just
    # "some status". This catches a silently-always-broken liquidity path.
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
    ex = MagicMock()
    ex.fetch_order_book = AsyncMock(
        return_value={"bids": [[99.9, 1000.0]], "asks": [[100.1, 1000.0]]}
    )
    with (
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock),
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
    ):
        await _tick({"bybit": ex}, rdb, _cfg(score_threshold=6))

    liq = mock_write.call_args.kwargs["liquidity"]
    assert liq["status"] == "sampled"
    assert liq["spread_bps"] == 20.0
    assert liq["quality"]["allowed"] is True
    assert liq["quality"]["depth_target_usd"] == 100.0


async def test_tick_market_quality_gate_allows_tradeable_book() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    cfg = _cfg(require_market_quality=True)
    ex = MagicMock()

    with (
        patch(
            "schurfer_execution.trader.liquidity.snapshot",
            AsyncMock(return_value=_healthy_liquidity_snapshot()),
        ) as snapshot_mock,
        patch(
            "schurfer_execution.trader.place_order",
            new_callable=AsyncMock,
            return_value={"allowed": True, "order_id": "ord-123"},
        ) as mock_order,
    ):
        await _tick({"bybit": ex}, rdb, cfg)

    snapshot_mock.assert_awaited_once_with(ex, "BEAT", required_depth_usd=100.0)
    mock_order.assert_awaited_once()
    assert mock_order.call_args.kwargs["liquidity_checked_usd"] == 100.0


@pytest.mark.parametrize(
    ("snapshot_data", "expected_reason"),
    [
        (None, "market_quality_snapshot_unavailable"),
        (
            {**_healthy_liquidity_snapshot(), "spread_bps": 50.01},
            "market_quality_spread_too_wide",
        ),
        (
            {
                **_healthy_liquidity_snapshot(),
                "bid_impact_bps": {"100": None},
            },
            "market_quality_insufficient_bid_depth",
        ),
    ],
)
async def test_tick_market_quality_gate_skips_untradeable_book(
    snapshot_data: dict[str, Any] | None,
    expected_reason: str,
) -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    cfg = _cfg(require_market_quality=True)

    with (
        patch(
            "schurfer_execution.trader.liquidity.snapshot",
            AsyncMock(return_value=snapshot_data),
        ),
        patch(
            "schurfer_execution.trader._fetch_funding_rate_pct",
            new_callable=AsyncMock,
        ) as funding,
        patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as order,
        patch(
            "schurfer_execution.trader.decisions.write_decision",
            new_callable=AsyncMock,
        ) as write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, cfg)

    funding.assert_not_awaited()
    order.assert_not_awaited()
    write.assert_awaited_once()
    decision = write.call_args.kwargs
    assert decision["action"] == "skipped"
    assert decision["reason"] == expected_reason
    assert decision["seen_ttl"] == _SEEN_TTL_ENTRY_WAIT
    assert decision["liquidity"]["quality"]["allowed"] is False


async def test_tick_threads_decision_id_into_setup_context() -> None:
    # decision_id and strategy_version must reach the trade (via setup_context ->
    # app.trades) so decision -> trade -> outcome can be joined later.
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    cfg = _cfg(score_threshold=6)
    cfg.dry_run = True
    ex = MagicMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": "1.5"})
    with (
        patch("schurfer_execution.trader.paper.open_paper", new_callable=AsyncMock) as mock_paper,
        patch("schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock),
    ):
        await _tick({"bybit": ex}, rdb, cfg)

    mock_paper.assert_called_once()
    ctx = mock_paper.call_args.kwargs["setup_context"]
    assert ctx["decision_id"]
    assert ctx["strategy_version"] == "pump_short_v1"


async def test_tick_no_exchange_decision_still_has_features() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=3)
    with patch(
        "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
    ) as mock_write:
        await _tick({}, rdb, _cfg())  # no configured exchanges

    kw = mock_write.call_args.kwargs
    assert kw["reason"] == "no_configured_exchange"
    assert kw["features"]["signal"]["score"] == 3
    assert kw["liquidity"] == {"status": "no_exchange"}


async def test_tick_writes_decision_when_dry_run_price_unavailable() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=7)
    cfg = _cfg(score_threshold=6)
    cfg.dry_run = True
    with patch(
        "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
    ) as mock_write:
        # MagicMock exchange: funding/entry/liquidity fetches degrade to None and
        # fetch_ticker fails, so we land in the dry-run price-unavailable branch.
        await _tick({"bybit": MagicMock()}, rdb, cfg)

    reasons = [c.kwargs["reason"] for c in mock_write.call_args_list]
    assert "dry_run_price_unavailable" in reasons


async def test_tick_dry_run_decision_price_is_scanner_not_ticker() -> None:
    # The decision must record the scanner price at decision time (1.5) while the
    # paper trade records the live entry price from the ticker (1.7). One shared
    # variable would make skipped / live / dry-run decision prices non-comparable.
    pumps_raw = json.dumps(
        {
            "pumps": [
                {
                    "base": "BEAT",
                    "max_change_pct": 50.0,
                    "exchanges": [
                        {
                            "exchange": "bybit",
                            "change_pct": 50.0,
                            "price": "1.5",
                            "volume_24h_usd": 1_000_000,
                        }
                    ],
                }
            ]
        }
    ).encode()
    rdb = _rdb(pumps_raw=pumps_raw, signal_score=7)
    cfg = _cfg(score_threshold=6)
    cfg.dry_run = True
    ex = MagicMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": "1.7"})
    with (
        patch("schurfer_execution.trader.paper.open_paper", new_callable=AsyncMock) as mock_paper,
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
    ):
        await _tick({"bybit": ex}, rdb, cfg)

    dry_run = next(c for c in mock_write.call_args_list if c.kwargs["action"] == "opened_dry_run")
    assert dry_run.kwargs["price"] == 1.5  # scanner decision price, not the ticker
    assert mock_paper.call_args.kwargs["price"] == 1.7  # live entry price


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
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
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
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
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


# --- _fetch_equity_usd ---


async def test_fetch_equity_usd_returns_total_usdt() -> None:
    ex = MagicMock()
    ex.options = {"defaultType": "swap"}
    ex.fetch_balance = AsyncMock(
        return_value={
            "USDT": {"free": 700.0, "used": 300.0, "total": 1000.0},
            "free": {},
            "used": {},
            "total": {},
            "info": {},
        }
    )
    result = await _fetch_equity_usd({"bybit": ex}, "bybit")
    assert result == pytest.approx(1000.0)


async def test_fetch_equity_usd_returns_none_on_exception() -> None:
    ex = MagicMock()
    ex.options = {"defaultType": "swap"}
    ex.fetch_balance = AsyncMock(side_effect=RuntimeError("timeout"))
    result = await _fetch_equity_usd({"bybit": ex}, "bybit")
    assert result is None


async def test_fetch_equity_usd_returns_none_for_unknown_exchange() -> None:
    result = await _fetch_equity_usd({}, "bybit")
    assert result is None


# --- risk-based position sizing in _tick ---


async def test_tick_uses_fixed_size_when_risk_pct_disabled() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-1"},
    ) as mock_order:
        await _tick({"bybit": _exchange_mock(0.0001)}, rdb, _cfg(risk_per_trade_pct=0.0))

    assert mock_order.call_args.kwargs["size_usd"] == pytest.approx(50.0)


async def test_tick_computes_size_from_equity_when_risk_pct_enabled() -> None:
    # equity=$1000, risk=0.5%, initial_sl depends on pump_pct=50 → ~10%
    # expected size = 1000 * 0.5 / 10 = $50 (capped at signal_position_usd=50)
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_balance = AsyncMock(
        return_value={
            "USDT": {"free": 700.0, "used": 300.0, "total": 1000.0},
            "free": {},
            "used": {},
            "total": {},
            "info": {},
        }
    )
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-2"},
    ) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(risk_per_trade_pct=0.5))

    size = mock_order.call_args.kwargs["size_usd"]
    assert size > 0
    assert size <= 50.0  # capped at signal_position_usd


async def test_tick_skips_when_equity_fetch_fails_and_risk_pct_enabled() -> None:
    # When risk sizing is on but equity is unavailable, skip the trade (fail-closed).
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_balance = AsyncMock(side_effect=RuntimeError("timeout"))
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
    ) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(risk_per_trade_pct=0.5))

    mock_order.assert_not_called()
    rdb.set.assert_called_once()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_SKIP


# --- _fetch_entry_candles ---


def _candles_green() -> list:
    return [[0, 100.0, 110.0, 95.0, 108.0, 5000.0]] * 6  # all green


def _candles_with_red() -> list:
    green = [0, 100.0, 110.0, 95.0, 108.0, 5000.0]
    red = [0, 108.0, 109.0, 103.0, 104.0, 3000.0]
    return [green, green, green, green, red, green]  # [-2] is red


async def test_fetch_entry_candles_returns_ohlcv() -> None:
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(return_value=_candles_green())
    result = await _fetch_entry_candles(ex, "BEAT")
    assert result == _candles_green()


async def test_fetch_entry_candles_returns_none_on_error() -> None:
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(side_effect=RuntimeError("timeout"))
    result = await _fetch_entry_candles(ex, "BEAT")
    assert result is None


# --- entry quality filter in _tick ---


async def test_tick_skips_with_entry_wait_ttl_when_no_red_candle() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(return_value=_candles_green())
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(require_red_candle=True))
    mock_order.assert_not_called()
    rdb.set.assert_called_once()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_ENTRY_WAIT


async def test_tick_proceeds_when_red_candle_present() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(return_value=_candles_with_red())
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-1"},
    ) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(require_red_candle=True))
    mock_order.assert_called_once()


async def test_tick_proceeds_when_entry_filters_disabled() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(return_value=_candles_green())
    with patch(
        "schurfer_execution.trader.place_order",
        new_callable=AsyncMock,
        return_value={"allowed": True, "order_id": "ord-2"},
    ) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(require_red_candle=False, min_retrace_pct=0.0))
    mock_order.assert_called_once()
    # fetch_ohlcv should NOT have been called (filters disabled)
    ex.fetch_ohlcv.assert_not_called()


async def test_tick_skips_when_entry_candles_unavailable_fail_closed() -> None:
    # When filters are enabled and OHLCV fetch fails, skip (fail-closed)
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(side_effect=RuntimeError("timeout"))
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(require_red_candle=True))
    mock_order.assert_not_called()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_ENTRY_WAIT


async def test_tick_skips_when_entry_candles_malformed() -> None:
    rdb = _rdb(pumps_raw=_pumps("BEAT"), signal_score=8)
    ex = _exchange_mock(0.0001)
    ex.fetch_ohlcv = AsyncMock(return_value=[[1, 2]])  # too short, malformed
    with patch("schurfer_execution.trader.place_order", new_callable=AsyncMock) as mock_order:
        await _tick({"bybit": ex}, rdb, _cfg(require_red_candle=True))
    mock_order.assert_not_called()
    assert rdb.set.call_args.kwargs["ex"] == _SEEN_TTL_ENTRY_WAIT


async def test_tick_continues_other_pumps_after_order_lock_lost() -> None:
    # A lease lost mid-operation on one candidate (see order_lock.py) must not abort
    # evaluation of the remaining candidates queued in the same tick.
    rdb = _rdb(pumps_raw=_pumps("BEAT", "MOON"), signal_score=7)
    with (
        patch(
            "schurfer_execution.trader.place_order",
            new_callable=AsyncMock,
            side_effect=[
                OrderLockLostError("order lock lease lost during open: ownership changed"),
                {"allowed": True, "order_id": "ord-2"},
            ],
        ) as mock_order,
        patch(
            "schurfer_execution.trader.decisions.write_decision", new_callable=AsyncMock
        ) as mock_write,
    ):
        await _tick({"bybit": MagicMock()}, rdb, _cfg())

    assert mock_order.await_count == 2
    assert mock_write.call_count == 2
    first, second = (call.kwargs for call in mock_write.call_args_list)
    assert first["base"] == "BEAT"
    assert first["action"] == "skipped"
    assert first["reason"] == "order_lock_lost_outcome_uncertain"
    assert second["base"] == "MOON"
    assert second["action"] == "opened"
