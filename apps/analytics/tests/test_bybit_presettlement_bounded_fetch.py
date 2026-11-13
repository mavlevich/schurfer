from __future__ import annotations

import base64
import gzip
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_presettlement_bounded_fetch as f
from schurfer_analytics import bybit_presettlement_feasibility as b1
from schurfer_analytics.bybit_post_funding_screen import MINUTE_MS

if TYPE_CHECKING:
    from collections.abc import Iterator


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def now(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.value += delay


def requests(n: int = 3) -> list[f.Request]:
    return [f.Request(f"TEST{i}USDT", 1_730_000_040_000, 1_730_000_160_000) for i in range(n)]


def payload(r: f.Request, **updates: Any) -> bytes:
    rows = [
        [str(ts), "-0.002", "-0.001", "-0.003", "-0.002"] for ts in range(r.start, r.end, MINUTE_MS)
    ]
    result = {"symbol": r.symbol, "category": "linear", "list": rows, **updates}
    return json.dumps({"retCode": 0, "result": result}).encode()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, dict[str, Any], list[f.Request], Clock]:
    rs = requests()
    contract = {"rules": f.RULES, "limits": asdict(f.Limits())}
    root = tmp_path / "run"
    f.prepare(root, contract, rs, [rs[0].key])
    return root, contract, rs, Clock()


def client_for(
    rs: list[f.Request],
    calls: list[str],
    clock: Clock | None = None,
    starts: list[float] | None = None,
) -> httpx.Client:
    by_symbol = {r.symbol: r for r in rs}

    def handle(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        calls.append(symbol)
        if starts is not None and clock is not None:
            starts.append(clock.now())
        # A real unconsumed byte stream, not httpx.Response(content=...)'s preloaded body.
        return httpx.Response(200, stream=httpx.ByteStream(payload(by_symbol[symbol])))

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_offline_prepare_is_atomic_and_never_reinitializes(setup: Any) -> None:
    root, contract, rs, _ = setup
    before = (root / "run.json").read_bytes()
    with pytest.raises(FileExistsError):
        f.prepare(root, contract, rs, [rs[0].key])
    assert (root / "run.json").read_bytes() == before
    assert not list(root.parent.glob(".run.prepare-*"))
    (root / "ledger.sqlite").unlink()
    with pytest.raises(ValueError, match="missing"):
        f.Run(root, contract)


def test_binding_and_single_writer_are_enforced(setup: Any) -> None:
    root, contract, _, _ = setup
    with pytest.raises(ValueError, match="binding differs"):
        f.Run(root, {**contract, "rules": "changed"})
    with f.Run(root, contract), pytest.raises(BlockingIOError):
        f.Run(root, contract)


def test_fixed_pilot_reused_restart_rate_spacing_and_deterministic_seal(setup: Any) -> None:
    root, contract, rs, clock = setup
    calls: list[str] = []
    starts: list[float] = []
    with (
        client_for(rs, calls, clock, starts) as client,
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
    ):
        with pytest.raises(ValueError, match="pilot must"):
            run.fetch("full", client)
        assert run.fetch("pilot", client)["passed"]
    with (
        client_for(rs, calls, clock, starts) as client,
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
    ):
        assert run.fetch("full", client)["complete"] == 3
        one = run.seal()
        raw = (root / "supplement.jsonl.gz").read_bytes()
        assert run.seal() == one
        assert (root / "supplement.jsonl.gz").read_bytes() == raw
        assert run.totals()[0] == 3
        assert run.totals("pilot")[0] == 1
    assert sorted(calls) == sorted(r.symbol for r in rs)
    assert all(b - a >= f.Limits().spacing - 1e-10 for a, b in pairwise(starts))
    minute = b1.load_minutes(root / "supplement.jsonl.gz")
    assert sum(len(x) for x in minute.closes.values()) == 6
    assert raw[4:8] == b"\0\0\0\0"  # gzip mtime
    assert len(gzip.decompress(raw).splitlines()) == 4


def test_attempt_charged_before_network_and_retry_count_survives_restart(setup: Any) -> None:
    root, contract, rs, clock = setup
    with f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run:
        assert run._start(rs[0].key, "pilot") == 1  # kill before opening HTTP
        assert run.totals()[0] == 1
    calls: list[str] = []
    with (
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
        client_for(rs, calls) as client,
    ):
        assert run.receipts[rs[0].key][0]["error"] == "interrupted"
        assert run.fetch("pilot", client)["passed"]
        assert run.totals()[0] == 2
    assert calls == [rs[0].symbol]


def test_budget_precedes_each_stream_read_and_cannot_reset(tmp_path: Path) -> None:
    rs = requests(1)
    contract = {"limits": asdict(f.Limits(body_bytes=f.CHUNK, pilot_bytes=f.CHUNK))}
    root = tmp_path / "capped"
    f.prepare(root, contract, rs, [rs[0].key])
    pulled: list[int] = []

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for i in range(3):
                pulled.append(i)
                yield b"x" * f.CHUNK

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Body())

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        f.Run(root, contract) as run,
    ):
        with pytest.raises(f.BudgetExceededError, match="body"):
            run.fetch("pilot", client)
        assert pulled == [0]
        assert run.totals() == (1, f.CHUNK)
        assert not (root / "pilot.json").exists()
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        f.Run(root, contract) as run,
    ):
        with pytest.raises(f.BudgetExceededError):
            run.fetch("pilot", client)
        assert run.totals()[1] == f.CHUNK
        assert pulled == [0]
        with pytest.raises(ValueError):
            run.seal()


def test_global_cap_includes_successful_pilot(setup: Any) -> None:
    root, contract, rs, clock = setup
    # A separate, synthetically smaller run, never an override of the real frozen CLI limits.
    root = root.parent / "small"
    contract = {**contract, "limits": asdict(f.Limits(attempts=1))}
    f.prepare(root, contract, rs, [rs[0].key])
    calls: list[str] = []
    with (
        client_for(rs, calls) as client,
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
    ):
        assert run.fetch("pilot", client)["passed"]
        with pytest.raises(f.BudgetExceededError, match="attempt"):
            run.fetch("full", client)
        assert run.totals()[0] == 1
    assert calls == [rs[0].symbol]


@pytest.mark.parametrize("change", ["symbol", "category", "bounds", "nan", "duplicate", "format"])
def test_native_integrity_failures_stop_pilot_without_replacement(setup: Any, change: str) -> None:
    root, contract, rs, _ = setup
    data = json.loads(payload(rs[0]))
    result = data["result"]
    if change == "symbol":
        result["symbol"] = "OTHERUSDT"
    elif change == "category":
        result["category"] = "inverse"
    elif change == "bounds":
        result["list"][0][0] = str(rs[0].end)
    elif change == "nan":
        result["list"][0][1] = "NaN"
    elif change == "duplicate":
        result["list"].append([*result["list"][0][:-1], "0.123"])
    else:
        result["list"] = None
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        f.Run(root, contract) as run,
    ):
        result = run.fetch("pilot", client)
        assert not result["passed"]
        assert result["fixed_intervals"] == 1
        assert result["counts"] == {"invalid": 1}
        assert run.fetch("pilot", client) == json.loads((root / "pilot.json").read_bytes())
        with pytest.raises(ValueError, match="pilot failed"):
            run.fetch("full", client)
    assert calls == 1


def test_incomplete_fixed_denominator_and_two_attempt_limit(setup: Any) -> None:
    root, contract, rs, clock = setup

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(payload(rs[0], list=[])))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
    ):
        result = run.fetch("pilot", client)
        assert result["counts"] == {"incomplete": 1}
        assert not result["passed"]
        assert run.totals()[0] == 2
        run.fetch("pilot", client)
        assert run.totals()[0] == 2


def test_saved_response_recovers_after_crash_between_file_and_db_commit(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, contract, rs, _ = setup
    calls: list[str] = []
    with client_for(rs, calls) as client, f.Run(root, contract) as run:
        original = run._commit_receipt

        def crash(*_: Any) -> None:
            raise InterruptedError("kill after fsynced receipt")

        monkeypatch.setattr(run, "_commit_receipt", crash)
        with pytest.raises(InterruptedError):
            run.fetch("pilot", client)
        monkeypatch.setattr(run, "_commit_receipt", original)
    with client_for(rs, calls) as client, f.Run(root, contract) as run:
        assert run.fetch("pilot", client)["passed"]
        assert run.totals()[0] == 1
    assert calls == [rs[0].symbol]


def test_corrupt_or_missing_receipts_refuse_resume(setup: Any) -> None:
    root, contract, rs, _ = setup
    calls: list[str] = []
    with client_for(rs, calls) as client, f.Run(root, contract) as run:
        run.fetch("pilot", client)
    receipt = next((root / "attempts").glob("*.json"))
    raw = receipt.read_bytes()
    receipt.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="checksum"):
        f.Run(root, contract)
    receipt.write_bytes(raw)
    receipt.unlink()
    with pytest.raises(ValueError, match="missing"):
        f.Run(root, contract)


def test_local_http_disconnect_is_charged_then_resumes(setup: Any) -> None:
    root, contract, rs, clock = setup
    calls: list[str] = []
    native = payload(rs[0])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802, RUF100 - stdlib name; CI pins Ruff 0.8
            calls.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(native)))
            self.end_headers()
            self.wfile.write(native[:10] if len(calls) == 1 else native)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        # Re-open a direct network transport on each request; do not preload the response body.
        class LocalTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                request.url = httpx.URL(f"http://127.0.0.1:{server.server_port}").copy_with(
                    query=request.url.query
                )
                return underlying.handle_request(request)

        with (
            httpx.HTTPTransport() as underlying,
            httpx.Client(transport=LocalTransport()) as client,
            f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
        ):
            assert run._one(rs[0].key, "pilot", client) == "retry"
            assert run.totals()[0] == 1
            assert run.totals()[1] >= f.CHUNK
        with (
            httpx.HTTPTransport() as underlying,
            httpx.Client(transport=LocalTransport()) as client,
            f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
        ):
            assert run.fetch("pilot", client)["passed"]
            assert run.totals()[0] == 2
        assert len(calls) == 2
        saved = json.loads(next((root / "attempts").glob("*-2.json")).read_bytes())
        assert base64.b64decode(saved["body_base64"]) == native
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_frozen_scope_loaded_without_market_requests() -> None:
    evidence = (
        Path(__file__).resolve().parents[3] / "docs/research/evidence/b1-minute-scope-repair-v1"
    )
    rs, pilot = f.frozen_requests(
        evidence / "four-hour-scope.json.gz", evidence / "proposed-pilot.json.gz"
    )
    assert len(rs) == 11_660 and len(pilot) == 64


@pytest.mark.parametrize(("missing", "passed"), [(3, True), (4, False)])
def test_pilot_95_percent_gate_uses_all_64_frozen_ids(
    tmp_path: Path, missing: int, passed: bool
) -> None:
    rs = requests(64)
    contract = {"limits": asdict(f.Limits())}
    root, clock = tmp_path / "pilot64", Clock()
    f.prepare(root, contract, rs, [r.key for r in rs])
    unavailable = {r.symbol for r in rs[:missing]}
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        calls.append(symbol)
        r = next(r for r in rs if r.symbol == symbol)
        raw = payload(r, list=[]) if symbol in unavailable else payload(r)
        return httpx.Response(200, stream=httpx.ByteStream(raw))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
    ):
        result = run.fetch("pilot", client)
        assert result["passed"] == passed
        assert result["fixed_intervals"] == result["terminal_intervals"] == 64
        assert result["complete"] == 64 - missing
        assert len(calls) == 64 + missing
        if passed:
            run.fetch("full", client)
            run.seal()
            minute = b1.load_minutes(root / "supplement.jsonl.gz")
            assert len(minute.failed) == missing
            assert len(calls) == 64 + missing


def test_unsaved_byte_reservation_is_not_refunded_after_kill(setup: Any) -> None:
    root, contract, rs, clock = setup
    with f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run:
        n = run._start(rs[0].key, "pilot")
        run._reserve(rs[0].key, n, "pilot")
        assert run.totals() == (1, f.CHUNK)
    calls: list[str] = []
    with (
        f.Run(root, contract, now=clock.now, sleep=clock.sleep) as run,
        client_for(rs, calls) as client,
    ):
        assert run.totals() == (1, f.CHUNK)
        run.fetch("pilot", client)
        assert run.totals() == (2, 3 * f.CHUNK)


def test_ledger_counters_cannot_disappear_behind_existing_parts(setup: Any) -> None:
    root, contract, rs, _ = setup
    calls: list[str] = []
    with client_for(rs, calls) as client, f.Run(root, contract) as run:
        run.fetch("pilot", client)
        with run.db:
            run.db.execute("DELETE FROM attempts")
    with pytest.raises(ValueError, match="without a charged"):
        f.Run(root, contract)


def test_equal_duplicates_fold_and_conflicting_native_ohlc_is_rejected() -> None:
    r = requests(1)[0]
    data = json.loads(payload(r))
    data["result"]["list"].append(list(data["result"]["list"][0]))
    assert f.validate(r, json.dumps(data).encode(), 200, None) == "complete"
    data["result"]["list"][-1][2] = "-0.0011"  # same close, different high
    assert f.validate(r, json.dumps(data).encode(), 200, None) == "invalid"
