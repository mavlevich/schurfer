from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import bybit_presettlement_feasibility as b1
from schurfer_analytics.bybit_post_funding_screen import (
    DAY_MS,
    HOUR_MS,
    MINUTE_MS,
    Index,
    Instrument,
)
from schurfer_analytics.bybit_presettlement_feasibility import (
    HourlyPremium,
    Interval,
    MinutePremium,
)

if TYPE_CHECKING:
    from pathlib import Path

T0 = b1.FIRST_MS + 10 * DAY_MS  # a settlement inside the discovery stage


def hour_interval(symbol: str = "AAAUSDT", s1: int = T0, *, uncertain: bool = False) -> Interval:
    return Interval(symbol, s1, s1 + HOUR_MS, 60, uncertain, s1 + HOUR_MS)


def closes_for(iv: Interval, first_half: float, second_half: float) -> dict[int, float]:
    starts = b1.minute_starts(iv)
    half = len(starts) // 2
    return {m: (first_half if k < half else second_half) for k, m in enumerate(starts)}


def test_observed_weight_share_matches_the_design_table() -> None:
    assert b1.observed_weight_share(60, 30) == pytest.approx(0.254, abs=5e-4)
    assert b1.observed_weight_share(480, 30) == pytest.approx(0.879, abs=5e-4)
    assert b1.observed_weight_share(240, 10) == pytest.approx(0.919, abs=5e-4)


def test_review_example_signal_fires_before_settlement_but_not_at_it() -> None:
    """Premium -0.40% in the first 30 minutes, 0 in the last 30, 1-hour interval:
    the signal at t - 30 is -0.35%, the settled rate about -0.052%."""
    iv = hour_interval()
    closes = closes_for(iv, -0.004, 0.0)
    assert b1.decision_rate(iv, closes, 30) == pytest.approx(-0.0035)
    assert b1.full_rate(iv, closes) == pytest.approx(-0.000516, abs=1e-6)


def test_decision_rate_ignores_minutes_after_the_cut() -> None:
    iv = hour_interval()
    closes = closes_for(iv, 0.0, 0.0)
    cut = iv.predicted - 30 * MINUTE_MS - b1.PUBLICATION_LAG_MS
    for m in b1.minute_starts(iv):
        if m + MINUTE_MS > cut:
            closes[m] = -1.0  # a future value that must not move the signal
    rate = b1.decision_rate(iv, closes, 30)
    assert rate == pytest.approx(b1.interest(60))


def test_gap_before_the_cut_makes_the_signal_undefined() -> None:
    iv = hour_interval()
    closes = closes_for(iv, -0.004, -0.004)
    del closes[iv.s1 + 5 * MINUTE_MS]
    assert b1.decision_rate(iv, closes, 30) is None
    assert b1.full_rate(iv, closes) is None


def test_clamp_and_interest() -> None:
    assert b1.funding_from([(1, 0.0)], 480) == pytest.approx(0.0001)
    assert b1.funding_from([(1, -0.004)], 480) == pytest.approx(-0.0035)
    assert b1.funding_from([(1, 0.004)], 480) == pytest.approx(0.0035)


def test_superset_keeps_the_review_example_and_missing_hours() -> None:
    iv = hour_interval()
    assert b1.in_superset(iv, {T0: -0.004})
    assert not b1.in_superset(iv, {T0: -0.0019})
    assert b1.in_superset(iv, {})  # a missing hour cannot exclude


def test_superset_bound_holds_for_any_interest() -> None:
    """F_hat <= -0.30% implies P_hat <= -0.25% whatever I is, so a minute at or below
    -0.25% exists; -0.20% leaves a margin."""
    for p in (-0.0025, -0.0026, -0.004):
        assert b1.funding_from([(1, p)], 60) >= p - b1.CLAMP - 1e-12
    assert b1.funding_from([(1, -0.0024)], 60) > b1.TRIGGER


def test_intervals_predict_from_past_spacing_only() -> None:
    funding = {T0 - 2 * HOUR_MS: -0.001, T0 - HOUR_MS: -0.001, T0: -0.001, T0 + 4 * HOUR_MS: 0.0}
    index = Index("discovery", {"AAAUSDT": Instrument(b1.LAST_MS, funding, {})}, ())
    by_s1 = {iv.s1: iv for iv in b1.intervals(index)}
    iv = by_s1[T0]
    assert iv.predicted == T0 + HOUR_MS  # from the last spacing, not the real next settlement
    assert iv.actual_next == T0 + 4 * HOUR_MS
    assert not iv.schedule_uncertain  # two equal spacings before it
    assert by_s1[T0 - HOUR_MS].schedule_uncertain  # one spacing only, nothing confirms it


def test_recent_extreme_uses_only_earlier_settlements_within_a_week() -> None:
    cut = T0
    assert b1.recent_extreme({cut - DAY_MS: -0.003}, cut)
    assert not b1.recent_extreme({cut - 8 * DAY_MS: -0.004}, cut)
    assert not b1.recent_extreme({cut + MINUTE_MS: -0.01}, cut)  # after the decision
    assert not b1.recent_extreme({cut - DAY_MS: -0.0029}, cut)


def test_liquidity_uses_only_closed_hourly_bars() -> None:
    cut = T0 + 30 * MINUTE_MS  # mid-hour
    last_closed = T0 - HOUR_MS  # the bar [T0 - 1h, T0) ended before the cut
    bars = {last_closed - k * HOUR_MS: 100_000.0 for k in range(24)}
    assert b1.liquid_at(bars, cut)
    bars[T0] = 10**9  # the bar still open at the cut must not count
    del bars[last_closed - 23 * HOUR_MS]
    assert not b1.liquid_at(bars, cut)


def _index_with(funding: dict[int, float], turnover: dict[int, float]) -> Index:
    return Index("discovery", {"AAAUSDT": Instrument(b1.LAST_MS, funding, turnover)}, ())


def test_classify_statuses() -> None:
    iv = hour_interval()
    closes = closes_for(iv, -0.004, -0.004)
    turnover = {T0 - (k + 1) * HOUR_MS: 200_000.0 for k in range(30)}
    proven = {T0 - DAY_MS: -0.004, T0: -0.001}
    assert b1.classify(_index_with(proven, turnover), iv, closes, 30)[0] == "admitted"
    assert b1.classify(_index_with({T0: -0.001}, turnover), iv, closes, 30)[0] == (
        "no_recent_extreme"
    )
    assert b1.classify(_index_with(proven, {}), iv, closes, 30)[0] == "illiquid"
    calm = closes_for(iv, 0.0, 0.0)
    assert b1.classify(_index_with(proven, turnover), iv, calm, 30)[0] == "no_trigger"
    uncertain = hour_interval(uncertain=True)
    assert b1.classify(_index_with(proven, turnover), uncertain, closes, 30)[0] == (
        "schedule_uncertain"
    )


def s1_world(
    n_below: int, n_above: int, *, offset: float = 0.0, missing: int = 0
) -> tuple[Index, list[Interval], HourlyPremium, MinutePremium]:
    """Hourly intervals of one symbol, every other hour; premiums vary slightly so no
    settled value repeats (no possible cap). Settled = rebuilt + offset."""
    funding: dict[int, float] = {}
    closes: dict[int, float] = {}
    pool: list[Interval] = []
    for k in range(n_below + n_above):
        s1 = T0 + 2 * k * HOUR_MS
        iv = Interval("AAAUSDT", s1, s1 + HOUR_MS, 60, False, s1 + HOUR_MS)
        premium = (-0.004 if k < n_below else -0.0018) - k * 1e-8
        own = closes_for(iv, premium, premium)
        rebuilt = b1.full_rate(iv, own)
        assert rebuilt is not None
        funding[iv.predicted] = rebuilt + offset
        if k >= missing:
            closes.update(own)
        pool.append(iv)
    index = _index_with(funding, {})
    hourly = HourlyPremium({"AAAUSDT": {}}, {"AAAUSDT": {}}, ())
    return index, pool, hourly, MinutePremium({"AAAUSDT": closes}, ())


def test_s1_passes_with_enough_scored_settlements_on_both_sides() -> None:
    report = b1.s1(*s1_world(200, 200))
    assert report["relevant"] == 400
    assert report["within_tolerance_share"] == 1.0
    assert report["class_agreement_share"] == 1.0
    assert report["status"] == "pass"


def test_s1_review_repro_missing_interval_is_not_a_pass() -> None:
    """Two intervals, one without data: the survivor alone must not pass."""
    report = b1.s1(*s1_world(1, 1, missing=1))
    assert report["coverage"] == 0.5
    assert report["status"] == "insufficient_data"


def test_s1_needs_coverage_and_both_sides() -> None:
    assert b1.s1(*s1_world(400, 10))["status"] == "insufficient_data"  # one side thin
    assert b1.s1(*s1_world(200, 200, missing=30))["status"] == "insufficient_data"
    assert b1.s1(*s1_world(200, 200, missing=10))["status"] == "pass"  # 97.5% coverage


def test_s1_fails_when_the_formula_misses() -> None:
    assert b1.s1(*s1_world(200, 200, offset=0.0003))["status"] == "fail"  # 3 bps off


def test_superseded_by_a_settlement_known_before_the_decision() -> None:
    """Review repro: 8-hour spacing up to 00:00, then hourly settlements from 01:00.
    At 07:30 the 00:00-08:00 forecast is stale and must not be admitted."""
    midnight = T0 - T0 % DAY_MS + DAY_MS
    funding = {midnight - 16 * HOUR_MS: -0.004, midnight - 8 * HOUR_MS: -0.004, midnight: -0.004}
    funding |= {midnight + h * HOUR_MS: -0.004 for h in range(1, 8)}
    turnover = {midnight + 7 * HOUR_MS - (k + 1) * HOUR_MS: 200_000.0 for k in range(40)}
    index = _index_with(funding, turnover)
    stale = next(iv for iv in b1.intervals(index) if iv.s1 == midnight)
    assert stale.predicted == midnight + 8 * HOUR_MS
    closes = closes_for(stale, -0.004, -0.004)
    assert b1.classify(index, stale, closes, 30)[0] == "superseded"
    # the hourly regime opens its own intervals; the first is uncertain (spacings differ)
    first_hourly = next(iv for iv in b1.intervals(index) if iv.s1 == midnight + HOUR_MS)
    assert first_hourly.schedule_uncertain


def test_a_settlement_after_the_cut_does_not_supersede() -> None:
    iv = hour_interval()
    times = [T0 - 2 * HOUR_MS, T0 - HOUR_MS, T0, iv.predicted - 5 * MINUTE_MS]
    assert not b1.superseded(times, iv, iv.predicted - 30 * MINUTE_MS)
    assert b1.superseded(times, iv, iv.predicted - 4 * MINUTE_MS)


def test_event_counts_and_floor() -> None:
    rows = [hour_interval(f"S{k:03d}USDT", T0 + k * 7 * DAY_MS) for k in range(40)]
    rows += [hour_interval(f"S{k:03d}USDT", T0 + k * 7 * DAY_MS + DAY_MS) for k in range(40)]
    rows += [hour_interval(f"S{k:03d}USDT", T0 + k * 7 * DAY_MS + 2 * DAY_MS) for k in range(40)]
    rows += [hour_interval(f"S{k:03d}USDT", T0 + k * 7 * DAY_MS + 3 * DAY_MS) for k in range(40)]
    counts = b1.event_counts(rows)
    assert counts["clusters"] == 160
    assert counts["symbols"] == 40
    assert counts["weeks"] >= 26
    assert counts["top5_symbol_share"] == pytest.approx(20 / 160)
    assert b1.floor_ok(counts)
    assert not b1.floor_ok(b1.event_counts(rows[:40]))


def test_validation_sample_is_deterministic_and_rate_free() -> None:
    pool = [hour_interval(f"S{k:04d}USDT") for k in range(3000)]
    first = b1.validation_sample(pool)
    assert first == b1.validation_sample(list(reversed(pool)))
    assert len(first) == b1.VALIDATION_SAMPLE


def test_snapshots_are_unpinned_until_fetched() -> None:
    assert b1.HOURLY_SHA256 is None
    assert b1.MINUTES_SHA256 is None


def test_s1_tolerance_is_two_bps_with_a_strict_diagnostic() -> None:
    report = b1.s1(*s1_world(200, 200, offset=0.00015))  # 1.5 bps off
    assert report["within_tolerance_share"] == 1.0
    assert report["within_strict_share"] == 0.0
    assert report["status"] == "pass"


def test_s3_reports_each_settlement_interval_apart() -> None:
    one_hour = hour_interval("AAAUSDT")
    four_hour = Interval("BBBUSDT", T0, T0 + 4 * HOUR_MS, 240, False, T0 + 4 * HOUR_MS)
    minutes = MinutePremium(
        {
            "AAAUSDT": closes_for(one_hour, -0.004, -0.004),
            "BBBUSDT": closes_for(four_hour, -0.004, -0.004),
        },
        (),
    )
    turnover = {T0 + 3 * HOUR_MS - (k + 1) * HOUR_MS: 200_000.0 for k in range(40)}
    proven = {T0 - DAY_MS: -0.004}
    index = Index(
        "discovery",
        {
            "AAAUSDT": Instrument(b1.LAST_MS, dict(proven), dict(turnover)),
            "BBBUSDT": Instrument(b1.LAST_MS, dict(proven), dict(turnover)),
        },
        (),
    )
    report = b1.s3(index, [one_hour, four_hour], minutes)["d30"]
    assert set(report["by_interval"]) == {"1h", "4h"}
    assert report["by_interval"]["1h"]["admitted"]["events"] == 1
    assert report["by_interval"]["4h"]["admitted"]["events"] == 1
    assert report["admitted"]["events"] == 2


def test_review_repro_pooled_pass_does_not_open_a_class_without_its_own_data() -> None:
    """Review P1 (2026-11-12): a pooled pass made of 1 h settlements must not open B2
    for 4 h contracts. The 4 h class has no settlement of its own, so it cannot pass."""
    index, pool, hourly, minutes = s1_world(200, 200)
    decision = {"d30": {"floor": True, "by_interval": {"1h": {"floor": True}}}}
    gates = b1.interval_gates(index, pool, hourly, minutes, decision, "pass")
    assert gates["1h"]["verdict"] == "pass"
    assert gates["1h"]["passing_decision_offsets"] == ["d30"]
    assert gates["4h"]["s1"]["status"] == "insufficient_data"
    assert gates["4h"]["verdict"] == "insufficient_validation"


def test_class_needs_its_own_floor() -> None:
    index, pool, hourly, minutes = s1_world(200, 200)
    decision = {
        "d30": {"floor": True, "by_interval": {"1h": {"floor": False}, "4h": {"floor": True}}}
    }
    gates = b1.interval_gates(index, pool, hourly, minutes, decision, "pass")
    assert gates["1h"]["verdict"] == "insufficient_events"


def test_class_inherits_a_failed_pooled_formula_check() -> None:
    index, pool, hourly, minutes = s1_world(200, 200)
    decision = {"d30": {"floor": True, "by_interval": {"1h": {"floor": True}}}}
    gates = b1.interval_gates(index, pool, hourly, minutes, decision, "not_reconstructible")
    assert gates["1h"]["s1"]["status"] == "pass"
    assert gates["1h"]["verdict"] == "not_reconstructible"


class FakeBybit:
    """Answers premium index kline requests like Bybit: newest first, capped at limit."""

    def __init__(self, bars: dict[tuple[str, str], dict[int, tuple[float, float, float, float]]]):
        self.bars = bars
        self.requests: list[dict[str, object]] = []

    async def public_get_v5_market_premium_index_price_kline(
        self, params: dict[str, object]
    ) -> dict[str, object]:
        self.requests.append(params)
        symbol, interval = str(params["symbol"]), str(params["interval"])
        start, end, limit = (
            int(str(params["start"])),
            int(str(params["end"])),
            int(str(params["limit"])),
        )
        series = self.bars.get((symbol, interval), {})
        rows = sorted((ts for ts in series if start <= ts <= end), reverse=True)[:limit]
        items = [[str(ts), *(str(v) for v in series[ts])] for ts in rows]
        return {"retCode": "0", "result": {"symbol": symbol, "category": "linear", "list": items}}


def test_api_to_snapshot_to_report(tmp_path: Path) -> None:
    """The whole path on artificial responses: fetch, write, load, plan, report."""
    s1_time = T0
    funding = {s1_time - 2 * HOUR_MS: -0.004, s1_time - HOUR_MS: -0.004, s1_time: -0.004,
               s1_time + HOUR_MS: -0.004}  # fmt: skip
    turnover = {s1_time - (k + 1) * HOUR_MS: 200_000.0 for k in range(40)}
    index = Index("discovery", {"AAAUSDT": Instrument(b1.LAST_MS, funding, turnover)}, ())
    hours = {h: (-0.004, -0.0039, -0.0041, -0.004) for h in range(s1_time - 3 * HOUR_MS,
             s1_time + 2 * HOUR_MS, HOUR_MS)}  # fmt: skip
    minutes = {s1_time - 3 * HOUR_MS + k * MINUTE_MS: (-0.004, -0.004, -0.004, -0.004)
               for k in range(5 * 60)}  # fmt: skip
    client = FakeBybit({("AAAUSDT", "60"): hours, ("AAAUSDT", "1"): minutes})

    hourly_path = tmp_path / "hourly.jsonl.gz"
    coverage = asyncio.run(b1.fetch_hourly(client, index, hourly_path))
    assert coverage["errors"] == {}
    hourly = b1.load_hourly(hourly_path)
    assert hourly.low["AAAUSDT"][s1_time] == -0.0041

    todo, counts = b1.minute_todo(index, hourly)
    assert counts["superset"] >= 1
    assert all(r["interval"] == "60" for r in client.requests)
    minutes_path = tmp_path / "minutes.jsonl.gz"
    asyncio.run(b1.fetch_minutes(client, todo, minutes_path))
    assert all(int(str(r["end"])) <= b1.LAST_MS for r in client.requests)
    loaded = b1.load_minutes(minutes_path)
    assert loaded.closes["AAAUSDT"][s1_time] == -0.004

    report = b1.build_report(index, hourly, loaded)
    d30 = report["s3"]["d30"]
    assert d30["status"].get("admitted", 0) >= 1
    assert report["s1"]["status"] == "insufficient_data"  # four settlements cannot validate
    assert report["verdict"] == "insufficient_validation"
    assert report["by_interval"]["4h"]["verdict"] == "insufficient_validation"


def test_s1_review_repro_extreme_side_cannot_hide_behind_the_ordinary_side() -> None:
    """3,000 scored settlements, 50 truly at or below -0.30%, none recognised, every
    error within 2 bps: overall agreement 98.3% but the extreme side fails."""
    funding: dict[int, float] = {}
    closes: dict[int, float] = {}
    pool: list[Interval] = []
    for k in range(3000):
        s1_time = T0 + 2 * k * HOUR_MS
        iv = Interval("AAAUSDT", s1_time, s1_time + HOUR_MS, 60, False, s1_time + HOUR_MS)
        extreme = k < 50
        premium = (-0.00349 if extreme else -0.0018) - k * 1e-9
        own = closes_for(iv, premium, premium)
        rebuilt = b1.full_rate(iv, own)
        assert rebuilt is not None
        # extreme: rebuilt just above the trigger, settled just below it (1.5 bps apart)
        funding[iv.predicted] = rebuilt - 0.00015 if extreme else rebuilt
        closes.update(own)
        pool.append(iv)
    index = _index_with(funding, {})
    hourly = HourlyPremium({"AAAUSDT": {}}, {"AAAUSDT": {}}, ())
    report = b1.s1(index, pool, hourly, MinutePremium({"AAAUSDT": closes}, ()))
    assert report["relevant_at_or_below_trigger"] == 50
    assert report["class_agreement_share"] > 0.98
    assert report["within_tolerance_share"] == 1.0
    assert report["sensitivity_at_trigger"] == 0.0
    assert report["status"] == "fail"
