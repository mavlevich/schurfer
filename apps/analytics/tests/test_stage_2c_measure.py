"""Stage 2C measurement v1: synthetic tests, no network.

The Bybit endpoints are replaced by a mock transport that follows the documented paging
(descending lists, ``limit``, inclusive time bounds), so the real paging, journal, budget and
stop code runs end to end against a world with known funding and a known entry basis.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import stage_2c_measure as m

if TYPE_CHECKING:
    from pathlib import Path

WS = m.WINDOW_START_MS
WE = m.WINDOW_END_MS


def ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


DELIVERIES = [
    ms(2024, 12, 27, 8),  # entry 2024-09-28: before the window
    ms(2025, 3, 28, 8),
    ms(2025, 6, 27, 8),
    ms(2025, 9, 26, 8),
    ms(2025, 12, 26, 8),
]


def spot_close(start: int) -> float:
    return 100.0 + (start // m.BAR_MS) % 7


@dataclass
class World:
    funding_rate: float = 0.0001
    basis_pct: float = 12.0
    drop_funding_at: int | None = None
    empty_dated: bool = False
    refuse_dated: bool = False
    status: int = 200
    big_body: bool = False
    calls: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.status != 200:
            return httpx.Response(self.status, content=b"{}")
        if self.big_body:
            return httpx.Response(200, content=b"x" * (m.MAX_BODY_BYTES + 10))
        params = dict(request.url.params)
        path = request.url.path
        if path.endswith("/funding/history"):
            return self._funding(params)
        if path.endswith("/kline"):
            return self._kline(params)
        if path.endswith("/instruments-info"):
            return self._catalog(params)
        return httpx.Response(404)

    @staticmethod
    def _ok(result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})

    def _funding(self, params: dict[str, str]) -> httpx.Response:
        start, end, limit = int(params["startTime"]), int(params["endTime"]), int(params["limit"])
        stamps = [
            t for t in range(WS, WE, m.SETTLE_MS) if start <= t <= end and t != self.drop_funding_at
        ]
        stamps.sort(reverse=True)
        rows = [
            {
                "symbol": params["symbol"],
                "fundingRate": f"{self.funding_rate}",
                "fundingRateTimestamp": str(t),
            }
            for t in stamps[:limit]
        ]
        return self._ok({"category": "linear", "list": rows})

    def _bar(self, params: dict[str, str], start: int) -> list[str]:
        symbol = params["symbol"]
        if params["category"] == "spot" or symbol == "ETHUSDT":
            close = spot_close(start)
        else:
            close = self._dated_close(symbol, start)
        return [str(start), "1", "1", "1", f"{close}", "1", "1"]

    def _dated_close(self, symbol: str, start: int) -> float:
        delivery = DELIVERIES[int(symbol.rsplit("-", 1)[1])]
        days = (delivery - start) / m.DAY_MS
        return spot_close(start) * (1 + self.basis_pct / 100 * days / m.YEAR_DAYS)

    def _kline(self, params: dict[str, str]) -> httpx.Response:
        start, end, limit = int(params["start"]), int(params["end"]), int(params["limit"])
        symbol = params["symbol"]
        dated = "-" in symbol
        if dated and self.refuse_dated:
            return httpx.Response(200, json={"retCode": 10001, "retMsg": "params error"})
        if dated and self.empty_dated:
            return self._ok({"category": "linear", "symbol": symbol, "list": []})
        lo, hi = start, end
        if dated:
            index = int(symbol.rsplit("-", 1)[1])
            hi = min(end, DELIVERIES[index])
        grid_start = -(-lo // m.BAR_MS) * m.BAR_MS
        starts = sorted(range(grid_start, hi + 1, m.BAR_MS), reverse=True)[:limit]
        rows = [self._bar(params, t) for t in starts]
        return self._ok({"category": params["category"], "symbol": symbol, "list": rows})

    def _catalog(self, params: dict[str, str]) -> httpx.Response:
        base = params["baseCoin"]
        items = [
            {
                "symbol": f"{base}-{index}",
                "contractType": "LinearFutures",
                "baseCoin": base,
                "launchTime": str(delivery - 180 * m.DAY_MS),
                "deliveryTime": str(delivery),
                "status": "Closed",
            }
            for index, delivery in enumerate(DELIVERIES)
        ]
        # Real catalogs also list weeklies, monthlies and a second settle-coin series for
        # the same expiry; none of them may become a cycle (they cannot be fetched here).
        for name, delivery, launch in (
            ("WEEK", ms(2025, 4, 4, 8), ms(2025, 3, 14, 8)),
            ("MONTH", ms(2025, 5, 30, 8), ms(2025, 3, 7, 8)),
            ("DUP", DELIVERIES[2], DELIVERIES[2] - 87 * m.DAY_MS),
        ):
            items.append(
                {
                    "symbol": f"{base}USDT-{name}",
                    "contractType": "LinearFutures",
                    "baseCoin": base,
                    "launchTime": str(launch),
                    "deliveryTime": str(delivery),
                    "status": "Closed",
                }
            )
        items.append(
            {
                "symbol": f"{base}USDT",
                "contractType": "LinearPerpetual",
                "baseCoin": base,
                "launchTime": "0",
                "deliveryTime": "0",
                "status": "Closed",
            }
        )
        return self._ok({"category": "linear", "list": items, "nextPageCursor": ""})


def run_of(world: World, root: Path) -> m.Run:
    m.prepare_root(root)
    client = httpx.Client(transport=httpx.MockTransport(world.handler))
    return m.Run(root, client, pace_s=0.0)


# Pure functions.


def settlements(rate: float, n: int = 1095) -> list[tuple[int, float]]:
    return [(WS + i * m.SETTLE_MS, rate) for i in range(n)]


def test_funding_stats_constant_rate_is_the_convention() -> None:
    stats = m.funding_stats(settlements(0.0001))
    assert stats["n"] == 1095
    assert stats["f_mean_pct"] == pytest.approx(10.95)
    assert stats["f_90min_pct"] == pytest.approx(10.95)


def test_funding_stats_finds_the_worst_rolling_window() -> None:
    rows = settlements(0.0002)
    # 270 settlements (90 days) at zero rate inside the year.
    rows = [(t, 0.0 if 400 <= i < 670 else r) for i, (t, r) in enumerate(rows)]
    stats = m.funding_stats(rows)
    assert stats["f_90min_pct"] == pytest.approx(0.0)
    assert stats["f_mean_pct"] > 0


def test_funding_stats_refuses_a_gap_and_an_edge() -> None:
    rows = settlements(0.0001)
    del rows[500]
    with pytest.raises(m.PartStopError) as gap:
        m.funding_stats(rows)
    assert gap.value.reason == "funding_gap"
    with pytest.raises(m.PartStopError) as edge:
        m.funding_stats(settlements(0.0001)[10:])
    assert edge.value.reason == "funding_gap"


def test_funding_stats_refuses_two_rates_for_one_settlement() -> None:
    rows = settlements(0.0001)
    rows.append((rows[3][0], 0.0005))
    with pytest.raises(m.PartStopError) as conflict:
        m.funding_stats(rows)
    assert conflict.value.reason == "funding_conflict"


def test_funding_stats_accepts_exact_duplicates_from_page_overlap() -> None:
    rows = settlements(0.0001)
    rows.append(rows[3])
    assert m.funding_stats(rows)["n"] == 1095


@pytest.mark.parametrize(
    ("f_mean", "f_90min", "expected"),
    [
        (14.50, 0.1, "i35_pass_protocol"),
        (14.499, 0.1, "i35_account_dependent"),
        (20.0, 0.0, "i35_account_dependent"),
        (20.0, None, "i35_account_dependent"),
        (5.64, 1.0, "i35_account_dependent"),
        (5.639, 1.0, "i35_fail"),
        (-3.0, -9.0, "i35_fail"),
    ],
)
def test_i35_outcome_boundaries(f_mean: float, f_90min: float | None, expected: str) -> None:
    assert m.i35_outcome(f_mean, f_90min) == expected


def test_entry_basis_is_annualized_on_notional() -> None:
    assert m.entry_basis(105.0, 100.0, 90.0) == pytest.approx(0.05 * 365 / 90 * 100)
    with pytest.raises(ValueError, match="positive"):
        m.entry_basis(105.0, 0.0, 90.0)


def test_i26_outcome_levels_and_unusable_cycles() -> None:
    assert m.i26_asset_outcome([12.0, 12.0, 3.0, 12.0])["outcome"] == "i26_pass_protocol"
    assert m.i26_asset_outcome([12.0, 12.0, -0.1, 12.0])["outcome"] == "i26_regime_dependent"
    assert m.i26_asset_outcome([7.0, 7.0, 1.0, 1.0])["outcome"] == "i26_regime_dependent"
    assert m.i26_asset_outcome([7.0, 1.0, 1.0, 1.0])["outcome"] == "i26_fail"
    # An unusable cycle counts in the denominator and never passes.
    assert m.i26_asset_outcome([12.0, None, None, None])["outcome"] == "i26_fail"
    assert m.i26_asset_outcome([12.0, 12.0, None, None])["outcome"] == "i26_pass_protocol"
    assert m.i26_asset_outcome([])["outcome"] == "i26_no_cycles"


def test_quarterly_means_the_last_friday_of_a_quarter_month() -> None:
    assert all(m.is_quarterly(d) for d in DELIVERIES)
    assert not m.is_quarterly(ms(2025, 4, 4, 8))  # a Friday, but a weekly in April
    assert not m.is_quarterly(ms(2025, 3, 21, 8))  # March, Friday, not the last one
    assert not m.is_quarterly(ms(2025, 3, 29, 8))  # March, last Saturday
    assert m.is_quarterly(ms(2024, 12, 27, 8))  # December rolls into the next year


def test_quarterly_cycles_keep_one_instrument_per_expiry() -> None:
    first = m.Instrument("BTC-27JUN25", "BTC", ms(2024, 7, 5), DELIVERIES[2])
    second = m.Instrument("BTCUSDT-27JUN25", "BTC", ms(2025, 4, 1), DELIVERIES[2])
    weekly = m.Instrument("BTCUSDT-04APR25", "BTC", ms(2025, 3, 14), ms(2025, 4, 4, 8))
    eth = m.Instrument("ETH-27JUN25", "ETH", ms(2024, 7, 5), DELIVERIES[2])
    chosen, excluded = m.quarterly_cycles([second, weekly, eth, first])
    assert [i.symbol for i in chosen] == ["BTC-27JUN25", "ETH-27JUN25"]
    assert excluded == {"not_quarterly": 1, "duplicate_expiry": 1}


def test_cycle_entry_windows() -> None:
    inst = m.Instrument("X", "BTC", DELIVERIES[0] - 180 * m.DAY_MS, DELIVERIES[0])
    assert m.cycle_entry(inst) == (None, "entry_before_window")
    inst = m.Instrument("X", "BTC", DELIVERIES[1] - 180 * m.DAY_MS, DELIVERIES[1])
    assert m.cycle_entry(inst) == (DELIVERIES[1] - 90 * m.DAY_MS, None)
    inst = m.Instrument("X", "BTC", DELIVERIES[4] + m.DAY_MS, DELIVERIES[4] + 90 * m.DAY_MS)
    assert m.cycle_entry(inst) == (None, "entry_after_window")
    # Listed later than delivery minus 90 days: the first bar after listing is the entry.
    late = m.Instrument("X", "BTC", DELIVERIES[2] - 30 * m.DAY_MS + 1, DELIVERIES[2])
    entry, reason = m.cycle_entry(late)
    assert reason is None
    assert entry == m.ceil_bar(DELIVERIES[2] - 30 * m.DAY_MS + 1)


# The run, end to end.


def test_full_run_reads_the_known_world(tmp_path: Path) -> None:
    world = World(funding_rate=0.0001, basis_pct=12.0)
    report = m.run_measurement(run_of(world, tmp_path / "run"))
    assert report["run_stop"] is None
    assert report["part_stops"] == {}
    assert report["i35"]["f_mean_pct"] == pytest.approx(10.95)
    assert report["i35"]["outcome"] == "i35_account_dependent"
    assert report["i35"]["eth_kline_coverage"]["missing_bars"] == 0
    i26 = report["i26"]
    assert i26["excluded_cycles"] == {
        "not_quarterly": 4,
        "duplicate_expiry": 2,
        "entry_before_window": 2,
    }
    for asset in m.ASSETS:
        assert i26["assets"][asset]["cycles"] == 4
        assert i26["assets"][asset]["outcome"] == "i26_pass_protocol"
    assert i26["idea_passes"] is True
    assert all(row["a_pct"] == pytest.approx(12.0) for row in i26["cycles"])
    # 6 funding + 3 ETH klines + 2 catalogs + 8 entries + 6 spot = 25 requests.
    assert report["requests"] == 25
    assert len(world.calls) == 25


def test_a_low_basis_fails_the_idea_and_funding_can_pass(tmp_path: Path) -> None:
    world = World(funding_rate=0.00020, basis_pct=3.0)
    report = m.run_measurement(run_of(world, tmp_path / "run"))
    assert report["i35"]["outcome"] == "i35_pass_protocol"
    assert report["i26"]["idea_passes"] is False
    assert report["i26"]["assets"]["BTC"]["outcome"] == "i26_fail"


def test_journal_and_raw_bodies_agree(tmp_path: Path) -> None:
    root = tmp_path / "run"
    m.run_measurement(run_of(World(), root))
    lines = [json.loads(line) for line in (root / "journal.jsonl").read_text().splitlines()]
    assert len(lines) == 25
    for entry in lines:
        body = (root / "raw" / f"{entry['n']:03d}.json").read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
        assert len(body) == entry["bytes"]
        assert entry["status"] == 200


def test_a_funding_gap_stops_only_the_funding_part(tmp_path: Path) -> None:
    world = World(drop_funding_at=WS + 500 * m.SETTLE_MS)
    report = m.run_measurement(run_of(world, tmp_path / "run"))
    assert report["part_stops"] == {"i35": "funding_gap"}
    assert report["i35"] is None
    assert report["i26"]["idea_passes"] is True


@pytest.mark.parametrize("variant", ["empty_dated", "refuse_dated"])
def test_missing_dated_history_stops_only_the_basis_part(tmp_path: Path, variant: str) -> None:
    world = World(empty_dated=variant == "empty_dated", refuse_dated=variant == "refuse_dated")
    report = m.run_measurement(run_of(world, tmp_path / "run"))
    assert report["part_stops"] == {"i26": "dated_history_unavailable"}
    assert report["i26"] is None
    assert report["i35"]["outcome"] == "i35_account_dependent"
    # The probe ran before any spot kline was requested.
    assert not any("category=spot" in call for call in world.calls)


def test_a_transport_error_stops_the_run_after_three_attempts(tmp_path: Path) -> None:
    world = World(status=500)
    report = m.run_measurement(run_of(world, tmp_path / "run"))
    assert report["run_stop"].startswith("fetch_error")
    assert report["requests"] == m.MAX_ATTEMPTS
    assert report["i35"] is None
    assert report["i26"] is None


def test_an_oversized_response_stops_the_run_uncounted_as_data(tmp_path: Path) -> None:
    report = m.run_measurement(run_of(World(big_body=True), tmp_path / "run"))
    assert report["run_stop"] == "response_too_large"
    assert report["i35"] is None


def test_the_budget_ceiling_stops_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BUDGET", 7)
    report = m.run_measurement(run_of(World(), tmp_path / "run"))
    assert report["run_stop"] == "budget_exceeded"
    assert report["requests"] == 7


def test_the_report_is_published_once(tmp_path: Path) -> None:
    run = run_of(World(), tmp_path / "run")
    m.run_measurement(run)
    with pytest.raises(FileExistsError):
        m.run_measurement(run)


# Approval and root.


def test_plan_hash_is_stable_and_bound_to_the_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    first = m.go_sha256()
    assert first == m.go_sha256()
    monkeypatch.setattr(m, "I35_PASS_F", 13.0)
    assert m.go_sha256() != first


def test_fetch_refuses_a_hash_that_is_not_the_plans(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="go hash"):
        m.main(["fetch", "--approved-sha256", "0" * 64, "--root", str(tmp_path / "run")])
    assert not (tmp_path / "run").exists()


def test_root_must_be_outside_the_repository_and_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        m.prepare_root(m.repo_root() / "runtime" / "stage-2c-test-root")
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("1")
    with pytest.raises(ValueError, match="empty"):
        m.prepare_root(full)


def test_plan_prints_without_a_network(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["go_sha256"] == m.go_sha256()
    assert out["plan"]["budget"] == 100
