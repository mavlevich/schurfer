from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import gate_collection_budget as budget_mod

if TYPE_CHECKING:
    from pathlib import Path

GIB = budget_mod.GIB
MIB = budget_mod.MIB


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sizes_come_from_head_requests_inside_the_boundary() -> None:
    seen: list[tuple[str, str]] = []
    statuses = iter([500, 200])

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if "MISSING" in request.url.path:
            return httpx.Response(404)
        if "RETRY" in request.url.path and "202606" in request.url.path:
            return httpx.Response(next(statuses), headers={"content-length": "7"})
        return httpx.Response(200, headers={"content-length": "1000"})

    sleeps: list[float] = []
    with _client(handle) as client:
        sizes, log = budget_mod.measure_sizes(
            client, ["AAA", "MISSING", "RETRY"], sleep=sleeps.append
        )
    assert sizes["AAA"] == {"202606": 1000, "202607": 1000}
    assert sizes["MISSING"] == {"202606": None, "202607": None}
    assert sizes["RETRY"]["202606"] == 7 and sleeps == [1.0]
    assert {method for method, _ in seen} == {"HEAD"}
    assert [row["status"] for row in log if row["base"] == "RETRY"] == [500, 200, 200]
    assert log[0]["window_end"] == "2026-07-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(403),
        httpx.Response(200, headers={"content-length": ""}),
        httpx.Response(200),
    ],
)
def test_an_unmeasured_archive_stops_instead_of_counting_as_absent(
    response: httpx.Response,
) -> None:
    with (
        _client(lambda _r: response) as client,
        pytest.raises(budget_mod.BudgetStoppedError, match="unmeasured archive"),
    ):
        budget_mod.measure_sizes(client, ["AAA"], sleep=lambda _s: None)


def test_a_transport_failure_after_retries_stops_the_measurement() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with (
        _client(handle) as client,
        pytest.raises(budget_mod.BudgetStoppedError, match="transport error"),
    ):
        budget_mod.measure_sizes(client, ["AAA"], sleep=lambda _s: None)


def test_a_month_past_the_boundary_is_refused_before_sending() -> None:
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200)

    with (
        _client(handle) as client,
        pytest.raises(budget_mod.ProtocolViolationError, match="data window ends"),
    ):
        budget_mod.measure_sizes(client, ["AAA"], months=("202608",))
    assert sent == []


def test_a_rate_limit_stops_the_measurement() -> None:
    with (
        _client(lambda _r: httpx.Response(429)) as client,
        pytest.raises(budget_mod.BudgetStoppedError, match="429"),
    ):
        budget_mod.measure_sizes(client, ["AAA"])


def test_conversion_profile_measures_expansion_and_parquet(tmp_path: Path) -> None:
    rows = "\n".join(f"{1782864000 + i}.123456,{1000 + i},1.0,{(-1) ** i * 3}" for i in range(500))
    path = tmp_path / "a3f0"  # content-addressed name, no extension, like the raw store
    path.write_bytes(gzip.compress((rows + "\n").encode()))
    profile = budget_mod.conversion_profile([path])
    (row,) = profile["files"]
    assert row["csv_bytes"] == len(rows) + 1
    assert profile["csv_per_gz"] > 1 and profile["parquet_per_gz"] > 0
    assert profile["seconds_per_gib_gz"] > 0


PROFILE = {"csv_per_gz": 5.0, "parquet_per_gz": 0.8, "seconds_per_gib_gz": 60.0}


def test_budget_arithmetic_and_headroom() -> None:
    sizes: dict[str, dict[str, int | None]] = {
        "A": {"202606": GIB, "202607": 3 * GIB},
        "B": {"202606": GIB, "202607": GIB},
        "C": {"202606": None, "202607": GIB},  # listed in July: its July still counts
    }
    rows = budget_mod.budget(sizes, {"all": ["A", "B", "C"]}, PROFILE)
    row = rows["all"]
    assert row["status"] == "measured"
    assert row["bases_with_both_months"] == 2 and row["bases_with_one_month"] == ["C"]
    assert row["gz_bytes_by_month"] == {"202606": 2 * GIB, "202607": 5 * GIB}
    assert row["monthly_gib"]["raw_gz"] == pytest.approx(5.0)  # the peak month
    assert row["monthly_gib"]["parquet"] == pytest.approx(4.0)
    assert row["cumulative_gib"]["6m"] == pytest.approx(54.0)
    assert row["scratch_gib"]["gunzip_first"] == pytest.approx(15.0)
    assert row["scratch_gib"]["streaming_conversion"] == pytest.approx(2.4)
    assert row["conversion_elapsed_minutes_per_month"] == pytest.approx(5.0)
    room = budget_mod.headroom(
        rows, free_gib=100.0, reserve_gib=10.0, other_growth_gib_per_day=0.1, release_gib=5.0
    )["all"]
    days = 6 * budget_mod.DAYS_PER_MONTH
    assert room["usable_gib"] == 95.0
    assert room["need_gib"] == pytest.approx(54.0 + 2.4 + 0.1 * days)
    assert room["fits"] is True
    assert room["days_until_reserve"] == pytest.approx(
        95.0 / (9.0 / budget_mod.DAYS_PER_MONTH + 0.1)
    )
    assert room["max_other_growth_mib_per_day"] == pytest.approx((95.0 - 56.4) / days * 1024)


def test_other_growth_eats_the_space_above_the_reserve_not_the_reserve() -> None:
    rows = budget_mod.budget({"A": {"202606": GIB, "202607": GIB}}, {"all": ["A"]}, PROFILE)
    kwargs: dict[str, Any] = {"free_gib": 25.0, "reserve_gib": 10.0}
    assert budget_mod.headroom(rows, other_growth_gib_per_day=0.0, **kwargs)["all"]["fits"]
    room = budget_mod.headroom(rows, other_growth_gib_per_day=0.05, **kwargs)["all"]
    assert room["fits"] is False and room["usable_gib"] == 15.0


def test_missing_or_unmeasured_data_never_fits() -> None:
    sizes: dict[str, dict[str, int | None]] = {"AAA": {"202606": None, "202607": None}}
    kwargs: dict[str, Any] = {"other_growth_gib_per_day": 0.0}
    absent = budget_mod.budget(sizes, {"u": ["AAA"]}, PROFILE)
    assert absent["u"]["status"] == "no_archives"
    assert budget_mod.headroom(absent, free_gib=5.0, reserve_gib=10.0, **kwargs)["u"] == {
        "status": "below_reserve",
        "usable_gib": -5.0,
        "fits": False,
    }
    unmeasured = budget_mod.budget(sizes, {"u": ["AAA", "NEVER"]}, PROFILE)
    assert unmeasured["u"]["status"] == "insufficient_data"
    assert unmeasured["u"]["bases_unmeasured"] == ["NEVER"]
    room = budget_mod.headroom(unmeasured, free_gib=50.0, reserve_gib=10.0, **kwargs)
    assert room["u"] == {"status": "insufficient_data", "fits": None}
    empty = budget_mod.budget(sizes, {"u": []}, PROFILE)
    assert (
        budget_mod.headroom(empty, free_gib=50.0, reserve_gib=10.0, **kwargs)["u"]["fits"] is None
    )


GROWTH_INPUTS: dict[str, Any] = {
    "measured_at": "2026-10-02T17:00:00+00:00",
    "window": {"start": "2026-09-15", "end": "2026-09-29"},
    "plain_tables": [{"table": "app.t", "bytes": 1400 * MIB, "rows": 1000, "rows_in_window": 500}],
    "hypertables": [
        {
            "hypertable": "ts.growing",
            "retention_drop_after": "180 days",
            "oldest_chunk_start": "2026-08-25T00:00:00+00:00",
            "chunks": [
                {
                    "start": "2026-09-14T00:00:00+00:00",
                    "end": "2026-09-15T00:00:00+00:00",
                    "bytes": 99 * MIB,
                },
                {
                    "start": "2026-09-15T00:00:00+00:00",
                    "end": "2026-09-16T00:00:00+00:00",
                    "bytes": 3 * MIB,
                },
                {
                    "start": "2026-09-16T00:00:00+00:00",
                    "end": "2026-09-17T00:00:00+00:00",
                    "bytes": 5 * MIB,
                },
                {
                    "start": "2026-09-28T00:00:00+00:00",
                    "end": "2026-09-29T00:00:00+00:00",
                    "bytes": 7 * MIB,
                },
                {
                    "start": "2026-09-29T00:00:00+00:00",
                    "end": "2026-09-30T00:00:00+00:00",
                    "bytes": 99 * MIB,
                },
            ],
        },
        {
            "hypertable": "ts.steady",
            "retention_drop_after": "45 days",
            "oldest_chunk_start": "2026-08-18T00:00:00+00:00",
            "chunks": [
                {
                    "start": "2026-09-20T00:00:00+00:00",
                    "end": "2026-09-21T00:00:00+00:00",
                    "bytes": 9 * MIB,
                }
            ],
        },
        {
            "hypertable": budget_mod.BARS_HYPERTABLE,
            "retention_drop_after": None,
            "oldest_chunk_start": "2026-09-16T00:00:00+00:00",
            "chunks": [
                {
                    "start": "2026-09-16T00:00:00+00:00",
                    "end": "2026-09-17T00:00:00+00:00",
                    "bytes": GIB,
                },
                {
                    "start": "2026-09-17T00:00:00+00:00",
                    "end": "2026-09-18T00:00:00+00:00",
                    "bytes": 2 * GIB,
                },
                {
                    "start": "2026-09-18T00:00:00+00:00",
                    "end": "2026-09-19T00:00:00+00:00",
                    "bytes": 4 * GIB,
                },
            ],
        },
    ],
}


def test_other_growth_from_metadata() -> None:
    growth = budget_mod.other_growth(GROWTH_INPUTS)
    assert growth["mib_per_day"] == {
        "app.t": pytest.approx(1.4 * 500 / 14),
        "ts.growing": pytest.approx(5.0),  # only the chunks wholly inside the window
        "ts.steady": 0.0,  # retention already drops its oldest chunks
    }
    assert growth["total_gib_per_day"] == pytest.approx((50.0 + 5.0) / 1024)


def test_bars_release_counts_chunks_past_the_cutoff() -> None:
    # Midnight 2026-10-02 minus 14 days: chunks ending by 2026-09-18 are past it.
    assert budget_mod.bars_release_gib(GROWTH_INPUTS, 14) == pytest.approx(3.0)
    assert budget_mod.bars_release_gib(GROWTH_INPUTS, 13) == pytest.approx(7.0)


def test_largest_and_universe_inputs(tmp_path: Path) -> None:
    sizes: dict[str, dict[str, int | None]] = {
        "A": {"202606": 5, "202607": 5},
        "B": {"202606": 9, "202607": 9},
        "C": {"202606": None, "202607": 99},
        "D": {"202606": None, "202607": None},
    }
    assert budget_mod.largest(sizes, ["A", "B", "C", "D"], 2) == ["C", "B"]
    for base in ("ANKR", "ARB"):
        (tmp_path / f"{base.lower()}-gate-bybit.json").write_text(json.dumps({"base": base}))
    (tmp_path / "x-gate-binance.json").write_text(json.dumps({"base": "X"}))
    assert budget_mod.registry_bases(tmp_path) == ["ANKR", "ARB"]
    universes = budget_mod.universes_from({"catalogue": {"universe": ["A", "B"]}}, ["ARB"])
    assert universes == {"registry_44": ["ARB"], "universe_592": ["A", "B"]}
    with pytest.raises(ValueError, match="no"):
        budget_mod.registry_bases(tmp_path / "missing")
    with pytest.raises(ValueError, match="empty universe"):
        budget_mod.universes_from({"catalogue": {"universe": []}}, ["ARB"])
