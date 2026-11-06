from __future__ import annotations

import asyncio
import gzip
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_compressed_spring as spring
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    MINUTE_MS,
    RANK_HOURS,
    RANK_MIN_DEFINED,
    WINDOW_END_MS,
    WINDOW_FIRST_MS,
    Features,
    Hour,
    RollingRank,
    Series,
    build_counts,
    estimate,
    grid_hours,
    hyp031_event_times,
    kept_minutes,
    last_decision_ms,
    last_rate_before,
    listed_days,
    load_features,
    minute_masked,
    non_overlapping,
    oi_growth,
    scope_days,
    size_sample,
    turnover_and_range,
    universe,
)
from schurfer_analytics.bybit_post_funding_screen import SnapshotWriter, file_sha256

if TYPE_CHECKING:
    from pathlib import Path

T0 = FIRST_DECISION_MS + 3 * DAY_MS


def perp(symbol: str, launch: int = 0, delivery: int = 0) -> dict[str, Any]:
    return {"symbol": symbol, "contractType": "LinearPerpetual", "settleCoin": "USDT",
            "launchTime": str(launch), "deliveryTime": str(delivery)}  # fmt: skip


def spring_series(t0: int = T0, end: int = WINDOW_END_MS - 1) -> Series:
    """Wide hourly ranges, flat OI, positive funding; at t0 all of conditions 1 to 3 hold."""
    series = Series(0, end)
    for start in range(WINDOW_FIRST_MS, end, HOUR_MS):
        width = 0.001 if t0 - 24 * HOUR_MS <= start < t0 else 0.02
        series.bars[start] = (100.0 * (1 + width), 100.0, 100.0, 200_000.0)
        series.oi[start] = 150.0 if start == t0 - HOUR_MS else 100.0
    for s in range(WINDOW_FIRST_MS, end, 8 * HOUR_MS):
        series.funding[s] = 0.0001
    series.funding[t0 - 2 * HOUR_MS] = -0.0001
    return series


def test_universe_keeps_delisted_perps_inside_the_window_and_caps_at_delivery() -> None:
    catalog = [
        perp("LIVEUSDT"),
        perp("GONEUSDT", delivery=WINDOW_FIRST_MS + 10 * DAY_MS),
        perp("OLDUSDT", delivery=WINDOW_FIRST_MS - DAY_MS),
        perp("LATEUSDT", launch=WINDOW_END_MS),
        {**perp("BTCPERP"), "settleCoin": "USDC"},
        {**perp("BTC-26DEC25"), "contractType": "LinearFutures"},
    ]
    assert universe(catalog) == {
        "LIVEUSDT": (0, WINDOW_END_MS - 1),
        "GONEUSDT": (0, WINDOW_FIRST_MS + 10 * DAY_MS - 1),
    }


def test_turnover_and_range_need_all_24_bars_before_t() -> None:
    series = spring_series()
    turnover, c = turnover_and_range(series, T0) or (0.0, 0.0)
    assert turnover == pytest.approx(24 * 200_000.0)
    assert c == pytest.approx(0.001)
    series.bars[T0] = (1e9, 0.0, 1.0, 1e12)  # the bar starting at t is never read
    assert turnover_and_range(series, T0) == (turnover, c)
    del series.bars[T0 - 24 * HOUR_MS]
    assert turnover_and_range(series, T0) is None


def test_open_interest_at_t_is_the_record_stamped_one_hour_earlier() -> None:
    series = Series(oi={T0 - HOUR_MS: 150.0, T0 - 25 * HOUR_MS: 100.0, T0: 1e9})
    assert oi_growth(series, T0) == pytest.approx(0.5)
    assert oi_growth(series, T0 - HOUR_MS) is None


def test_funding_sign_uses_the_last_settlement_strictly_before_t() -> None:
    times, rates = [T0 - HOUR_MS, T0], [-0.001, 0.002]
    assert last_rate_before(times, rates, T0) == -0.001
    assert last_rate_before(times, rates, T0 + HOUR_MS) == 0.002
    assert last_rate_before(times, rates, T0 - HOUR_MS) is None


def test_rolling_rank_is_a_mid_rank_over_the_previous_336_defined_values() -> None:
    ranker = RollingRank()
    for k in range(RANK_MIN_DEFINED - 1):
        ranker.push(float(k))
    assert ranker.rank(10.0) is None  # fewer than 80% defined
    ranker.push(None)
    assert ranker.rank(10.0) is None
    ranker.push(float(RANK_MIN_DEFINED))
    assert ranker.rank(-1.0) == 0.0
    assert ranker.rank(1e9) == 1.0
    assert ranker.rank(10.0) == pytest.approx(10.5 / RANK_MIN_DEFINED)
    for _ in range(RANK_HOURS):
        ranker.push(5.0)
    assert len(ranker.window) == RANK_HOURS
    assert ranker.rank(5.0) == 0.5


def test_grid_hours_pass_conditions_1_to_3_only_at_the_spring() -> None:
    hours = {h.t: h for h in grid_hours("AUSDT", spring_series())}
    assert min(hours) == FIRST_DECISION_MS
    spring_hour = hours[T0]
    assert (spring_hour.liquidity, spring_hour.c1, spring_hour.c2, spring_hour.c3) == (
        "liquid", "true", "true", "true",
    )  # fmt: skip
    assert spring_hour.passes
    assert not hours[T0 - 3 * HOUR_MS].passes  # funding still positive then
    assert [t for t, h in hours.items() if h.passes] == [T0]


def test_grid_hours_ignore_every_datum_at_or_after_t() -> None:
    base = {h.t: h for h in grid_hours("AUSDT", spring_series())}[T0]
    later = spring_series()
    for start in range(T0, T0 + DAY_MS, HOUR_MS):
        later.bars[start] = (1e6, 1.0, 1.0, 1.0)
        later.oi[start] = 1e-3
    later.funding[T0] = 0.05
    assert {h.t: h for h in grid_hours("AUSDT", later)}[T0] == base


def test_grid_hours_report_reasons_instead_of_false() -> None:
    series = spring_series()
    del series.oi[T0 - 25 * HOUR_MS]
    series.funding = {}
    hour = {h.t: h for h in grid_hours("AUSDT", series)}[T0]
    assert (hour.c2, hour.c3, hour.setup13) == ("oi_gap", "no_settlement", "undefined")
    young = spring_series()
    young.launch_ms = T0 - 2 * DAY_MS
    for start in range(WINDOW_FIRST_MS, young.launch_ms, HOUR_MS):
        del young.bars[start]
        del young.oi[start]
    hours = grid_hours("AUSDT", young)
    assert min(h.t for h in hours) == young.launch_ms
    assert {h.t: h for h in hours}[T0].c1 == "rank_history_short"


def test_grid_stops_at_delivery_and_at_the_shortest_horizon() -> None:
    gone = spring_series(end=T0 + 10 * HOUR_MS + 5)
    assert max(h.t for h in grid_hours("AUSDT", gone)) == T0 + 10 * HOUR_MS
    assert last_decision_ms(4) == WINDOW_END_MS - 5 * HOUR_MS
    assert last_decision_ms(24) == WINDOW_END_MS - 25 * HOUR_MS


def test_event_times_take_any_extreme_settlement_and_require_whole_hours() -> None:
    a = Series(funding={T0: -0.003, T0 + HOUR_MS: -0.0029})
    b = Series(funding={T0 + 8 * HOUR_MS: -0.01})
    assert hyp031_event_times(Features({"A": a, "B": b}, ())) == [T0, T0 + 8 * HOUR_MS]
    with pytest.raises(SystemExit, match="whole hour"):
        hyp031_event_times(Features({"A": Series(funding={T0 + 1: 0.0})}, ()))


def test_fixed_mask_drops_minute_55_inclusive_to_minute_05_exclusive() -> None:
    h = T0 + 7 * HOUR_MS
    assert minute_masked(h - 5 * MINUTE_MS)  # :55
    assert not minute_masked(h - 6 * MINUTE_MS)  # :54
    assert minute_masked(h)  # :00
    assert minute_masked(h + 4 * MINUTE_MS + 59_999)  # :04:59.999
    assert not minute_masked(h + 5 * MINUTE_MS)  # :05
    assert kept_minutes(h - 6 * HOUR_MS, h) == spring.S_KEPT_MINUTES == 300


def test_fixed_mask_covers_the_seconds_draft_windows_at_every_hour() -> None:
    """[s - 60 s, s + 60 s), rounded out to full minutes, for any whole-hour s."""
    for s in (T0, T0 + 13 * HOUR_MS):
        assert all(minute_masked(m) for m in range(s - MINUTE_MS, s + MINUTE_MS, 1_000))


def _hour(symbol: str, t: int) -> Hour:
    return Hour(symbol, t, "liquid", "true", "true", "true")


def test_non_overlapping_keeps_one_hold_at_a_time_per_instrument() -> None:
    hours = [_hour("A", T0 + k * HOUR_MS) for k in range(6)] + [_hour("B", T0)]
    assert non_overlapping(hours, 4) == 3  # A at T0 and T0 + 5 h, B at T0
    assert non_overlapping(hours, 12) == 2


def test_scope_days_cover_fourteen_days_and_six_hours_before_each_decision() -> None:
    days = scope_days([_hour("A", T0)])["A"]
    assert days[0] == spring._day(T0 - 15 * DAY_MS)
    assert days[-1] == spring._day(T0 - DAY_MS)
    assert len(days) == 15


def test_listing_keeps_only_this_symbol_inside_the_window() -> None:
    html = "".join(
        f'<a href="{name}">x</a>'
        for name in (
            "AUSDT2024-09-30.csv.gz",
            "AUSDT2024-10-01.csv.gz",
            "AUSDT2025-09-30.csv.gz",
            "AUSDT2025-10-01.csv.gz",
            "BAUSDT2024-10-02.csv.gz",
        )
    )
    assert listed_days("AUSDT", html) == {"2024-10-01", "2025-09-30"}


def test_size_sample_is_fixed_one_in_twenty_and_at_least_two() -> None:
    days = [spring._day(WINDOW_FIRST_MS + k * DAY_MS) for k in range(100)]
    sample = size_sample("AUSDT", days)
    assert len(sample) == 5
    assert sample == size_sample("AUSDT", list(reversed(days)))
    assert len(size_sample("AUSDT", days[:3])) == 2
    assert size_sample("AUSDT", days[:1]) == days[:1]


def test_estimate_scales_each_symbol_mean_size_to_its_days_with_a_file() -> None:
    scope = {"A": ["d1", "d2", "d3", "d4"], "B": ["d1"], "C": ["d1"]}
    listed = {"A": {"d1", "d2", "d3"}, "B": {"d1"}, "C": set()}
    sizes = {("A", "d1"): 100, ("A", "d2"): 300}
    assert estimate(scope, listed, sizes) == {
        "symbol_days": 6,
        "symbol_days_with_file": 4,
        "estimated_bytes": 600,
        "symbols_without_size": ["B"],
    }
    assert spring.cap_status(estimate(scope, listed, sizes)) == "unknown"
    assert spring.cap_status(estimate({"A": ["d1"]}, listed, sizes)) == "within"
    huge = {("A", "d1"): spring.MAX_PRIMARY_SCOPE_BYTES + 1}
    assert spring.cap_status(estimate({"A": ["d1"]}, listed, huge)) == "over"


def test_counts_report_funnel_horizons_mask_and_scope_without_values() -> None:
    series = spring_series()
    series.funding[T0] = -0.004  # an extreme settlement at t itself, counted only
    report, scope = build_counts(Features({"AUSDT": series}, ()))
    funnel = report["funnel"]
    assert funnel["setup13_true"] == 1
    assert funnel["grid"] == funnel["liquidity_liquid"]
    statuses = ("true", "false", "kline_gap", "rank_history_short")
    assert sum(funnel.get(f"c1_{s}", 0) for s in statuses) == funnel["liquidity_liquid"]
    assert report["decisions_passing_1_to_3"]["4h"]["clusters"] == 1
    assert report["mask"] == {"hyp031_event_times": 1, "s_kept_minutes": 300, "s_minutes": 360}
    assert report["tape_scope"]["primary_symbol_days"] == 15
    assert list(scope["primary"]) == ["AUSDT"]
    text = json.dumps(report)
    assert "100.0" not in text and "0.001" not in text


def test_load_features_round_trip_and_symbol_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "features.jsonl.gz"
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": spring.VERSION})
    writer.record("catalog", None, {}, {"list": [perp("AUSDT"), perp("BUSDT")]})
    writer.record("funding", "AUSDT", {}, {"result": {"list": [
        {"symbol": "AUSDT", "fundingRateTimestamp": str(T0), "fundingRate": "-0.001"},
        {"symbol": "AUSDT", "fundingRateTimestamp": str(WINDOW_FIRST_MS - HOUR_MS),
         "fundingRate": "0.1"},
    ]}})  # fmt: skip
    writer.record("kline_60", "AUSDT", {}, {"result": {"symbol": "AUSDT", "list": [
        [str(T0), "1", "2", "0.5", "1.5", "10", "15"]]}})  # fmt: skip
    writer.record("oi_60", "AUSDT", {}, {"result": {"symbol": "AUSDT", "list": [
        {"timestamp": str(T0), "openInterest": "42"}]}})  # fmt: skip
    writer.record("oi_60", "BUSDT", {}, {"result": {"symbol": "AUSDT", "list": []}})
    writer.close()
    features = load_features(path)
    assert features.failed == ("BUSDT",)
    a = features.instruments["AUSDT"]
    assert a.funding == {T0: -0.001}
    assert a.bars == {T0: (2.0, 0.5, 1.5, 15.0)}
    assert a.oi == {T0: 42.0}
    assert a.end_ms == WINDOW_END_MS - 1


def test_load_features_refuses_another_version(tmp_path: Path) -> None:
    path = tmp_path / "other.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.write(json.dumps({"kind": "version", "symbol": None, "params": {},
                                 "response": {"version": "other"}}) + "\n")  # fmt: skip
    with pytest.raises(SystemExit, match="not bybit_compressed_spring_v1"):
        load_features(path)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def _page(items: list[Any], symbol: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"list": items, "nextPageCursor": ""}
        if symbol:
            result["symbol"] = symbol
        return {"retCode": 0, "result": result}

    async def public_get_v5_market_instruments_info(self, params: dict[str, Any]) -> Any:
        live = [perp("AUSDT"), perp("GONEUSDT", delivery=T0)]
        return self._page([] if params.get("status") else live)

    async def public_get_v5_market_funding_history(self, params: dict[str, Any]) -> Any:
        self.calls.append(("funding", params))
        return self._page([])

    async def public_get_v5_market_kline(self, params: dict[str, Any]) -> Any:
        self.calls.append(("kline", params))
        return self._page([], params["symbol"])

    async def public_get_v5_market_open_interest(self, params: dict[str, Any]) -> Any:
        self.calls.append(("oi", params))
        return self._page([], params["symbol"])


def test_fetch_features_stays_inside_the_window_and_caps_at_delivery(tmp_path: Path) -> None:
    client = FakeClient()
    summary = asyncio.run(spring.fetch_features(client, tmp_path / "f.jsonl.gz"))
    assert summary["symbols"] == 2 and summary["errors"] == {}
    ends = {(kind, p["symbol"]): p.get("endTime", p.get("end")) for kind, p in client.calls}
    assert ends[("oi", "AUSDT")] == WINDOW_END_MS - 1
    assert ends[("kline", "GONEUSDT")] == T0 - 1
    starts = {p.get("startTime", p.get("start")) for _, p in client.calls}
    assert starts == {WINDOW_FIRST_MS}
    oi = next(p for kind, p in client.calls if kind == "oi")
    assert (oi["intervalTime"], oi["limit"]) == ("1h", 200)
    with pytest.raises(SystemExit, match="refusing"):
        spring.require_window(WINDOW_END_MS)


def test_counts_command_publishes_once(tmp_path: Path) -> None:
    path = tmp_path / "f.jsonl.gz"
    asyncio.run(spring.fetch_features(FakeClient(), path))
    digest = file_sha256(path)
    argv = ["counts", "--features", str(path), "--features-sha256", digest,
            "--output-dir", str(tmp_path / "out"), "--code-revision", "a" * 40,
            "--no-working-tree-dirty"]  # fmt: skip
    spring.main(argv)
    report = json.loads((tmp_path / "out" / "counts.json").read_bytes())
    assert report["features_sha256"] == digest
    assert report["scope_sha256"]
    with pytest.raises(FileExistsError):
        spring.main(argv)
    assert MINUTE_MS == 60_000


def test_fetch_archive_sizes_heads_only_the_fixed_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(spring, "_monotonic", clock.monotonic)
    monkeypatch.setattr(spring, "_sleep", clock.sleep)
    days = [spring._day(WINDOW_FIRST_MS + k * DAY_MS) for k in range(40)]
    scope = {"version": spring.VERSION, "primary": {"AUSDT": days[:30]},
             "ablation": {"AUSDT": days, "GONEUSDT": days[:2]}}  # fmt: skip
    requests: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/trading/AUSDT/":
            names = [*days, "2025-10-02", "2026-10-01"]
            html = "".join(f'<a href="AUSDT{d}.csv.gz">x</a>' for d in names)
            return httpx.Response(200, text=html)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "1000"})
        return httpx.Response(404)

    summary = asyncio.run(
        spring.fetch_archive_sizes(scope, tmp_path / "s.jsonl.gz", httpx.MockTransport(handle))
    )
    heads = [path for method, path in requests if method == "HEAD"]
    assert len(heads) == 2  # 40 days present: 1 in 20
    assert all("2025-10" not in path and "2026" not in path for _, path in requests)
    assert summary["primary"]["estimated_bytes"] == 30_000
    assert summary["ablation"]["symbol_days_with_file"] == 40
    assert summary["primary_cap"] == "within"
    with gzip.open(tmp_path / "s.jsonl.gz", "rt") as stream:
        listing = next(json.loads(line) for line in stream if '"listing"' in line)
    assert "2026-10-01" not in json.dumps(listing)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_pacer_spaces_request_starts_across_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(spring, "_monotonic", clock.monotonic)
    monkeypatch.setattr(spring, "_sleep", clock.sleep)
    pacer = spring.Pacer(4.0)
    starts: list[float] = []

    async def one() -> None:
        await pacer.wait()
        starts.append(clock.now)

    async def run() -> None:
        await asyncio.gather(*(one() for _ in range(5)))

    asyncio.run(run())
    assert starts == [0.0, 0.25, 0.5, 0.75, 1.0]


def _replay(statuses: list[int]) -> httpx.AsyncClient:
    answers = iter(statuses)

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(answers))

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def test_http_waits_out_a_block_then_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(spring, "_monotonic", clock.monotonic)
    monkeypatch.setattr(spring, "_sleep", clock.sleep)
    pacer = spring.Pacer(1000.0)

    async def call(statuses: list[int]) -> int:
        async with _replay(statuses) as client:
            return (await spring._http(client, pacer, "HEAD", "https://x/y")).status_code

    assert asyncio.run(call([403, 429, 200])) == 200
    assert [s for s in clock.slept if s >= 30] == [30.0, 60.0]
    with pytest.raises(spring.ArchiveBlockedError, match="403"):
        asyncio.run(call([403] * 5))
    assert asyncio.run(call([500, 200])) == 200
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(call([503] * 5))
