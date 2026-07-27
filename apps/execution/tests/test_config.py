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
