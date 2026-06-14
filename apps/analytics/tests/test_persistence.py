from schurfer_analytics.persistence import _high_24h_pct, _true_peak_pct


def _ex(price: str, change_pct: float, high_24h: str) -> dict[str, object]:
    return {
        "exchange": "bybit",
        "symbol": "BTCUSDT",
        "price": price,
        "change_pct": change_pct,
        "high_24h": high_24h,
        "volume_24h_usd": 1_000_000.0,
    }


def test_high_24h_pct_normal() -> None:
    # price=100, change_pct=+25% => open=80; high=120 => peak=(120/80-1)*100=50%
    result = _high_24h_pct(_ex("100.0", 25.0, "120.0"))
    assert result == 50.0


def test_high_24h_pct_empty_price() -> None:
    assert _high_24h_pct(_ex("", 25.0, "120.0")) == 0.0


def test_high_24h_pct_empty_high() -> None:
    assert _high_24h_pct(_ex("100.0", 25.0, "")) == 0.0


def test_high_24h_pct_zero_price() -> None:
    assert _high_24h_pct(_ex("0", 25.0, "120.0")) == 0.0


def test_high_24h_pct_change_pct_at_minus_100() -> None:
    # change_pct == -100 would cause division by zero in open reconstruction — must return 0
    assert _high_24h_pct(_ex("100.0", -100.0, "120.0")) == 0.0


def test_high_24h_pct_change_pct_below_minus_100() -> None:
    assert _high_24h_pct(_ex("100.0", -150.0, "120.0")) == 0.0


def test_true_peak_pct_prefers_high_24h() -> None:
    # max_change_pct=30 but high_24h implies 50% peak
    pump = {
        "base": "BTC",
        "max_change_pct": 30.0,
        "exchanges": [_ex("100.0", 25.0, "120.0")],  # high_24h_pct=50%
    }
    assert _true_peak_pct(pump) == 50.0


def test_true_peak_pct_prefers_current_when_higher() -> None:
    # max_change_pct=80 is higher than high_24h-derived 50%
    pump = {
        "base": "BTC",
        "max_change_pct": 80.0,
        "exchanges": [_ex("100.0", 25.0, "120.0")],  # high_24h_pct=50%
    }
    assert _true_peak_pct(pump) == 80.0


def test_true_peak_pct_no_exchanges() -> None:
    pump = {"base": "BTC", "max_change_pct": 45.0, "exchanges": []}
    assert _true_peak_pct(pump) == 45.0


def test_true_peak_pct_multi_exchange_takes_max() -> None:
    pump = {
        "base": "BTC",
        "max_change_pct": 30.0,
        "exchanges": [
            _ex("100.0", 25.0, "120.0"),  # 50%
            _ex("100.0", 10.0, "115.0"),  # (115/~90.9-1)*100 ≈ 26.5%
        ],
    }
    assert _true_peak_pct(pump) == 50.0
