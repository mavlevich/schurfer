from __future__ import annotations

import gzip
import json
import math
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import gate_collection_budget as budget_mod

if TYPE_CHECKING:
    from pathlib import Path

GIB = budget_mod.GIB


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
        sizes = budget_mod.measure_sizes(client, ["AAA", "MISSING", "RETRY"], sleep=sleeps.append)
    assert sizes["AAA"] == {"202606": 1000, "202607": 1000}
    assert sizes["MISSING"] == {"202606": None, "202607": None}
    assert sizes["RETRY"]["202606"] == 7 and sleeps == [1.0]
    assert {method for method, _ in seen} == {"HEAD"}


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
    path = tmp_path / "AAA_USDT-202607.csv.gz"
    path.write_bytes(gzip.compress((rows + "\n").encode()))
    profile = budget_mod.conversion_profile([path])
    (row,) = profile["files"]
    assert row["csv_bytes"] == len(rows) + 1
    assert profile["csv_per_gz"] > 1 and profile["parquet_per_gz"] > 0
    assert profile["seconds_per_gib_gz"] > 0


PROFILE = {"csv_per_gz": 5.0, "parquet_per_gz": 0.8, "seconds_per_gib_gz": 60.0}


def test_budget_arithmetic_and_headroom() -> None:
    sizes: dict[str, dict[str, int | None]] = {
        "A": {"202606": GIB, "202607": 3 * GIB},  # mean 2 GiB
        "B": {"202606": GIB, "202607": GIB},  # mean 1 GiB
        "C": {"202606": None, "202607": GIB},  # missing a month: excluded
    }
    rows = budget_mod.budget(sizes, {"all": ["A", "B", "C"]}, PROFILE)
    row = rows["all"]
    assert row["bases_present_both_months"] == 2
    assert row["monthly_gib"]["raw_gz"] == pytest.approx(3.0)
    assert row["monthly_gib"]["parquet"] == pytest.approx(2.4)
    assert row["cumulative_gib"]["6m"] == pytest.approx(5.4 * 6)
    assert row["scratch_gib"]["gunzip_first"] == pytest.approx(10.0)
    assert row["scratch_gib"]["streaming_conversion"] == pytest.approx(1.6)
    assert row["conversion_cpu_minutes_per_month"] == pytest.approx(3.0)
    room = budget_mod.headroom(rows, free_gib=20.0, reserve_gib=10.0)["all"]
    assert room["usable_gib"] == 10.0
    assert room["months_until_full"] == pytest.approx(10.0 / 5.4)
    assert room["fits_6m"] is False
    empty = budget_mod.budget(sizes, {"none": ["C"]}, PROFILE)
    assert budget_mod.headroom(empty, free_gib=5.0, reserve_gib=10.0)["none"] == {
        "usable_gib": 0.0,
        "months_until_full": math.inf,
        "fits_6m": True,
    }


def test_largest_and_universe_inputs(tmp_path: Path) -> None:
    sizes: dict[str, dict[str, int | None]] = {
        "A": {"202606": 5, "202607": 5},
        "B": {"202606": 9, "202607": 9},
        "C": {"202606": None, "202607": 99},
    }
    assert budget_mod.largest(sizes, ["A", "B", "C"], 1) == ["B"]
    for base in ("ANKR", "ARB"):
        (tmp_path / f"{base.lower()}-gate-bybit.json").write_text(json.dumps({"base": base}))
    (tmp_path / "x-gate-binance.json").write_text(json.dumps({"base": "X"}))
    assert budget_mod.registry_bases(tmp_path) == ["ANKR", "ARB"]
    universes = budget_mod.universes_from({"catalogue": {"universe": ["A", "B"]}}, ["ARB"])
    assert universes == {"registry_44": ["ARB"], "universe_592": ["A", "B"]}
