"""Tests for the pure funding-snapshot logic (no network / CCXT)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.abnormal_flow_funding_snapshot import (
    FundingSettlement,
    build_snapshot,
    fetch_settlements,
    funding_percentiles_cadence_aware,
    old_funding_percentiles,
)

_WS = datetime(2026, 8, 16, tzinfo=UTC)
_WE = datetime(2026, 8, 30, tzinfo=UTC)


def _s(venue: str, mid: str, ts: datetime, rate: float) -> FundingSettlement:
    return FundingSettlement(
        exchange=venue, native_market_id=mid, settlement_at=ts, funding_rate=rate
    )


def test_funding_percentiles_use_max_rate_zero_per_venue() -> None:
    settlements = [
        _s("bybit", "AUSDT", _WS, -0.01),  # negative clamped to 0 (long-only conservative)
        _s("bybit", "AUSDT", _WS, 0.0),
        _s("bybit", "BUSDT", _WS, 0.001),
        _s("binance", "AUSDT", _WS, 0.0005),
    ]
    p = old_funding_percentiles(settlements)
    assert p["bybit"]["settlements"] == 3
    assert p["bybit"]["instruments"] == 2
    # values (clamped, sorted) = [0, 0, 0.001]; P95 interpolates near the top.
    assert p["bybit"]["max_rate_0_percentiles"]["p50"] == pytest.approx(0.0)
    assert 0.0 < p["bybit"]["max_rate_0_percentiles"]["p95"] <= 0.001
    assert p["binance"]["max_rate_0_percentiles"]["p95"] == pytest.approx(0.0005)


class _FakeClient:
    """Serves a fixed native->unified map and canned funding history."""

    def __init__(self, mapping: dict[str, str | None], history: dict[str, list[tuple[int, float]]]):
        self._map = mapping
        self._hist = history

    def unified_symbol(self, native_market_id: str) -> str | None:
        return self._map.get(native_market_id)

    def funding_history(self, unified_symbol: str, since_ms: int, until_ms: int):  # type: ignore[no-untyped-def]
        return self._hist.get(unified_symbol, [])


def test_fetch_settlements_records_coverage_and_filters_window() -> None:
    in_window = int(datetime(2026, 8, 20, tzinfo=UTC).timestamp() * 1000)
    before = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)  # outside window
    client = _FakeClient(
        mapping={"AUSDT": "A/USDT:USDT", "BUSDT": "B/USDT:USDT", "WEIRD": None},
        history={
            "A/USDT:USDT": [
                (before, 0.02),
                (in_window, 0.001),
            ],  # first row dropped (out of window)
            "B/USDT:USDT": [],  # resolvable but no settlements
        },
    )
    settlements, coverage = fetch_settlements(
        client, "bybit", ["AUSDT", "BUSDT", "WEIRD"], window_start=_WS, window_end=_WE
    )
    assert len(settlements) == 1 and settlements[0].native_market_id == "AUSDT"
    assert coverage["requested_instruments"] == 3
    assert coverage["covered_instruments"] == 1  # only AUSDT had an in-window settlement
    assert coverage["unresolved_symbols"] == 1  # WEIRD
    assert coverage["no_settlement_instruments"] == ["BUSDT"]


def test_build_snapshot_has_source_coverage_and_stable_hash() -> None:
    settlements = [_s("bybit", "AUSDT", _WS, 0.001), _s("binance", "BUSDT", _WS, 0.0)]
    snap = build_snapshot(
        settlements,
        window_start=_WS,
        window_end=_WE,
        coverage_by_venue={"bybit": {"covered_instruments": 1}},
        source={"method": "ccxt.fetchFundingRateHistory", "ccxt_version": "4.5.77"},
    )
    assert snap["funding_snapshot_version"].startswith("abnormal_flow_funding_snapshot")
    assert snap["content_hash"].startswith("sha256:")
    assert snap["total_settlements"] == 2
    assert "bybit" in snap["percentiles"] and "binance" in snap["percentiles"]
    # Hash is deterministic for the same content.
    snap2 = build_snapshot(
        list(reversed(settlements)),
        window_start=_WS,
        window_end=_WE,
        coverage_by_venue={},
        source={},
    )
    assert snap["content_hash"] == snap2["content_hash"]


def test_funding_cadence_aware_8h_4h_1h() -> None:
    # 8h cadence
    s_8h = []
    for i in range(-12, 48, 8):
        s_8h.append(_s("bybit", "8H", _WS + timedelta(hours=i), 0.001))

    # 4h cadence
    s_4h = []
    for i in range(-12, 48, 4):
        s_4h.append(_s("bybit", "4H", _WS + timedelta(hours=i), 0.001))

    # 1h cadence
    s_1h = []
    for i in range(-12, 48, 1):
        s_1h.append(_s("bybit", "1H", _WS + timedelta(hours=i), 0.001))

    p = funding_percentiles_cadence_aware(
        s_8h + s_4h + s_1h,
        grid_start=_WS,
        grid_end=_WS + timedelta(hours=24),
        hold_minutes=720,
        grid_step_minutes=60,
    )

    assert p["bybit"]["instruments"] == 3
    # Check max rates (at 8h cadence, max is 2 settlements in 12h)
    assert 0.001 <= p["bybit"]["max_rate_0_percentiles"]["p95"] <= 0.0121


def test_funding_cadence_aware_truncated_history() -> None:
    # Listed at WS + 6h. No settlement before WS.
    s_trunc = []
    for i in range(8, 48, 8):
        s_trunc.append(_s("binance", "TRUNC", _WS + timedelta(hours=i), 0.001))

    p = funding_percentiles_cadence_aware(
        s_trunc,
        grid_start=_WS,
        grid_end=_WS + timedelta(hours=24),
        hold_minutes=720,
        grid_step_minutes=60,
    )
    # The first few hours of grid [WS, WS+6h) should be marked incomplete.
    assert p["binance"]["incomplete_windows"] > 0
    assert p["binance"]["valid_windows"] > 0
