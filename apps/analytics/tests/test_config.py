import pytest
from schurfer_analytics.config import Config, _bool


def test_source_lead_capture_boolean_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_LEAD_CAPTURE_ENABLED", "maybe")

    with pytest.raises(ValueError, match="must be a boolean value"):
        Config()


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("0", False), ("OFF", False)])
def test_boolean_parser_accepts_explicit_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("FLAG", value)

    assert _bool("FLAG", not expected) is expected


def test_source_lead_capture_bounds_fail_closed() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        Config(source_lead_notional_usd=0)

    with pytest.raises(ValueError, match="unique Binance/Bybit"):
        Config(source_lead_targets=("binance", "binance"))
