from __future__ import annotations

import pytest
from schurfer_execution.config import Config


def test_entry_floor_and_measurement_strategy_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUMP_ENTRY_MIN_PCT", raising=False)
    monkeypatch.delenv("MEASUREMENT_STRATEGY_VERSION", raising=False)

    cfg = Config()

    assert cfg.entry_min_pct == 30
    assert cfg.measurement_strategy_version == "pump_short_measurement_v1"


def test_legacy_pump_min_pct_remains_an_entry_floor_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUMP_ENTRY_MIN_PCT", raising=False)
    monkeypatch.setenv("PUMP_MIN_PCT", "35")

    assert Config().entry_min_pct == 35


@pytest.mark.parametrize("value", ["0", "-1", "5001", "nan", "inf"])
def test_entry_floor_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("PUMP_ENTRY_MIN_PCT", value)

    with pytest.raises(ValueError, match="PUMP_ENTRY_MIN_PCT"):
        Config()


def test_measurement_strategy_version_must_not_be_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEASUREMENT_STRATEGY_VERSION", " ")

    with pytest.raises(ValueError, match="MEASUREMENT_STRATEGY_VERSION"):
        Config()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_health_alert_cooldown_must_be_strictly_positive(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """0 would be passed straight to Redis as SET...EX 0, which Redis
    rejects outright rather than treating as "no cooldown"."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EARLY_MOMENTUM_HEALTH_ALERT_COOLDOWN_SECONDS", value)

    with pytest.raises(ValueError, match="EARLY_MOMENTUM_HEALTH_ALERT_COOLDOWN_SECONDS"):
        Config()


def test_health_alert_cooldown_default_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("EARLY_MOMENTUM_HEALTH_ALERT_COOLDOWN_SECONDS", raising=False)

    assert Config().early_momentum_health_alert_cooldown_seconds == 1800


# --- per-strategy TradingMode overrides (execution_intent.py) ---
#
# Config.__post_init__ calls execution_intent.resolve_mode for all three
# strategies -- these lock in the regression the colleague review's blocker
# #1 was about: an unset override must never inherit AUTO_TRADE's ceiling
# into a live mode, and AUTO_TRADE=true with nothing else set must not raise
# (unset resolves safely to PAPER, not LIVE_MICRO).


def _clear_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PUMP_SHORT_MODE", "EARLY_MOMENTUM_MODE", "LIQUIDATION_CASCADE_MODE"):
        monkeypatch.delenv(key, raising=False)


def test_auto_trade_with_no_mode_overrides_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical regression case: AUTO_TRADE=true alone must not promote
    any strategy to a live mode just because nothing else was set -- an
    unset per-strategy override always resolves to PAPER, never the
    AUTO_TRADE-derived LIVE_MICRO ceiling."""
    _clear_mode_env(monkeypatch)
    monkeypatch.setenv("AUTO_TRADE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")

    Config()  # must not raise


def test_dry_run_with_no_mode_overrides_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mode_env(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")

    Config()  # must not raise


def test_unknown_mode_override_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mode_env(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EARLY_MOMENTUM_MODE", "not_a_real_mode")

    with pytest.raises(ValueError):
        Config()


def test_mode_override_above_the_dry_run_ceiling_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRY_RUN=true's ceiling is PAPER -- an explicit LIVE_MICRO override
    exceeds it and must fail at startup, not be silently capped."""
    _clear_mode_env(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EARLY_MOMENTUM_MODE", "live_micro")

    with pytest.raises(ValueError, match="exceeds"):
        Config()


def test_mode_override_with_neither_flag_set_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither AUTO_TRADE nor DRY_RUN -> ceiling is DISABLED -- even PAPER,
    otherwise the safest mode, is above that ceiling and must be rejected."""
    _clear_mode_env(monkeypatch)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("AUTO_TRADE", raising=False)
    monkeypatch.setenv("PUMP_SHORT_MODE", "paper")

    with pytest.raises(ValueError, match="exceeds"):
        Config()


@pytest.mark.parametrize("mode", ["shadow", "live_probe", "live_micro"])
def test_mode_override_selecting_an_unimplemented_broker_raises(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """SHADOW/LIVE_PROBE/LIVE_MICRO have no broker implementation in this
    build -- an explicit override must fail loud at startup even when it
    fits under the AUTO_TRADE ceiling, never silently fall back to PAPER."""
    _clear_mode_env(monkeypatch)
    monkeypatch.setenv("AUTO_TRADE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("LIQUIDATION_CASCADE_MODE", mode)

    with pytest.raises(ValueError, match="no implemented broker"):
        Config()


def test_explicit_paper_override_under_dry_run_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mode_env(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PUMP_SHORT_MODE", "paper")

    Config()  # must not raise
