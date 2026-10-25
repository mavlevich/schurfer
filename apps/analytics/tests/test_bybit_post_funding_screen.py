from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_post_funding_screen as screen
from schurfer_analytics.bybit_post_funding_screen import (
    DAY_MS,
    HOUR_MS,
    MINUTE_MS,
    STAGES,
    Bars,
    Cluster,
    Index,
    Instrument,
    Settlement,
    SnapshotWriter,
    controls_for,
    held,
    plan,
    read_stage,
    turnover_24h,
    universe,
    week_block_lower_bound,
)

if TYPE_CHECKING:
    from pathlib import Path

D0 = STAGES["discovery"][0]
WEEK_MS = 7 * DAY_MS


def flat_bars(t: int, price: float = 100.0) -> Bars:
    return {
        m: (price, price, price, price)
        for m in range(t - 60 * MINUTE_MS, t + 65 * MINUTE_MS, MINUTE_MS)
    }


def falling_after(t: int, drop: float, start: float = 100.0) -> Bars:
    """Flat until t + 1 min, then the price sits ``drop`` lower."""
    bars = flat_bars(t, start)
    low = start * (1 - drop)
    for m in range(t + 2 * MINUTE_MS, t + 65 * MINUTE_MS, MINUTE_MS):
        bars[m] = (low, low, low, low)
    return bars


def liquid_hours(t: int, per_hour: float = 200_000.0) -> dict[int, float]:
    return {t - (k + 1) * HOUR_MS: per_hour for k in range(24)}


def test_universe_keeps_delisted_perps_inside_the_stage_and_caps_at_delivery() -> None:
    first, last, _ = STAGES["discovery"]
    catalog = [
        {"symbol": "LIVEUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT",
         "launchTime": "0", "deliveryTime": "0"},
        {"symbol": "GONEUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT",
         "launchTime": "0", "deliveryTime": str(first + 10 * DAY_MS)},
        {"symbol": "OLDUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT",
         "launchTime": "0", "deliveryTime": str(first - DAY_MS)},
        {"symbol": "LATEUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT",
         "launchTime": str(last + DAY_MS), "deliveryTime": "0"},
        {"symbol": "BTCPERP", "contractType": "LinearPerpetual", "settleCoin": "USDC",
         "launchTime": "0", "deliveryTime": "0"},
        {"symbol": "BTC-26DEC25", "contractType": "LinearFutures", "settleCoin": "USDT",
         "launchTime": "0", "deliveryTime": "0"},
    ]  # fmt: skip
    assert universe(catalog, "discovery") == {
        "LIVEUSDT": last,
        "GONEUSDT": first + 10 * DAY_MS - 1,
    }


def test_turnover_needs_all_24_hourly_bars_before_t() -> None:
    t = D0 + 2 * DAY_MS
    inst = Instrument(t, {}, liquid_hours(t))
    assert turnover_24h(inst, t) == pytest.approx(24 * 200_000.0)
    del inst.turnover[t - 5 * HOUR_MS]
    assert turnover_24h(inst, t) is None  # missing is unknown, never zero
    inst.turnover[t] = 1e9  # the bar starting at t is after the decision: ignored
    inst.turnover[t - 5 * HOUR_MS] = 0.0
    assert turnover_24h(inst, t) == pytest.approx(23 * 200_000.0)


def _settlement(symbol: str, rate: float, turnover: float | None, t: int = D0) -> Settlement:
    return Settlement(symbol, t, rate, None, None, None, turnover)


def test_controls_are_calm_liquid_same_time_and_nearest_in_turnover() -> None:
    event = _settlement("EVTUSDT", -0.005, 4e6)
    pool = [
        event,
        _settlement("AUSDT", 0.0001, 4e6),  # equal turnover
        _settlement("BUSDT", -0.0001, 8e6),
        _settlement("CUSDT", 0.00005, 2e6),  # same log distance as BUSDT: ties by symbol
        _settlement("DUSDT", 0.0, 40e6),
        _settlement("EUSDT", 0.0, 3e6),
        _settlement("FUSDT", 0.0, 100e6),
        _settlement("HOTUSDT", 0.0002, 4e6),  # not calm
        _settlement("THINUSDT", 0.0, 1e6),  # below USD 2M
        _settlement("NEWUSDT", 0.0, None),  # turnover unknown
    ]
    chosen = [c.symbol for c in controls_for(event, pool)]
    assert chosen == ["AUSDT", "EUSDT", "BUSDT", "CUSDT", "DUSDT"]


def _index(instruments: dict[str, Instrument]) -> Index:
    return Index("discovery", instruments, ())


def test_plan_fetches_primary_diagnostic_positive_and_r_prev_events_with_btc() -> None:
    t0, t1 = D0 + 2 * DAY_MS, D0 + 2 * DAY_MS + 8 * HOUR_MS
    hours = liquid_hours(t0) | liquid_hours(t1)
    index = _index(
        {
            "PRIMUSDT": Instrument(STAGES["discovery"][1], {t0: -0.004, t1: 0.0}, hours),
            "WEAKUSDT": Instrument(STAGES["discovery"][1], {t0: -0.0015}, hours),
            "POSUSDT": Instrument(STAGES["discovery"][1], {t0: 0.004}, hours),
            "CALMUSDT": Instrument(STAGES["discovery"][1], {t0: 0.0, t1: 0.0}, hours),
            "BTCUSDT": Instrument(STAGES["discovery"][1], {t0: 0.0001, t1: 0.0001}, hours),
        }
    )
    todo = plan(index)
    fetched = {(e.symbol, e.t) for e in todo.events}
    # PRIMUSDT at t1 is fetched only because its previous rate was -0.40% (r_prev diagnostic)
    assert fetched == {("PRIMUSDT", t0), ("WEAKUSDT", t0), ("POSUSDT", t0), ("PRIMUSDT", t1)}
    assert set(todo.controls) == {("PRIMUSDT", t0)}
    assert [c.symbol for c in todo.controls[("PRIMUSDT", t0)]] == ["BTCUSDT", "CALMUSDT"]
    assert ("BTCUSDT", t0) in todo.windows and ("CALMUSDT", t0) in todo.windows


def test_short_enters_at_the_next_minute_open_and_exits_at_the_t_plus_31_close() -> None:
    t = D0
    bars = flat_bars(t)
    bars[t + MINUTE_MS] = (100.0, 100.0, 100.0, 99.0)  # entry at the open, 100
    bars[t + 30 * MINUTE_MS] = (99.0, 99.0, 98.0, 98.0)  # exit at the close, 98
    bars[t + 10 * MINUTE_MS] = (100.0, 103.0, 99.0, 100.0)  # +3% against the short
    path = held(bars, t, side=-1)
    assert not isinstance(path, str)
    assert path.gross_bps == pytest.approx(200.0)
    assert path.adverse_bps == pytest.approx(300.0)
    assert not path.exit_delayed


def test_a_missing_exit_bar_uses_the_next_bar_up_to_t_plus_40_then_is_incomplete() -> None:
    t = D0
    bars = flat_bars(t)
    del bars[t + 30 * MINUTE_MS]
    bars[t + 31 * MINUTE_MS] = (100.0, 100.0, 95.0, 95.0)
    path = held(bars, t, side=-1)
    assert not isinstance(path, str) and path.exit_delayed
    assert path.gross_bps == pytest.approx(500.0)
    for m in range(t + 30 * MINUTE_MS, t + 40 * MINUTE_MS, MINUTE_MS):
        bars.pop(m, None)
    assert held(bars, t, side=-1) == "exit_missing"
    del bars[t + MINUTE_MS]
    assert held(bars, t, side=-1) == "entry_missing"


def test_week_block_bootstrap_keeps_weeks_whole() -> None:
    one_week = [Cluster("AUSDT", 0, "2024-W40", D0, v) for v in (10.0, 20.0)]
    assert week_block_lower_bound(one_week) is None
    # two weeks with opposite means: whole-week resampling can draw a week twice
    cs = [Cluster("AUSDT", 0, "2024-W40", D0, 50.0)] * 10 + [
        Cluster("BUSDT", 7, "2024-W41", D0 + WEEK_MS, -50.0)
    ] * 10
    lower = week_block_lower_bound(cs)
    assert lower is not None and lower == pytest.approx(-50.0)


def _stage_data(*, drop: float, symbols: int, weeks: int) -> tuple[Index, dict[Any, Bars]]:
    """One event per symbol and week at an 8-hour boundary, each with a calm control."""
    last = STAGES["discovery"][1]
    instruments: dict[str, Instrument] = {}
    windows: dict[Any, Bars] = {}
    for k in range(symbols):
        event_sym, calm_sym = f"E{k:03d}USDT", f"C{k:03d}USDT"
        instruments[event_sym] = Instrument(last)
        instruments[calm_sym] = Instrument(last)
        for w in range(weeks):
            # spread symbols over the hours of a week so clusters differ by day
            t = D0 + w * WEEK_MS + (k % 21) * 8 * HOUR_MS
            for sym, rate in ((event_sym, -0.005), (calm_sym, 0.0)):
                instruments[sym].funding[t] = rate
                instruments[sym].turnover.update(liquid_hours(t))
            windows[(event_sym, t)] = falling_after(t, drop)
            windows[(calm_sym, t)] = flat_bars(t)
    instruments["BTCUSDT"] = Instrument(last)
    return _index(instruments), windows


def test_reading_passes_a_large_clean_effect_and_fails_a_negative_one() -> None:
    index, windows = _stage_data(drop=0.01, symbols=45, weeks=30)
    result = read_stage(index, windows)
    assert result["clusters"] >= 300 and result["complete_share"] == 1.0
    assert result["mean_net_bps"] == pytest.approx(100.0 - 30.0, abs=1e-6)
    assert result["excess_over_control_bps"] == pytest.approx(100.0, abs=1e-6)
    assert result["verdict"] == "pass", result["conditions"]

    index, windows = _stage_data(drop=-0.01, symbols=45, weeks=30)
    assert read_stage(index, windows)["verdict"] == "fail"


def test_reading_is_insufficient_below_the_floor_or_with_missing_entries() -> None:
    index, windows = _stage_data(drop=0.01, symbols=10, weeks=30)
    assert read_stage(index, windows)["verdict"] == "insufficient_data"  # 10 symbols

    index, windows = _stage_data(drop=0.01, symbols=45, weeks=30)
    events = [key for key in windows if key[0].startswith("E")]
    for key in events[: len(events) // 10]:  # 10% of entries missing
        del windows[key][key[1] + MINUTE_MS]
    result = read_stage(index, windows)
    assert result["incomplete"] == {"entry_missing": len(events) // 10}
    assert result["verdict"] == "insufficient_data"


def test_one_position_takes_the_most_negative_rate_per_settlement_time() -> None:
    index, windows = _stage_data(drop=0.01, symbols=45, weeks=30)
    result = read_stage(index, windows)
    one = result["diagnostics"]["one_position"]
    distinct_times = len({key[1] for key in windows if key[0].startswith("E")})
    assert one["trades"] == distinct_times
    assert one["trades_per_month"] == pytest.approx(distinct_times / 12)


def test_no_request_may_end_after_the_research_boundary() -> None:
    with pytest.raises(SystemExit):
        screen.require_boundary(screen.DATA_UNTIL_MS + 1)


def test_windows_are_capped_at_delivery(tmp_path: Path) -> None:
    t = D0 + 2 * DAY_MS
    delivery_end = t + 10 * MINUTE_MS
    index_path = tmp_path / "index.jsonl.gz"
    writer = SnapshotWriter(index_path)
    writer.record("stage", None, {}, {"stage": "discovery"})
    catalog = [
        {"symbol": "GONEUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT",
         "launchTime": "0", "deliveryTime": str(delivery_end + 1)},
        {"symbol": "BTCUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT",
         "launchTime": "0", "deliveryTime": "0"},
    ]  # fmt: skip
    writer.record("catalog", None, {"status": "live"}, {"list": catalog})
    for symbol, rate in (("GONEUSDT", "-0.005"), ("BTCUSDT", "0.0001")):
        rows = [{"symbol": symbol, "fundingRateTimestamp": str(t), "fundingRate": rate}]
        writer.record("funding", symbol, {}, {"retCode": 0, "result": {"list": rows}})
        bars = [[str(h), "1", "1", "1", "1", "1", "200000"] for h in liquid_hours(t)]
        writer.record(
            "kline_60", symbol, {}, {"retCode": 0, "result": {"symbol": symbol, "list": bars}}
        )
    writer.close()

    calls: list[dict[str, Any]] = []

    class Client:
        async def public_get_v5_market_kline(self, params: dict[str, Any]) -> Any:
            calls.append(params)
            return {"retCode": 0, "result": {"symbol": params["symbol"], "list": []}}

    sha = screen.file_sha256(index_path)
    asyncio.run(screen.fetch_windows(Client(), index_path, sha, tmp_path / "windows.jsonl.gz"))
    ends = {call["symbol"]: call["end"] for call in calls}
    assert ends["GONEUSDT"] == delivery_end
    assert ends["BTCUSDT"] == t + 65 * MINUTE_MS - 1
    with pytest.raises(SystemExit):
        screen.load_windows(tmp_path / "windows.jsonl.gz", "0" * 64)  # another index
