from schurfer_execution.risk import (
    check_daily_loss,
    check_duplicate_position,
    check_max_position_size,
    check_max_positions,
    check_positions_available,
    check_sufficient_margin,
    check_trading_enabled,
    run_all_checks,
)


def _pos(base: str, exchange: str = "bingx") -> dict:  # type: ignore[type-arg]
    return {"base": base, "exchange": exchange, "side": "short", "size_usd": 200.0}


def _bal(exchange: str = "bingx", free: float = 1000.0) -> dict:  # type: ignore[type-arg]
    return {"exchange": exchange, "free": free, "used": 0.0, "total": free}


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
