from __future__ import annotations

import gzip
import json
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_compressed_spring_tape as tape
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    HOUR_MS,
    MINUTE_MS,
    VERSION,
    WINDOW_FIRST_MS,
    ArchiveBlockedError,
)
from schurfer_analytics.bybit_compressed_spring_tape import (
    Task,
    aggregate,
    boundary_of,
    build_tasks,
    download,
    mask_gate,
    pilot_days,
    reconcile,
    run_task,
    seal,
)
from schurfer_analytics.bybit_post_funding_screen import SnapshotWriter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

DAY = "2024-11-05"
D0 = tape._day_ms(DAY)
HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional"
)


def row(ms: int, side: str, size: float, price: float = 2.0, symbol: str = "AUSDT") -> str:
    seconds, millis = divmod(ms, 1000)
    stamp = f"{seconds}.{millis:03d}0"
    return f"{stamp},{symbol},{side},{size},{price},PlusTick,id,0,{size},{size * price}"


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_bytes(gzip.compress(("\n".join([HEADER, *rows]) + "\n").encode()))
    return path


def test_boundary_of_masks_minute_55_through_04() -> None:
    h = D0 + 5 * HOUR_MS
    assert boundary_of(h - 5 * MINUTE_MS) == h
    assert boundary_of(h - 6 * MINUTE_MS) is None
    assert boundary_of(h) == h
    assert boundary_of(h + 4 * MINUTE_MS) == h
    assert boundary_of(h + 5 * MINUTE_MS) is None


def test_mask_gate_needs_whole_hours_and_unions_event_hours() -> None:
    own = {("A", D0): -0.004, ("A", D0 + HOUR_MS): 0.0001}
    theirs = {("B", D0 + 2 * HOUR_MS): -0.003, ("B", D0): -0.001}
    gate = mask_gate(own, theirs)
    assert gate.event_hours == {D0, D0 + 2 * HOUR_MS}
    assert (gate.own_event_hours, gate.hyp031_event_hours) == (1, 1)
    with pytest.raises(SystemExit, match="whole hour"):
        mask_gate(own, {("B", D0 + 1): -0.01})


def test_aggregate_keeps_s_minutes_sets_aside_unreserved_and_counts_reserved(
    tmp_path: Path,
) -> None:
    event = D0 + 3 * HOUR_MS
    calm = D0 + 7 * HOUR_MS
    rows = [
        row(D0 + 30 * MINUTE_MS + 1500, "Buy", 10),  # kept, 20 USD
        row(D0 + 30 * MINUTE_MS + 2500, "Sell", 4),  # kept, 8 USD
        row(D0 + 31 * MINUTE_MS, "Sell", 1),  # kept
        row(event - 2 * MINUTE_MS, "Sell", 1000),  # reserved: event boundary
        row(event + 4 * MINUTE_MS, "Buy", 1000),  # reserved
        row(calm - MINUTE_MS, "Buy", 3),  # unreserved masked minute
        row(D0 - MINUTE_MS, "Buy", 1),  # previous day: bad
        row(D0 + 40 * MINUTE_MS, "Buy", 1, symbol="BUSDT"),  # wrong symbol: bad
    ]
    out = aggregate(write_csv(tmp_path / "a.csv.gz", rows), "AUSDT", DAY, [event])
    s = out["s_minutes"]
    assert s[str(D0 + 30 * MINUTE_MS)] == [20.0, 8.0, 10.0, 4.0, 2]
    assert s[str(D0 + 31 * MINUTE_MS)] == [0.0, 2.0, 0.0, 1.0, 1]
    assert out["sensitivity_minutes"] == {str(calm - MINUTE_MS): [6.0, 0.0, 3.0, 0.0, 1]}
    assert out["counts"] == {"rows": 4, "reserved_rows": 2, "bad_rows": 2}
    assert "1000" not in json.dumps(out)  # nothing of the reserved minutes leaves the query


def test_aggregate_is_exact_and_repeatable(tmp_path: Path) -> None:
    rows = [row(D0 + 30 * MINUTE_MS + k, "Buy", 0.1) for k in range(1000)]
    rows.append(row(D0 + 30 * MINUTE_MS, "Sell", 1).rsplit(",", 1)[0] + ",0.1234567890123456")
    path = write_csv(tmp_path / "x.csv.gz", rows)
    first = aggregate(path, "AUSDT", DAY, [])
    assert first == aggregate(path, "AUSDT", DAY, [])
    cell = first["s_minutes"][str(D0 + 30 * MINUTE_MS)]
    assert cell[2] == 100.0  # 1000 x 0.1 exactly, not 99.9999999999986
    assert cell[1] == pytest.approx(0.123456789012, abs=1e-15)  # beyond 12 decimals: cut
    assert first["counts"] == {"rows": 1001}


def test_parts_are_byte_reproducible(tmp_path: Path) -> None:
    part = {"status": "ok", "s_minutes": {"1": [1.0, 2.0, 3.0, 4.0, 5]}}
    tape.write_part(tmp_path / "a.json.gz", part)
    first = (tmp_path / "a.json.gz").read_bytes()
    time.sleep(1.1)
    tape.write_part(tmp_path / "a.json.gz", part)
    assert (tmp_path / "a.json.gz").read_bytes() == first


def test_a_bad_row_in_a_reserved_minute_still_marks_the_day(tmp_path: Path) -> None:
    event = D0 + 3 * HOUR_MS
    broken = row(event + MINUTE_MS, "Buy", 1).replace(",Buy,", ",,")
    out = aggregate(write_csv(tmp_path / "r.csv.gz", [broken]), "AUSDT", DAY, [event])
    assert out["counts"] == {"bad_rows": 1}


def test_aggregate_rejects_empty_side_and_non_finite_numbers(tmp_path: Path) -> None:
    good = row(D0 + 30 * MINUTE_MS, "Buy", 1)
    for broken in (
        good.replace(",Buy,", ",,"),
        good.replace(",AUSDT,", ",,"),
        good.rsplit(",", 1)[0] + ",inf",
        good.replace(",1,2.0,", ",nan,2.0,"),
    ):
        path = write_csv(tmp_path / "b.csv.gz", [good, broken])
        out = aggregate(path, "AUSDT", DAY, [])
        assert out["counts"] == {"rows": 1, "bad_rows": 1}, broken


def _aggregated(volume_by_hour: dict[int, float], events: list[int]) -> dict[str, Any]:
    s = {str(h + 30 * MINUTE_MS): [0.0, 0.0, v, 0.0, 1] for h, v in volume_by_hour.items()}
    return {"s_minutes": s, "sensitivity_minutes": {}, "event_hours": events}


def test_reconcile_gives_every_hour_a_status() -> None:
    tape_hours = {D0 + k * HOUR_MS: 1000.0 for k in range(24)}
    klines = {h: (v, 0.1) for h, v in tape_hours.items()}
    del klines[D0 + 20 * HOUR_MS]
    agg = _aggregated(tape_hours, [D0 + 5 * HOUR_MS])
    out = reconcile(DAY, agg, klines)
    assert out["counts"] == {"matched": 21, "unchecked_reserved": 2, "missing_kline": 1}
    assert out["hours"][str(D0 + 4 * HOUR_MS)] == "unchecked_reserved"
    assert out["hours"][str(D0 + 20 * HOUR_MS)] == "missing_kline"
    assert out["gap_bins"] == {"<=0.01%": 21}
    off = dict(klines)
    off[D0 + 9 * HOUR_MS] = (1001.2, 0.1)  # 1.2 apart: above 0.1% (1.0) + one unit (0.1)
    out = reconcile(DAY, agg, off)
    assert out["hours"][str(D0 + 9 * HOUR_MS)] == "mismatch"
    assert out["counts"]["mismatch"] == 1
    assert out["gap_bins"]["<=0.5%"] == 1
    near = dict(klines)
    near[D0 + 9 * HOUR_MS] = (1001.0, 0.01)  # 1.0 apart: within 1.001 + 0.01
    assert reconcile(DAY, agg, near)["counts"].get("mismatch") is None


def test_large_trades_do_not_widen_the_tolerance() -> None:
    agg = _aggregated({D0: 100.0}, [])
    out = reconcile(DAY, agg, {D0: (199.0, 1.0)})
    assert out["hours"][str(D0)] == "mismatch"


def test_kline_volume_unit_follows_the_published_decimals() -> None:
    assert tape._unit("123.45") == pytest.approx(0.01)
    assert tape._unit("120") == 1.0


def _transport(statuses: list[int], body: bytes = b"") -> httpx.MockTransport:
    answers = iter(statuses)

    def handle(_request: httpx.Request) -> httpx.Response:
        return _streamed(next(answers), body)

    return httpx.MockTransport(handle)


def _streamed(status: int, body: bytes) -> httpx.Response:
    """A response read from the wire, as the archive's are (iter_raw works on it)."""
    headers = {"content-length": str(len(body))}
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def test_download_checks_gzip_and_handles_404_and_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(tape, "_sleep", slept.append)
    body = gzip.compress(b"x" * 5000)
    target = tmp_path / "f"
    with httpx.Client(transport=_transport([403, 200], body)) as client:
        size, digest = download(client, "https://x/f", target) or (0, "")
    assert size == len(body) and len(digest) == 64 and slept == [30.0]
    with httpx.Client(transport=_transport([404])) as client:
        assert download(client, "https://x/f", target) is None
    cut = _transport([200], body[:-6])
    with httpx.Client(transport=cut) as client, pytest.raises(ValueError, match="truncated"):
        download(client, "https://x/f", target)
    blocked = _transport([403] * 5)
    with httpx.Client(transport=blocked) as client, pytest.raises(ArchiveBlockedError):
        download(client, "https://x/f", target)


def test_run_task_writes_one_part_and_removes_the_download(tmp_path: Path) -> None:
    rows = [row(D0 + k * HOUR_MS + 30 * MINUTE_MS, "Buy", 5) for k in range(24)]
    body = write_csv(tmp_path / "src.csv.gz", rows).read_bytes()
    volumes = {D0 + k * HOUR_MS: (5.0, 1.0) for k in range(24)}
    task = Task("AUSDT", DAY, tmp_path / "out" / "AUSDT" / f"{DAY}.json.gz", volumes, ())
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    assert run_task(task, tmp_dir, _transport([200], body)) == "ok"
    part = json.loads(gzip.decompress(task.part.read_bytes()))
    assert part["reconciliation"]["counts"] == {"matched": 24}
    assert part["file_bytes"] == len(body)
    assert list(tmp_dir.iterdir()) == []
    missing = Task("AUSDT", "2024-11-06", tmp_path / "out" / "AUSDT" / "m.json.gz", {}, ())
    assert run_task(missing, tmp_dir, _transport([404])) == "missing"
    wrong = dict(volumes)
    wrong[D0] = (50.0, 1.0)
    task2 = Task("AUSDT", DAY, tmp_path / "out" / "AUSDT" / "w.json.gz", wrong, ())
    assert run_task(task2, tmp_dir, _transport([200], body)) == "ok"  # one hour, not the day
    hours = json.loads(gzip.decompress(task2.part.read_bytes()))["reconciliation"]["hours"]
    assert hours[str(D0)] == "mismatch" and hours[str(D0 + HOUR_MS)] == "matched"


def test_pilot_days_are_fixed() -> None:
    scope = {"A": [f"2024-11-{d:02d}" for d in range(1, 31)], "B": ["2024-11-01"]}
    first = pilot_days(scope, 5)
    assert len(first) == 5 and first == pilot_days(dict(reversed(scope.items())), 5)


BINDING = {"version": VERSION, **tape.RULES, "scope_sha256": "s", "pilot": 0}


def test_fetch_tape_skips_existing_parts_and_seal_checks_the_binding(tmp_path: Path) -> None:
    out = tmp_path / "run"
    volumes = {"AUSDT": {D0: (1.0, 1.0)}}
    tasks = build_tasks([("AUSDT", DAY)], out, volumes, frozenset({D0 + DAY_MS}))
    assert tasks[0].kline_volume == {D0: (1.0, 1.0)}
    assert tasks[0].event_hours == (D0 + DAY_MS,)
    with pytest.raises(SystemExit, match=r"no run\.json"):
        seal(tasks, out, BINDING)
    tape.bind_run(out, BINDING, create=True)
    with pytest.raises(SystemExit, match="no part yet"):
        seal(tasks, out, BINDING)
    rec = {"counts": {"matched": 20, "mismatch": 1, "unchecked_reserved": 3},
           "gap_bins": {"<=0.01%": 20, "<=0.5%": 1}}  # fmt: skip
    part = {"symbol": "AUSDT", "day": DAY, "status": "ok", "file_bytes": 7,
            "counts": {"rows": 3}, "reconciliation": rec}  # fmt: skip
    tape.write_part(tasks[0].part, part)
    summary = tape.fetch_tape(tasks, out, max_bytes=10**9, workers=1)
    assert summary["files_done_before"] == 1 and summary["statuses_this_run"] == {}
    with pytest.raises(SystemExit, match="other inputs"):
        seal(tasks, out, BINDING | {"hyp031_index_sha256": "new"})
    manifest, digest = seal(tasks, out, BINDING)
    assert manifest["statuses"] == {"ok": 1} and manifest["file_bytes"] == 7
    assert manifest["matched_share_of_ok_hours"] == round(20 / 24, 4)
    assert manifest["matched_share_of_checked_hours"] == round(20 / 21, 4)
    assert manifest["gap_bins_in_ok_days"]["<=0.5%"] == 1
    assert len(digest) == 64


def test_bind_run_refuses_parts_without_a_binding(tmp_path: Path) -> None:
    out = tmp_path / "run"
    tape.write_part(out / "parts" / "AUSDT" / f"{DAY}.json.gz", {"status": "ok"})
    with pytest.raises(SystemExit, match=r"parts but no run\.json"):
        tape.bind_run(out, BINDING, create=True)


class _BrokenStream(httpx.SyncByteStream):
    def __init__(self, body: bytes, cut: int) -> None:
        self.body, self.cut = body, cut

    def __iter__(self) -> Iterator[bytes]:
        yield self.body[: self.cut]
        raise httpx.ReadError("connection reset")


def test_budget_reserves_whole_attempts_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multiprocessing

    monkeypatch.setattr(tape, "_sleep", lambda _s: None)
    body = gzip.compress(bytes(range(256)) * 50)
    ledger = tmp_path / "ledger.jsonl"
    value = multiprocessing.get_context("spawn").Value("q", 0)
    budget = tape.Budget(10 * len(body), value, ledger)
    answers = iter(
        [
            httpx.Response(
                200,
                headers={"content-length": str(len(body))},
                stream=_BrokenStream(body, 2048),
            ),
            _streamed(200, body),
        ]
    )
    transport = httpx.MockTransport(lambda _r: next(answers))
    with httpx.Client(transport=transport) as client:
        size, _ = download(client, "https://x/f", tmp_path / "a", budget) or (0, "")
    assert size == len(body)
    assert value.value == 2 * len(body)  # the broken attempt counts in full
    assert tape.ledger_spent(ledger) == 2 * len(body)  # and survives a crash
    small = tape.Budget(len(body) - 1, multiprocessing.get_context("spawn").Value("q", 0), ledger)
    with (
        httpx.Client(transport=_transport([200], body)) as client,
        pytest.raises(tape.BudgetExceededError),
    ):
        download(client, "https://x/f", tmp_path / "b", small)
    assert not (tmp_path / "b").exists()  # refused before reading


def test_main_refuses_another_hyp031_index(tmp_path: Path) -> None:
    argv = ["seal", "--scope", "s", "--scope-sha256", "x", "--features", "f",
            "--features-sha256", "y", "--hyp031-index", "i", "--hyp031-index-sha256", "0" * 64,
            "--out-dir", str(tmp_path)]  # fmt: skip
    with pytest.raises(SystemExit, match="pinned"):
        tape.main(argv)


def test_settlement_rates_reads_funding_lines(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl.gz"
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": VERSION})
    writer.record("funding", "AUSDT", {}, {"result": {"list": [
        {"symbol": "AUSDT", "fundingRateTimestamp": str(WINDOW_FIRST_MS), "fundingRate": "-0.005"}
    ]}})  # fmt: skip
    writer.record("fetch_error", "AUSDT", {}, {"error": "oi failed"})
    writer.close()
    assert tape.settlement_rates(path) == {("AUSDT", WINDOW_FIRST_MS): -0.005}
