import pytest
from schurfer_execution.risk import (
    EntryCheck,
    check_daily_loss,
    check_duplicate_position,
    check_entry_candles,
    check_funding_rate,
    check_liquidation_distance,
    check_max_position_size,
    check_max_positions,
    check_positions_available,
    check_sufficient_margin,
    check_trading_enabled,
    compute_position_size_usd,
    run_all_checks,
)


def _pos(base: str, exchange: str = "bingx") -> dict:  # type: ignore[type-arg]
    return {"base": base, "exchange": exchange, "side": "short", "size_usd": 200.0}


def _bal(
    exchange: str = "bingx", free: float = 1000.0, tradeable: bool = True, asset: str = "USDT"
) -> dict:  # type: ignore[type-arg]
    return {
        "exchange": exchange,
        "wallet": "swap",
        "asset": asset,
        "tradeable": tradeable,
        "free": free,
        "used": 0.0,
        "total": free,
    }


class TestCheckTradingEnabled:
    def test_enabled_by_default(self) -> None:
        assert check_trading_enabled(None).allowed
        assert check_trading_enabled("1").allowed
        assert check_trading_enabled("true").allowed

    def test_disabled_on_zero(self) -> None:
        assert not check_trading_enabled("0").allowed

    def test_disabled_on_false(self) -> None:
        assert not check_trading_enabled("false").allowed


class TestCheckPositionsAvailable:
    def test_ok_when_no_failures(self) -> None:
        assert check_positions_available("bingx", set()).allowed

    def test_blocks_when_target_exchange_failed(self) -> None:
        result = check_positions_available("bingx", {"bingx"})
        assert not result.allowed
        assert "fetch failed" in result.reason

    def test_ok_when_different_exchange_failed(self) -> None:
        assert check_positions_available("bingx", {"mexc"}).allowed


class TestCheckMaxPositions:
    def test_under_limit(self) -> None:
        assert check_max_positions(2, 5).allowed

    def test_at_limit(self) -> None:
        assert not check_max_positions(5, 5).allowed

    def test_over_limit(self) -> None:
        assert not check_max_positions(6, 5).allowed


class TestCheckDuplicatePosition:
    def test_no_existing_position(self) -> None:
        assert check_duplicate_position("BEAT", [_pos("ACT"), _pos("SYN")]).allowed

    def test_already_have_position(self) -> None:
        assert not check_duplicate_position("BEAT", [_pos("BEAT")]).allowed

    def test_case_insensitive(self) -> None:
        assert not check_duplicate_position("beat", [_pos("BEAT")]).allowed


class TestCheckSufficientMargin:
    def test_sufficient(self) -> None:
        assert check_sufficient_margin(200.0, [_bal(free=1000.0)], "bingx").allowed

    def test_insufficient(self) -> None:
        assert not check_sufficient_margin(500.0, [_bal(free=100.0)], "bingx").allowed

    def test_exact_amount(self) -> None:
        assert check_sufficient_margin(200.0, [_bal(free=200.0)], "bingx").allowed

    def test_missing_exchange(self) -> None:
        assert not check_sufficient_margin(200.0, [_bal(exchange="mexc")], "bingx").allowed

    def test_spot_balance_not_used_for_margin(self) -> None:
        spot = _bal(exchange="bingx", free=1000.0, tradeable=False)
        assert not check_sufficient_margin(200.0, [spot], "bingx").allowed

    def test_non_usdt_asset_not_used_for_margin(self) -> None:
        btc = _bal(exchange="bingx", free=1000.0, tradeable=True, asset="BTC")
        assert not check_sufficient_margin(200.0, [btc], "bingx").allowed


class TestCheckMaxPositionSize:
    def test_within_limit(self) -> None:
        assert check_max_position_size(200.0, 500.0).allowed

    def test_at_limit(self) -> None:
        assert check_max_position_size(500.0, 500.0).allowed

    def test_exceeds_limit(self) -> None:
        result = check_max_position_size(501.0, 500.0)
        assert not result.allowed
        assert "exceeds limit" in result.reason


class TestCheckDailyLoss:
    def test_within_limit(self) -> None:
        assert check_daily_loss(-50.0, 200.0).allowed

    def test_at_limit(self) -> None:
        assert not check_daily_loss(-200.0, 200.0).allowed

    def test_over_limit(self) -> None:
        assert not check_daily_loss(-250.0, 200.0).allowed

    def test_positive_pnl(self) -> None:
        assert check_daily_loss(100.0, 200.0).allowed


class TestRunAllChecks:
    def _defaults(self, **overrides: object) -> dict:  # type: ignore[type-arg]
        base: dict = {  # type: ignore[type-arg]
            "base": "BEAT",
            "exchange": "bingx",
            "size_usd": 200.0,
            "trading_flag": "1",
            "open_positions": [],
            "balances": [_bal()],
            "daily_pnl": 0.0,
            "max_positions": 5,
            "max_position_usd": 500.0,
            "daily_loss_limit_usd": 200.0,
            "failed_exchanges": set(),
        }
        base.update(overrides)
        return base

    def test_all_ok(self) -> None:
        assert run_all_checks(**self._defaults()).allowed

    def test_emergency_stop_blocks_first(self) -> None:
        result = run_all_checks(**self._defaults(trading_flag="0"))
        assert not result.allowed
        assert "emergency stop" in result.reason

    def test_position_fetch_failure_blocks(self) -> None:
        result = run_all_checks(**self._defaults(failed_exchanges={"bingx"}))
        assert not result.allowed
        assert "fetch failed" in result.reason

    def test_daily_loss_blocks_before_position_check(self) -> None:
        result = run_all_checks(**self._defaults(daily_pnl=-200.0))
        assert not result.allowed
        assert "daily loss" in result.reason

    def test_max_positions_blocks(self) -> None:
        positions = [_pos(f"TOKEN{i}") for i in range(5)]
        result = run_all_checks(**self._defaults(open_positions=positions))
        assert not result.allowed
        assert "max positions" in result.reason

    def test_duplicate_blocks(self) -> None:
        result = run_all_checks(**self._defaults(open_positions=[_pos("BEAT")]))
        assert not result.allowed
        assert "BEAT" in result.reason

    def test_max_position_size_blocks(self) -> None:
        result = run_all_checks(**self._defaults(size_usd=600.0, max_position_usd=500.0))
        assert not result.allowed
        assert "exceeds limit" in result.reason

    def test_insufficient_margin_blocks(self) -> None:
        result = run_all_checks(**self._defaults(balances=[_bal(free=10.0)]))
        assert not result.allowed
        assert "insufficient" in result.reason


class TestCheckLiquidationDistance:
    # With maintenance_margin=0.5%, liq_distance = 100/L - 0.5

    def test_safe_sl_allowed(self) -> None:
        # 5x: liq_distance=19.5%, max_sl=19.5*0.8=15.6% → 10% is safe
        assert check_liquidation_distance(10.0, leverage=5, buffer_pct=20.0).allowed

    def test_unsafe_sl_blocked(self) -> None:
        # 10x: liq_distance=9.5%, max_sl=9.5*0.8=7.6% → 10% is too close
        result = check_liquidation_distance(10.0, leverage=10, buffer_pct=20.0)
        assert not result.allowed
        assert "too close to liquidation" in result.reason
        assert "10x" in result.reason

    def test_just_over_limit_is_blocked(self) -> None:
        # 5x: max_sl=15.6 → 15.7 is blocked
        assert not check_liquidation_distance(15.7, leverage=5, buffer_pct=20.0).allowed

    def test_zero_buffer_allows_sl_up_to_liq_distance(self) -> None:
        # 5x, buffer=0%: max_sl = 19.5%
        assert check_liquidation_distance(19.4, leverage=5, buffer_pct=0.0).allowed
        assert not check_liquidation_distance(19.6, leverage=5, buffer_pct=0.0).allowed

    def test_high_leverage_tightens_allowed_sl(self) -> None:
        # 20x: liq_distance=4.5%, max_sl=4.5*0.8=3.6%
        assert check_liquidation_distance(3.5, leverage=20, buffer_pct=20.0).allowed
        assert not check_liquidation_distance(3.7, leverage=20, buffer_pct=20.0).allowed

    def test_leverage_zero_blocked(self) -> None:
        result = check_liquidation_distance(10.0, leverage=0, buffer_pct=20.0)
        assert not result.allowed
        assert "leverage must be > 0" in result.reason

    def test_leverage_negative_blocked(self) -> None:
        assert not check_liquidation_distance(10.0, leverage=-1, buffer_pct=20.0).allowed


class TestCheckFundingRate:
    def test_positive_rate_allowed(self) -> None:
        # Shorts receive funding — always allow
        assert check_funding_rate(0.01, min_funding_rate_pct=-0.1).allowed

    def test_zero_rate_allowed(self) -> None:
        assert check_funding_rate(0.0, min_funding_rate_pct=-0.1).allowed

    def test_mildly_negative_rate_allowed(self) -> None:
        # -0.05%/8h is above the -0.1% threshold
        assert check_funding_rate(-0.05, min_funding_rate_pct=-0.1).allowed

    def test_rate_at_threshold_allowed(self) -> None:
        assert check_funding_rate(-0.1, min_funding_rate_pct=-0.1).allowed

    def test_rate_below_threshold_blocked(self) -> None:
        result = check_funding_rate(-0.15, min_funding_rate_pct=-0.1)
        assert not result.allowed
        assert "funding_rate" in result.reason
        assert "shorts paying too much" in result.reason

    def test_custom_threshold(self) -> None:
        # Strict threshold: block anything negative
        assert not check_funding_rate(-0.01, min_funding_rate_pct=0.0).allowed
        assert check_funding_rate(0.01, min_funding_rate_pct=0.0).allowed


class TestComputePositionSizeUsd:
    def test_basic_formula(self) -> None:
        # equity=$1000, risk=0.5%, sl=10% → size = 1000 * 0.5 / 10 = $50
        assert compute_position_size_usd(1000.0, 0.5, 10.0, 500.0) == 50.0

    def test_tighter_sl_gives_larger_position(self) -> None:
        # same risk, half the SL → double the position
        assert compute_position_size_usd(1000.0, 0.5, 5.0, 500.0) == 100.0

    def test_capped_at_max_usd(self) -> None:
        # result would be $200, but max is $100 — hard ceiling always respected
        result = compute_position_size_usd(1000.0, 2.0, 10.0, 100.0)
        assert result == 100.0

    def test_returns_none_below_min_notional(self) -> None:
        # tiny equity: $10 * 0.5% / 10% = $0.5 < MIN_POSITION_USD → skip signal
        result = compute_position_size_usd(10.0, 0.5, 10.0, 500.0)
        assert result is None

    def test_zero_equity_returns_none(self) -> None:
        assert compute_position_size_usd(0.0, 0.5, 10.0, 500.0) is None

    def test_zero_sl_returns_none(self) -> None:
        # degenerate sl=0 → division guard, returns None
        assert compute_position_size_usd(1000.0, 0.5, 0.0, 500.0) is None

    def test_scales_with_equity(self) -> None:
        s1 = compute_position_size_usd(500.0, 0.5, 10.0, 1000.0)
        s2 = compute_position_size_usd(1000.0, 0.5, 10.0, 1000.0)
        assert s1 is not None and s2 is not None
        assert s2 == pytest.approx(s1 * 2)

    def test_returns_none_when_max_usd_below_min_notional(self) -> None:
        # computed=$50, capped to max_usd=$3, $3 < MIN_POSITION_USD=$5 → None
        assert compute_position_size_usd(1000.0, 0.5, 10.0, 3.0) is None

    def test_returns_value_when_max_usd_above_min_notional(self) -> None:
        # computed=$50, capped to max_usd=$10, $10 >= MIN_POSITION_USD=$5 → $10
        assert compute_position_size_usd(1000.0, 0.5, 10.0, 10.0) == pytest.approx(10.0)


def _c(open_: float, close: float, high: float | None = None, low: float | None = None) -> list:
    return [0, open_, high or max(open_, close), low or min(open_, close), close, 1000.0]


def _red(price: float = 100.0) -> list:
    return _c(price, price * 0.98)  # -2%


def _green(price: float = 100.0) -> list:
    return _c(price, price * 1.05)  # +5%


class TestCheckEntryCandles:
    def test_both_disabled_always_ok(self) -> None:
        candles = [_green(), _green()]
        assert check_entry_candles(candles, require_red_candle=False, min_retrace_pct=0.0).allowed

    def test_too_few_candles_ok_when_filters_disabled(self) -> None:
        result = check_entry_candles([_green()], require_red_candle=False, min_retrace_pct=0.0)
        assert result.allowed

    def test_too_few_candles_blocked_when_filter_enabled(self) -> None:
        result = check_entry_candles([_green()], require_red_candle=True, min_retrace_pct=0.0)
        assert not result.allowed
        assert "insufficient" in result.reason

    def test_too_few_candles_blocked_when_retrace_filter_enabled(self) -> None:
        result = check_entry_candles([_green()], require_red_candle=False, min_retrace_pct=2.0)
        assert not result.allowed
        assert "insufficient" in result.reason

    # --- require_red_candle ---

    def test_red_candle_passes_when_last_closed_is_red(self) -> None:
        # [-2] is red (last closed), [-1] is green (forming) → ok
        candles = [_green(), _green(), _red(), _green()]
        result = check_entry_candles(candles, require_red_candle=True, min_retrace_pct=0.0)
        assert result.allowed
        assert result.closed_red is True

    def test_red_candle_blocked_when_last_closed_is_green_even_if_current_red(self) -> None:
        # [-2] green, [-1] red (still forming — unreliable) → blocked
        candles = [_green(), _green(), _green(), _red()]
        result = check_entry_candles(candles, require_red_candle=True, min_retrace_pct=0.0)
        assert not result.allowed
        assert "no_red_candle" in result.reason

    def test_red_candle_blocked_when_all_green(self) -> None:
        candles = [_green(), _green(), _green(), _green()]
        result = check_entry_candles(candles, require_red_candle=True, min_retrace_pct=0.0)
        assert not result.allowed
        assert "no_red_candle" in result.reason

    def test_entry_check_exposes_closed_red_and_retrace(self) -> None:
        # [-2]=red(pump→106), [-1]=green(106→107, forming) — last closed is red
        candles = [_c(100, 110, high=110), _red(108), _c(106, 107)]
        result = check_entry_candles(candles, require_red_candle=True, min_retrace_pct=1.0)
        assert isinstance(result, EntryCheck)
        assert result.closed_red is True
        assert result.retrace_pct is not None and result.retrace_pct > 0

    # --- min_retrace_pct ---

    def test_retrace_passes_when_price_dropped_enough(self) -> None:
        # pump high=110, current=106 → retrace = (110-106)/110*100 ≈ 3.6%
        candles = [_c(100, 110, high=110), _c(108, 106)]
        result = check_entry_candles(candles, require_red_candle=False, min_retrace_pct=3.0)
        assert result.allowed

    def test_retrace_blocked_when_price_still_near_high(self) -> None:
        # pump high=110, current=109.5 → retrace ≈ 0.45%
        candles = [_c(100, 110, high=110), _c(110, 109.5)]
        result = check_entry_candles(candles, require_red_candle=False, min_retrace_pct=3.0)
        assert not result.allowed
        assert "insufficient_retrace" in result.reason

    def test_retrace_uses_highest_high_across_all_candles(self) -> None:
        # high is in the middle candle
        candles = [_c(90, 95, high=95), _c(95, 120, high=120), _c(118, 114)]
        # retrace = (120-114)/120*100 = 5%
        result = check_entry_candles(candles, require_red_candle=False, min_retrace_pct=4.0)
        assert result.allowed

    # --- combined ---

    def test_both_filters_must_pass(self) -> None:
        # [-2]=red (last closed OK), [-1]=near high (retrace < 5% → blocked)
        candles = [_c(100, 110, high=110), _red(109), _c(109, 109.5)]
        result = check_entry_candles(candles, require_red_candle=True, min_retrace_pct=5.0)
        assert not result.allowed
        assert "insufficient_retrace" in result.reason
