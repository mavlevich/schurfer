from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from schurfer_analytics import disk_growth_budget as dgb
from schurfer_analytics.gate_collection_budget import BARS_HYPERTABLE, GIB, MIB

GATE_BUDGET = (
    Path(__file__).resolve().parents[3]
    / "docs/research/evidence/gate-collection-budget-v1/gate-collection-budget.json"
)
START = datetime(2026, 10, 4, 12, tzinfo=UTC)


def _chunks(taken: datetime, days: int, size: int) -> list[dict[str, Any]]:
    """One chunk per UTC day, the newest covering the reading's own day."""
    end = taken.replace(hour=0) + timedelta(days=1)
    return [
        {
            "chunk": f"c{i}",
            "range_start": (end - timedelta(days=i + 1)).isoformat(),
            "range_end": (end - timedelta(days=i)).isoformat(),
            "compressed": i > 0,
            "bytes": size,
        }
        for i in range(days)
    ]


def _reading(
    day: float,
    *,
    db_other: int,
    runtime: int = GIB,
    rest: int = 5 * GIB,
    bar_days: int = 14,
    bar_size: int = GIB // 2,
    build_cache: str = "1.0GB",
    tables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    taken = START + timedelta(days=day)
    chunks = _chunks(taken, bar_days, bar_size)
    bars = sum(c["bytes"] for c in chunks)
    docker = [
        {"Type": "Images", "Size": "3.0GB"},
        {"Type": "Containers", "Size": "200MB"},
        {"Type": "Local Volumes", "Size": "35GB"},
        {"Type": "Build Cache", "Size": build_cache},
    ]
    docker_total = sum(dgb.docker_bytes(r["Size"]) for r in docker if r["Type"] != "Local Volumes")
    used = bars + db_other + runtime + docker_total + rest
    total = 80 * GIB
    return {
        "version": dgb.READING_VERSION,
        "taken_at": taken.isoformat(),
        "filesystem": {"total": total, "used": used, "free": total - used},
        "database": {
            "database_bytes": bars + db_other,
            "tables": tables or [],
            "hypertables": [{"hypertable": BARS_HYPERTABLE, "chunks": chunks}],
        },
        "docker": docker,
        "runtime_dirs": {"sizes": {"research": runtime}, "unreadable": []},
    }


def _write(directory: Path, reading: dict[str, Any], *, sidecar: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(reading["taken_at"]).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"reading-{stamp}.json"
    body = json.dumps(reading).encode()
    path.write_bytes(body)
    (directory / f"{path.name}.sha256").write_text(
        (sidecar or hashlib.sha256(body).hexdigest()) + "\n"
    )
    return path


def _series(
    directory: Path, days: int, *, mib_per_day: float, rest_mib_per_day: float = 0.0
) -> list[dict[str, Any]]:
    for day in range(days + 1):
        _write(
            directory,
            _reading(
                day,
                db_other=20 * GIB + round(day * mib_per_day * MIB),
                rest=5 * GIB + round(day * rest_mib_per_day * MIB),
            ),
        )
    return dgb.load_series(directory)


def _gate() -> dict[str, Any]:
    return dgb.load_gate_budget(GATE_BUDGET)[0]


def test_docker_sizes_are_decimal_units() -> None:
    assert dgb.docker_bytes("3.901GB") == 3_901_000_000
    assert dgb.docker_bytes("52.54MB (24%)") == 52_540_000
    assert dgb.docker_bytes("0B") == 0
    assert dgb.docker_bytes("1.5kB") == 1_500
    with pytest.raises(dgb.SeriesError):
        dgb.docker_bytes("n/a")


def test_a_reading_without_its_hash_stops_the_series(tmp_path: Path) -> None:
    path = _write(tmp_path, _reading(0, db_other=GIB))
    _write(tmp_path, _reading(1, db_other=GIB))
    path.with_name(path.name + ".sha256").unlink()
    with pytest.raises(dgb.SeriesError, match=r"no \.sha256"):
        dgb.load_series(tmp_path)


def test_a_reading_that_does_not_match_its_hash_stops_the_series(tmp_path: Path) -> None:
    _write(tmp_path, _reading(0, db_other=GIB), sidecar="0" * 64)
    with pytest.raises(dgb.SeriesError, match="does not match"):
        dgb.load_series(tmp_path)


def test_another_reading_version_stops_the_series(tmp_path: Path) -> None:
    reading = _reading(0, db_other=GIB)
    reading["version"] = "disk_growth_reading_v0"
    _write(tmp_path, reading)
    with pytest.raises(dgb.SeriesError, match="not 'disk_growth_reading_v1'"):
        dgb.load_series(tmp_path)


def test_an_empty_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(dgb.SeriesError, match="no reading"):
        dgb.load_series(tmp_path)


def test_parts_add_up_to_the_filesystem() -> None:
    reading = _reading(0, db_other=20 * GIB, runtime=GIB, rest=5 * GIB)
    parts = dgb.parts(reading)
    assert parts["bars"] == 7 * GIB
    assert parts["database_outside_bars"] == 20 * GIB
    assert parts["rest"] == 5 * GIB
    assert sum(parts[name] for name in dgb.PARTS) == parts["used_outside_bars"]
    assert parts["used_outside_bars"] + parts["bars"] == parts["filesystem_used"]


def test_steady_growth_is_a_slope(tmp_path: Path) -> None:
    growth = dgb.rates(_series(tmp_path, 7, mib_per_day=100))
    assert growth["parts_mib_per_day"]["database_outside_bars"] == pytest.approx(100, abs=0.01)
    assert growth["central_mib_per_day"] == pytest.approx(100, abs=0.01)
    assert growth["conservative_mib_per_day"] == pytest.approx(100, abs=0.01)
    assert growth["steps"] == []


def test_one_download_is_a_step_not_a_daily_rate(tmp_path: Path) -> None:
    for day in range(8):
        runtime = GIB + (2 * GIB if day >= 1 else 0)
        _write(tmp_path, _reading(day, db_other=20 * GIB, runtime=runtime))
    growth = dgb.rates(dgb.load_series(tmp_path))
    assert growth["parts_mib_per_day"]["runtime_readable"] == 0
    assert growth["steps"] == [
        {
            "part": "runtime_readable",
            "before": "reading-20261004T120000Z.json",
            "after": "reading-20261005T120000Z.json",
            "mib": pytest.approx(2048),
        }
    ]


def test_a_pruned_build_cache_is_not_growth_and_not_a_saving(tmp_path: Path) -> None:
    for day, cache in enumerate(["3.0GB", "0.4GB", "1.8GB", "0.4GB", "3.0GB", "1.0GB", "2.0GB"]):
        _write(tmp_path, _reading(day, db_other=20 * GIB, build_cache=cache))
    growth = dgb.rates(dgb.load_series(tmp_path))
    assert growth["central_mib_per_day"] == pytest.approx(0, abs=0.01)
    cache = growth["reserve_parts_gib"]["docker_build_cache"]
    assert cache["min"] == pytest.approx(0.4e9 / GIB)
    assert cache["max"] == pytest.approx(3.0e9 / GIB)


def test_a_shrinking_part_offsets_only_the_central_rate(tmp_path: Path) -> None:
    growth = dgb.rates(_series(tmp_path, 7, mib_per_day=100, rest_mib_per_day=-60))
    assert growth["central_mib_per_day"] == pytest.approx(40, abs=0.01)
    assert growth["conservative_mib_per_day"] == pytest.approx(100, abs=0.01)


def test_bars_past_the_cutoff_are_due_for_release() -> None:
    release = dgb.bars_release(_reading(0, db_other=GIB, bar_days=21), cutoff_days=14)
    assert release["chunks"] == 21
    assert release["chunks_due"] == 6
    assert release["due_gib"] == pytest.approx(3.0)


def test_only_the_committed_gate_budget_is_accepted(tmp_path: Path) -> None:
    budget, digest = dgb.load_gate_budget(GATE_BUDGET)
    assert digest == dgb.GATE_BUDGET_SHA256
    assert set(budget["budget"]) == {"registry_44", "universe_592", "universe_without_top10"}
    other = tmp_path / "budget.json"
    other.write_text(json.dumps({**budget, "reserve_gib": 0}))
    with pytest.raises(dgb.SeriesError, match="committed budget"):
        dgb.load_gate_budget(other)


def test_less_than_a_week_gives_no_verdict(tmp_path: Path) -> None:
    result = dgb.analyse(_series(tmp_path, 6, mib_per_day=10), _gate(), cutoff_days=14)
    assert {v["verdict"] for v in result["verdicts"].values()} == {"undetermined"}


def test_registry_verdict_follows_the_conservative_rate(tmp_path: Path) -> None:
    # 80 GiB disk, 21.7 GiB used: 58.3 GiB free, 48.3 above the reserve.
    small = dgb.analyse(_series(tmp_path / "a", 7, mib_per_day=10), _gate(), cutoff_days=14)
    assert small["verdicts"]["registry_44"]["verdict"] == "fits"
    assert small["verdicts"]["universe_592"]["verdict"] == "fits"
    offset = dgb.analyse(
        _series(tmp_path / "b", 7, mib_per_day=300, rest_mib_per_day=-200),
        _gate(),
        cutoff_days=14,
    )
    assert offset["verdicts"]["registry_44"]["verdict"] == "fits_central_only"
    large = dgb.analyse(_series(tmp_path / "c", 7, mib_per_day=400), _gate(), cutoff_days=14)
    assert large["verdicts"]["registry_44"]["verdict"] == "does_not_fit"
    assert large["no_collection_days_to_reserve_conservative"] < 200


def test_relation_counters_reset_by_a_restart_are_not_rates(tmp_path: Path) -> None:
    def table(ins: int, size: int) -> list[dict[str, Any]]:
        return [
            {
                "table": "app.t",
                "total_bytes": size,
                "n_tup_ins": ins,
                "n_tup_upd": 0,
                "n_tup_del": 0,
                "n_dead_tup": 0,
            }
        ]

    _write(tmp_path, _reading(0, db_other=GIB, tables=table(1000, 100 * MIB)))
    _write(tmp_path, _reading(2, db_other=GIB, tables=table(10, 100 * MIB)))
    (row,) = dgb.relations(dgb.load_series(tmp_path))
    assert row["n_tup_ins_per_day"] is None
    assert row["mib_per_day"] == 0


def test_main_writes_the_analysis_once(tmp_path: Path) -> None:
    _series(tmp_path / "readings", 7, mib_per_day=50)
    argv = [
        "--readings",
        str(tmp_path / "readings"),
        "--gate-budget",
        str(GATE_BUDGET),
        "--bars-cutoff-days",
        "14",
        "--code-revision",
        "abc123",
        "--no-working-tree-dirty",
        "--out-dir",
        str(tmp_path / "out"),
    ]
    assert dgb.main(argv) == 0
    (path,) = (tmp_path / "out").glob("disk-growth-budget-*.json")
    body = path.read_bytes()
    assert path.with_name(path.name + ".sha256").read_text().strip() == (
        hashlib.sha256(body).hexdigest()
    )
    record = json.loads(body)
    assert record["code_revision"] == "abc123"
    assert record["working_tree_dirty"] is False
    assert record["inputs"]["gate_budget_sha256"] == dgb.GATE_BUDGET_SHA256
    assert len(record["inputs"]["readings"]) == 8
    with pytest.raises(FileExistsError):
        dgb.main(argv)


def test_main_refuses_a_bad_series_before_writing(tmp_path: Path) -> None:
    _write(tmp_path / "readings", _reading(0, db_other=GIB), sidecar="0" * 64)
    argv = [
        "--readings",
        str(tmp_path / "readings"),
        "--gate-budget",
        str(GATE_BUDGET),
        "--bars-cutoff-days",
        "14",
        "--code-revision",
        "abc123",
        "--no-working-tree-dirty",
        "--out-dir",
        str(tmp_path / "out"),
    ]
    assert dgb.main(argv) == 1
    assert not (tmp_path / "out").exists()


def test_the_what_if_removes_only_the_pump_short_tables(tmp_path: Path) -> None:
    def table(name: str, size: int) -> dict[str, Any]:
        return {"table": name, "total_bytes": size, "n_tup_ins": 0, "n_tup_upd": 0,
                "n_tup_del": 0, "n_dead_tup": 0}  # fmt: skip

    for day in range(8):
        tables = [
            table("app.trade_decisions", GIB + day * 30 * MIB),
            table("app.trade_decision_outcomes", GIB + day * 20 * MIB),
            table("app.other", GIB + day * 50 * MIB),
        ]
        _write(tmp_path, _reading(day, db_other=20 * GIB + day * 100 * MIB, tables=tables))
    result = dgb.analyse(dgb.load_series(tmp_path), _gate(), cutoff_days=14)
    what_if = result["what_if_without_pump_short"]
    assert what_if["removed_mib_per_day"] == pytest.approx(50, abs=0.01)
    assert what_if["conservative_mib_per_day"] == pytest.approx(50, abs=0.01)
    assert result["growth"]["conservative_mib_per_day"] == pytest.approx(100, abs=0.01)
    assert set(what_if["verdicts"]) == set(result["verdicts"])
