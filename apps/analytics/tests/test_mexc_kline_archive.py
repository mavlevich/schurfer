from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import mexc_kline_archive as a

if TYPE_CHECKING:
    from pathlib import Path

START = datetime(2026, 9, 1, tzinfo=UTC)


def test_the_blind_end_is_never_crossed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HYP-012 v2 blind"):
        asyncio.run(a.archive(tmp_path, "Min1", START, datetime(2026, 9, 29, 0, 1, tzinfo=UTC)))
    assert not any(tmp_path.iterdir())


def test_parallel_arrays_become_bars() -> None:
    data = {
        "time": [60, 120],
        "open": [1, 2],
        "high": [3, 4],
        "low": [0.5, 1],
        "close": [2, 3],
        "vol": [10, 20],
        "amount": [100, 200],
    }
    assert a.bars_from_payload(data) == [
        {"t": 60, "o": 1.0, "h": 3.0, "l": 0.5, "c": 2.0, "v": 10.0, "a": 100.0},
        {"t": 120, "o": 2.0, "h": 4.0, "l": 1.0, "c": 3.0, "v": 20.0, "a": 200.0},
    ]
    assert a.bars_from_payload(None) == []


def _mock(requests: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append({"path": request.url.path, **params})
        if request.url.path.endswith("/contract/detail"):
            data = [
                {"symbol": "AAA_USDT", "quoteCoin": "USDT", "settleCoin": "USDT"},
                {"symbol": "BBB_USDC", "quoteCoin": "USDC", "settleCoin": "USDC"},
            ]
            return httpx.Response(200, json={"success": True, "code": 0, "data": data})
        lo, hi = int(params["start"]), int(params["end"])
        times = list(range(lo, min(hi, lo + 3 * 60) + 1, 60))  # one extra bar at `end`
        n = len(times)
        bars: dict[str, list[Any]] = {"time": times}
        bars |= {k: [1.0] * n for k in ("open", "high", "low", "close", "vol", "amount")}
        return httpx.Response(200, json={"success": True, "code": 0, "data": bars})

    return httpx.MockTransport(handler)


def test_archive_writes_usdt_perps_once_with_a_manifest_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=_mock(requests), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(a, "_MIN_REQUEST_GAP_SECONDS", 0.0)
    monkeypatch.setattr(a, "_THROTTLE", a._Throttle(0.0))
    end = datetime.fromtimestamp(START.timestamp() + 180, UTC)
    summary = asyncio.run(a.archive(tmp_path, "Min1", START, end))
    assert summary == {"listed_now": 1, "complete": 1, "empty": 0, "failed": 0, "rows": 3}
    path = tmp_path / "Min1" / "AAA_USDT.jsonl.gz"
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    # start inclusive, end exclusive: the extra bar at `end` is dropped
    assert [r["t"] for r in rows] == [int(START.timestamp()) + 60 * k for k in range(3)]
    manifest = json.loads((tmp_path / "Min1" / "manifest.json").read_text())
    assert (
        manifest["symbols"]["AAA_USDT"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert "BBB_USDC" not in manifest["symbols"]
    kline_calls = sum(1 for r in requests if "/kline/" in r["path"])
    asyncio.run(a.archive(tmp_path, "Min1", START, end))  # resume: nothing refetched
    assert sum(1 for r in requests if "/kline/" in r["path"]) == kline_calls


def test_files_from_a_run_without_its_manifest_are_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=_mock(requests), **kw)
    )
    monkeypatch.setattr(a, "_THROTTLE", a._Throttle(0.0))
    target = tmp_path / "Min1"
    target.mkdir()
    inside = int(START.timestamp()) + 60
    a.write_once(target / "AAA_USDT.jsonl.gz", [{"t": inside, "o": 1, "h": 1, "l": 1, "c": 1}])
    end = datetime.fromtimestamp(START.timestamp() + 180, UTC)
    asyncio.run(a.archive(tmp_path, "Min1", START, end))
    assert not any("/kline/" in r["path"] for r in requests)
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["symbols"]["AAA_USDT"]["rows"] == 1
    assert manifest["symbols"]["AAA_USDT"]["status"] == "complete"


def test_an_orphan_file_outside_the_window_is_failed_and_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Min1"
    target.mkdir()
    a.write_once(target / "AAA_USDT.jsonl.gz", [{"t": 1}])
    with pytest.raises(a.ArchiveMismatchError, match="exists for a failed symbol"):
        _run(tmp_path, monkeypatch)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **k: real_client(transport=_mock(requests), **k)
    )
    monkeypatch.setattr(a, "_THROTTLE", a._Throttle(0.0))
    end = kw.get("end", datetime.fromtimestamp(START.timestamp() + 180, UTC))
    return asyncio.run(a.archive(tmp_path, "Min1", kw.get("start", START), end))


def test_a_run_with_another_window_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch)
    later = datetime.fromtimestamp(START.timestamp() + 3600, UTC)
    with pytest.raises(ValueError, match="pinned to"):
        _run(tmp_path, monkeypatch, end=later)


def test_a_file_that_differs_from_the_manifest_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch)
    path = tmp_path / "Min1" / "AAA_USDT.jsonl.gz"
    path.write_bytes(gzip.compress(b'{"t":1}\n'))
    with pytest.raises(a.ArchiveMismatchError, match="differ from the manifest"):
        _run(tmp_path, monkeypatch)


def test_write_once_never_overwrites(tmp_path: Path) -> None:
    a.write_once(tmp_path / "x.jsonl.gz", [{"t": 1}])
    with pytest.raises(FileExistsError):
        a.write_once(tmp_path / "x.jsonl.gz", [{"t": 2}])


@pytest.mark.parametrize(
    ("rows", "status"),
    [
        ([], "empty"),
        ([{"t": 100}, {"t": 160}], "complete"),
        ([{"t": 100}, {"t": 400}], "failed"),  # a bar at or after the window end
    ],
)
def test_entry_status(rows: list[dict[str, Any]], status: str) -> None:
    assert a.entry_for(rows, "x", 100, 400)["status"] == status


def test_a_failed_symbol_is_recorded_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(a, "fetch_symbol", boom)
    summary = _run(tmp_path, monkeypatch)
    assert summary["failed"] == 1
    manifest = json.loads((tmp_path / "Min1" / "manifest.json").read_text())
    assert manifest["symbols"]["AAA_USDT"]["status"] == "failed"
    assert manifest["failures"] == ["AAA_USDT"]
    monkeypatch.undo()
    assert _run(tmp_path, monkeypatch)["complete"] == 1  # retried on the next run
