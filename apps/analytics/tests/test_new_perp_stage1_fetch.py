"""I25 stage 1 fetcher: two gos, persisted stops, binding, journal and bounded reads.

Every test runs the real fetcher against `new_perp_world`, an httpx mock transport: no
network. Prices, rates and tick sizes in its bodies are sentinels, and so is a launch after
the cut.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics import new_perp_stage1_verdict as v1
from schurfer_analytics.bybit_carry_feasibility import publish_once

from .new_perp_go1_sizes import SIZES_KIB
from .new_perp_world import (
    PRICE,
    SENTINELS,
    Clock,
    World,
    fetcher,
    ms,
    rehash,
    run_go,
    run_world,
    streamed,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CUT = s1.CUT_MS


def small_world(count: int = 12) -> World:
    world = World.evenly(count)
    world.prior = [("1000T000USDT", "T000", ms("2024-06-01T00:00:00"))]
    world.archive_only = ["GHOSTUSDT"]
    return world


def all_text(root: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(root.rglob("*.json")))


def sends(world: World, needle: str) -> int:
    return sum(1 for r in world.requests if needle in str(r.url))


# the two gos


def test_go_1_reads_metadata_only_and_writes_the_manifest(tmp_path: Path) -> None:
    world = small_world()
    root = tmp_path / "run"
    summary = run_go(world, root, 1)
    n = len(world.launches)
    assert summary["attempts_by_class"] == {"C1": 3, "C3": 1, "D1": n + 1, "D2h": n}
    hosts = {r.url.host for r in world.requests}
    assert hosts == {"api.bybit.com", "public.bybit.com"}
    # no file body, no bar, no funding page: not one GET of a trade file or of a price series
    assert not any(r.method == "GET" and r.url.path.endswith(".csv.gz") for r in world.requests)
    assert not any(x in str(r.url) for r in world.requests for x in ("kline", "funding"))
    assert (root / "go1_done.json").exists() and (root / "manifest.json").exists()
    manifest = summary["manifest"]
    assert manifest["launches"] == n and manifest["d2_requests"] == n
    assert manifest["preflight"]["within_cap"] is True
    for sentinel in SENTINELS:
        assert sentinel not in all_text(root)


def test_go_2_cannot_run_out_of_order(tmp_path: Path) -> None:
    world = small_world(4)
    root = tmp_path / "run"
    f2, _ = fetcher(world, root, go=2)
    with pytest.raises(s1.FetchStoppedError, match="finished go 1"):
        s1.run_go2(f2, "0" * 64)
    assert world.requests == []
    run_go(world, tmp_path / "other", 1)
    f2b, _ = fetcher(world, tmp_path / "other", go=2)
    with pytest.raises(s1.FetchStoppedError, match="not the one that was approved"):
        s1.run_go2(f2b, "0" * 64)
    assert not any(r.url.path.endswith(".csv.gz") and r.method == "GET" for r in world.requests)
    # a go 1 fetcher refuses a go 2 class even if asked directly
    f1, _ = fetcher(world, tmp_path / "third", go=1)
    request = s1.bar_requests(
        [{"symbol": "AAAUSDT", "launch_ms": 1, "t0_ms": 1, "entry_ms": 1, "exit_ms": 2}]
    )[0]
    with pytest.raises(s1.FetchStoppedError, match="not in go 1"):
        f1.run_request(request)


def test_go_2_refuses_a_manifest_that_changed_after_it_was_approved(tmp_path: Path) -> None:
    world = small_world(4)
    root = tmp_path / "run"
    run_go(world, root, 1)
    approved = s1.manifest_sha256(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["d2_requests"] += 1
    (root / "manifest.json").write_text(json.dumps(manifest))
    f2, _ = fetcher(world, root, go=2)
    with pytest.raises(s1.FetchStoppedError, match="not the one that was approved"):
        s1.run_go2(f2, approved)


def test_a_full_run_requests_only_the_contract_and_stores_no_value(tmp_path: Path) -> None:
    world = small_world()
    summary, report = run_world(world, tmp_path / "run")
    n = len(world.launches)
    counts = report["requests"]["by_class"]
    # C1 3, C3 1, D1 launches + one archive-only candidate, D2h and D2 per launch, then one
    # entry bar, mark series and trade series per candidate, one funding page each
    expected = {
        "C1": 3, "C3": 1, "D1": n + 1, "D2h": n, "D2": n,
        "D3": n - 1, "D4": n - 1, "D5": n - 1, "D6": n - 1,
    }  # fmt: skip
    assert counts == expected  # T000 is a relist (the prior 1000T000USDT): never entered
    assert summary["go"] == 2
    text = all_text(tmp_path / "run")
    for sentinel in SENTINELS:
        assert sentinel not in text
    for request in world.requests:
        url = str(request.url)
        assert request.url.host in {"api.bybit.com", "public.bybit.com"}
        assert "POSTCUT" not in url
        assert all(day < "2025-10-01" for day in re.findall(r"\d{4}-\d{2}-\d{2}", url))
        for key in ("start", "end", "startTime", "endTime"):
            value = dict(request.url.params).get(key)
            assert value is None or int(value) < CUT
    assert report["launches"] == n
    for path in (tmp_path / "run" / "receipts").glob("*.json"):
        receipt = json.loads(path.read_text())
        assert {"cls", "key", "outcome", "body_sha256", "body_bytes"} <= set(receipt)


def test_the_first_json_response_of_each_class_leaves_a_key_only_schema(tmp_path: Path) -> None:
    run_world(small_world(3), tmp_path / "run")
    schemas = {}
    for path in sorted((tmp_path / "run" / "receipts").glob("*.json")):
        receipt = json.loads(path.read_text())
        if "schema" in receipt:
            schemas[receipt["cls"]] = receipt["schema"]
    assert {"C1", "D3", "D4", "D5", "D6"} <= set(schemas)
    assert all(
        re.fullmatch(r"[\w.\[\]]+:[a-z]+", entry) for tree in schemas.values() for entry in tree
    )


def test_a_request_asks_for_an_identity_encoding(tmp_path: Path) -> None:
    world = small_world(2)
    run_go(world, tmp_path / "run", 1)
    assert {r.headers["accept-encoding"] for r in world.requests} == {"identity"}


# retries, pacing, stops


def test_a_429_is_retried_after_retry_after_and_slows_the_run(tmp_path: Path) -> None:
    world = small_world(2)
    world.fail["instruments-info"] = [429, 500]
    f, clock = fetcher(world, tmp_path / "run")
    s1.run_go1(f)
    assert 7.0 in clock.sleeps  # the server's Retry-After
    assert 30.0 in clock.sleeps  # then the ladder: the second failed attempt of one request
    assert f.spacing == 2.0  # doubled once, by the 429 only
    assert f.summary()["attempts_by_class"]["C1"] == 5  # 3 requests + 2 retries


def test_the_slowdown_of_a_429_persists_across_a_resume(tmp_path: Path) -> None:
    """Q14: a resumed run does not start again at full speed after a rate limit."""
    world = small_world(2)
    world.fail["instruments-info"] = [429]
    world.crash = ["public.bybit.com/trading/T000USDT/"]  # die in phase 1
    root = tmp_path / "run"
    f, _ = fetcher(world, root)
    with pytest.raises(RuntimeError):
        s1.run_go1(f)
    again, _ = fetcher(world, root)
    assert again.spacing == 2.0


def test_three_failed_attempts_make_a_request_unavailable_not_a_stop(tmp_path: Path) -> None:
    world = small_world(3)
    world.fail["funding/history"] = [500, 500, 500]
    root = tmp_path / "run"
    run_go(world, root, 1)
    summary = run_go(world, root, 2)
    assert summary["outcomes"]["D6"]["unavailable"] == 1
    assert summary["outcomes"]["D6"]["ok"] == 1  # T000 is a relist; two candidates remain


def test_a_core_class_that_stays_unavailable_stops_the_run(tmp_path: Path) -> None:
    world = small_world(2)
    world.fail["instruments-info"] = [500, 500, 500]
    f, _ = fetcher(world, tmp_path / "run")
    with pytest.raises(s1.FetchStoppedError, match="C1"):
        s1.run_go1(f)
    assert (tmp_path / "run" / "stop.json").exists()


def test_403_stops_without_a_retry_and_ends_the_root(tmp_path: Path) -> None:
    """P1-4 regression: after a 403 a new fetcher used to send the same request again."""
    world = small_world(3)
    world.fail["/v5/market/kline"] = [403]
    root = tmp_path / "run"
    run_go(world, root, 1)
    f2, clock = fetcher(world, root, go=2)
    with pytest.raises(s1.FetchStoppedError, match="403"):
        s1.run_go2(f2, s1.manifest_sha256(root))
    assert clock.sleeps == [] or all(x <= 1.0 for x in clock.sleeps)
    stop = json.loads((root / "stop.json").read_text())
    assert stop["go"] == 2 and "403" in stop["reason"]
    before = len(world.requests)
    with pytest.raises(s1.FetchStoppedError, match="was stopped"):
        fetcher(world, root, go=2)
    with pytest.raises(s1.FetchStoppedError, match="was stopped"):
        fetcher(world, root, go=1)
    assert len(world.requests) == before  # nothing was sent again


def test_a_403_on_the_optional_looking_class_is_still_global(tmp_path: Path) -> None:
    """There is no optional class any more: a 403 on any class ends the whole root."""
    world = small_world(2)
    world.fail["instruments-info"] = [403]
    f, _ = fetcher(world, tmp_path / "run")
    with pytest.raises(s1.FetchStoppedError):
        s1.run_go1(f)
    assert (tmp_path / "run" / "stop.json").exists()


def test_a_stopped_run_has_no_success_verdict(tmp_path: Path) -> None:
    world = World.evenly(105)
    world.fail["/v5/market/kline"] = []  # a healthy world up to the stop
    root = tmp_path / "run"
    run_go(world, root, 1)
    world.fail["/v5/market/funding"] = [403]
    f2, _ = fetcher(world, root, go=2)
    with pytest.raises(s1.FetchStoppedError):
        s1.run_go2(f2, s1.manifest_sha256(root))
    report = v1.evaluate(v1.load_run(root))
    assert report["outcome"] == "stage1_stopped" and "pass" not in report["outcome"]
    assert report["stop"]["reason"].startswith("D6")


def test_a_schema_mismatch_stops_and_names_a_reason_not_a_value(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda r: streamed(content=b'{"a": 1}'))
    f = s1.Fetcher(tmp_path / "run", s1.build_contract(), httpx.Client(transport=transport))
    with pytest.raises(s1.FetchStoppedError, match=r"schema mismatch \(envelope\)"):
        s1.run_go1(f)
    receipt = json.loads(next((tmp_path / "run" / "receipts").glob("*.json")).read_text())
    assert receipt["outcome"] == "schema_mismatch" and receipt["schema"] == ["a:int"]
    assert "schema mismatch" in json.loads((tmp_path / "run" / "stop.json").read_text())["reason"]


# resume and binding


def test_a_crashed_run_resumes_without_repeating_finished_requests(tmp_path: Path) -> None:
    world = small_world(5)
    world.crash = ["/v5/market/kline"]  # the process dies on the first entry bar
    root = tmp_path / "run"
    run_go(world, root, 1)
    with pytest.raises(RuntimeError):
        run_go(world, root, 2)
    first_files = sends(world, ".csv.gz")
    summary = run_go(world, root, 2)
    assert sends(world, ".csv.gz") == first_files  # finished D2 requests are not sent again
    assert summary["outcomes"]["D3"]["ok"] == 4
    assert not (root / "stop.json").exists()  # a crash is not a stop


def test_an_interrupted_phase_zero_starts_a_new_round(tmp_path: Path) -> None:
    world = small_world(3)
    world.crash = ["public.bybit.com/trading/"]  # the root listing, after the catalog
    root = tmp_path / "run"
    f, _ = fetcher(world, root)
    with pytest.raises(RuntimeError):
        s1.run_go1(f)
    assert not (root / "phase0.json").exists()
    f2, _ = fetcher(world, root)
    s1.run_go1(f2)
    assert len(list((root / "rounds").glob("*.json"))) == 2
    assert f2.summary()["attempts_by_class"]["C1"] == 6  # the whole phase, twice


def test_a_resume_under_different_code_is_refused(tmp_path: Path) -> None:
    """P1-3 regression: only the contract hash was compared, so a parser fix could resume."""
    world = small_world(2)
    root = tmp_path / "run"
    old = {"revision": "OLD_CODE", "publication": {"new_perp_stage1.py": "a" * 64}}
    f, _ = fetcher(world, root, code=old)
    s1.run_go1(f)
    fetcher(world, root, code=old)  # the same code resumes
    with pytest.raises(ValueError, match="run binding differs"):
        fetcher(world, root, code={**old, "revision": "NEW_CODE"})
    with pytest.raises(ValueError, match="run binding differs"):
        fetcher(world, root, code={**old, "publication": {"new_perp_stage1.py": "b" * 64}})
    with pytest.raises(ValueError, match="run binding differs"):
        fetcher(world, root)  # no code binding at all is also a different binding


def test_the_run_binding_refuses_a_different_contract(tmp_path: Path) -> None:
    f, _ = fetcher(small_world(2), tmp_path / "run")
    del f
    other = json.loads(json.dumps(s1.build_contract()))
    other["go_limits"]["1"]["max_attempts"] += 1
    with pytest.raises(ValueError, match="run binding differs"):
        fetcher(small_world(2), tmp_path / "run", rehash(other))


def test_the_attempt_cap_is_a_hard_stop_and_persisted(tmp_path: Path) -> None:
    tight = json.loads(json.dumps(s1.build_contract()))
    tight["go_limits"]["1"]["max_attempts"] = 4
    f, _ = fetcher(small_world(2), tmp_path / "run", rehash(tight))
    with pytest.raises(s1.BudgetExceededError, match="attempt cap"):
        s1.run_go1(f)
    assert "attempt cap" in json.loads((tmp_path / "run" / "stop.json").read_text())["reason"]
    with pytest.raises(s1.FetchStoppedError, match="was stopped"):
        fetcher(small_world(2), tmp_path / "run", rehash(tight))


def test_the_command_line_refuses_an_unapproved_hash_before_any_request(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved hash"):
        s1.main(
            ["fetch", "--go", "1", "--approved-sha256", "0" * 64, "--root", str(tmp_path / "r")]
        )
    assert not (tmp_path / "r").exists()


# The attempt journal and bounded reads.


def test_a_send_that_died_before_its_receipt_still_counts_as_an_attempt(tmp_path: Path) -> None:
    """P2-5 regression: attempts were counted from receipts, so a fourth send happened."""
    world = small_world(3)
    root = tmp_path / "run"
    run_go(world, root, 1)
    world.crash = ["funding/history"]  # the first funding send dies before its receipt
    with pytest.raises(RuntimeError):
        run_go(world, root, 2)
    world.fail["funding/history"] = [500, 500]  # then two failed sends for the same request
    summary = run_go(world, root, 2)
    first = sorted(
        r.url.params["symbol"] for r in world.requests if "funding/history" in str(r.url)
    )
    crashed = first[0]
    per_symbol = sum(
        1
        for r in world.requests
        if "funding/history" in str(r.url) and r.url.params["symbol"] == crashed
    )
    assert per_symbol == 3  # one died, two failed: never a fourth
    assert summary["outcomes"]["D6"]["unavailable"] == 1


class CountingStream(httpx.SyncByteStream):
    """A body of fixed-size pieces that counts every byte the client pulls from it."""

    def __init__(self, total: int, piece: int = 1000) -> None:
        self.total, self.piece, self.pulled = total, piece, 0

    def __iter__(self) -> Iterator[bytes]:
        left = self.total
        while left > 0:
            size = min(self.piece, left)
            left -= size
            self.pulled += size
            yield b"x" * size


def bar_request() -> s1.Request:
    candidate = {
        "symbol": "AAAUSDT",
        "launch_ms": 1,
        "t0_ms": ms("2024-11-01T10:00:00"),
        "entry_ms": ms("2024-11-08T10:00:00"),
        "exit_ms": ms("2024-11-15T10:00:00"),
    }
    return s1.bar_requests([candidate])[0]


def test_every_received_byte_is_accounted_when_the_length_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 regression: the client received 65,537 bytes and journaled 65,536 (cap 65,536)."""
    cap = 65_536
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D3", dataclasses.replace(s1.CLASS_BY_ID["D3"], max_response_bytes=cap)
    )
    stream = CountingStream(cap + 1)  # no Content-Length: the length is unknown
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    client = httpx.Client(transport=transport)
    f = s1.Fetcher(tmp_path / "run", s1.build_contract(), client, go=2)
    result = f.run_request(bar_request())
    assert result["outcome"] == "too_large"
    receipt = json.loads(next((tmp_path / "run" / "receipts").glob("*.json")).read_text())
    assert receipt["body_bytes"] == stream.pulled  # received == accounted, at the transport
    assert cap < stream.pulled <= cap + 1000  # the read ended at the first chunk over the cap


def test_a_declared_length_over_the_cap_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = 65_536
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D3", dataclasses.replace(s1.CLASS_BY_ID["D3"], max_response_bytes=cap)
    )
    stream = CountingStream(cap + 1)
    headers = {"content-length": str(cap + 1)}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=stream))
    client = httpx.Client(transport=transport)
    f = s1.Fetcher(tmp_path / "run", s1.build_contract(), client, go=2)
    assert f.run_request(bar_request())["outcome"] == "too_large"
    receipt = json.loads(next((tmp_path / "run" / "receipts").glob("*.json")).read_text())
    assert stream.pulled == 0 and receipt["body_bytes"] == 0


def test_a_first_file_declared_over_the_response_cap_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D2", dataclasses.replace(s1.CLASS_BY_ID["D2"], max_response_bytes=40)
    )
    world = small_world(2)
    root = tmp_path / "run"
    run_go(world, root, 1)
    summary = run_go(world, root, 2)
    assert summary["outcomes"]["D2"] == {"too_large": 2}
    receipts = [json.loads(p.read_text()) for p in (root / "receipts").glob("*.json")]
    assert {r["body_bytes"] for r in receipts if r["cls"] == "D2"} == {0}  # nothing received


def test_a_compressed_transfer_encoding_is_not_trusted(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return streamed(headers={"content-encoding": "br"}, content=b"{}")

    f = s1.Fetcher(
        tmp_path / "run", s1.build_contract(), httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(s1.FetchStoppedError, match="content_encoding"):
        s1.run_go1(f)


# the preflight and the big files


def test_the_preflight_stops_go_2_before_any_first_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D2", dataclasses.replace(s1.CLASS_BY_ID["D2"], max_class_bytes=10)
    )
    world = small_world(3)
    root = tmp_path / "run"
    run_go(world, root, 1)
    assert json.loads((root / "manifest.json").read_text())["preflight"]["within_cap"] is False
    f2, _ = fetcher(world, root, go=2)
    with pytest.raises(s1.FetchStoppedError, match="preflight"):
        s1.run_go2(f2, s1.manifest_sha256(root))
    assert not any(r.method == "GET" and r.url.path.endswith(".csv.gz") for r in world.requests)
    assert "preflight" in json.loads((root / "stop.json").read_text())["reason"]


def test_a_file_over_the_per_file_cap_is_never_downloaded_and_is_counted_apart(
    tmp_path: Path,
) -> None:
    world = small_world(3)
    world.sizes_scale = 10**9  # every HEAD reports a huge Content-Length
    summary, report = run_world(world, tmp_path / "run")
    assert "D2" not in report["requests"]["by_class"]
    assert summary["go1"]["preflight"]["over_per_file_cap"] == 3
    assert report["m1"]["first_file_over_cap"] == 3
    assert report["m1"]["classes"] == {"t0_over_cap": 3}
    assert report["stop_codes"][0] == "not_reconstructible"


def test_no_request_leaves_the_mock_and_no_price_reaches_a_file(tmp_path: Path) -> None:
    world = small_world(2)
    run_world(world, tmp_path / "run")
    assert world.requests, "the run made no request through the mock"
    assert PRICE not in all_text(tmp_path / "run")


# Recovery after a crash between two writes, and the persisted wait


def test_a_fatal_receipt_without_stop_json_still_stops_the_run(tmp_path: Path) -> None:
    """P1 regression: a crash between the 403 receipt and stop.json let a new fetcher resend."""
    world = small_world(3)
    world.fail["instruments-info"] = [403]
    root = tmp_path / "run"
    f, _ = fetcher(world, root)
    with pytest.raises(s1.FetchStoppedError):
        s1.run_go1(f)
    (root / "stop.json").unlink()  # the process died between the receipt and the stop file
    before = len(world.requests)
    with pytest.raises(s1.FetchStoppedError, match="recovered from its receipt"):
        fetcher(world, root)
    assert len(world.requests) == before  # nothing was resent
    assert "HTTP 403" in json.loads((root / "stop.json").read_text())["reason"]  # rewritten
    with pytest.raises(s1.FetchStoppedError, match="was stopped"):
        fetcher(world, root)


def test_a_schema_mismatch_receipt_without_stop_json_also_stops_the_run(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda r: streamed(content=b'{"a": 1}'))
    root = tmp_path / "run"
    f = s1.Fetcher(root, s1.build_contract(), httpx.Client(transport=transport))
    with pytest.raises(s1.FetchStoppedError):
        s1.run_go1(f)
    (root / "stop.json").unlink()
    with pytest.raises(s1.FetchStoppedError, match="schema mismatch"):
        s1.Fetcher(root, s1.build_contract(), httpx.Client(transport=transport))
    assert (root / "stop.json").exists()


def test_retry_after_survives_a_restart_before_the_wait(tmp_path: Path) -> None:
    """P2 regression: after a 429 with Retry-After 300 and a crash, the next process waited 10 s."""
    world = small_world(2)
    world.retry_after = 300
    world.fail["instruments-info"] = [429]
    root = tmp_path / "run"
    dying = Clock(die_over=5.0)
    f, _ = fetcher(world, root, clock=dying)
    with pytest.raises(RuntimeError):
        s1.run_go1(f)  # the 429 receipt is written, then the process dies before the wait
    later = Clock(wall_offset=dying.wall() - 1_800_000_000.0 + 1.0)  # one second later
    f2, clock = fetcher(world, root, clock=later)
    s1.run_go1(f2)
    assert 290 <= clock.sleeps[0] <= 300, clock.sleeps[:3]  # the rest of Retry-After
    assert f2.spacing == 2.0  # the doubled spacing persists too


def test_a_recovered_request_waits_the_rest_of_its_retry_after(tmp_path: Path) -> None:
    """P2 regression: a recovered request waited 10 s whatever the server had asked."""
    world = small_world(2)
    world.retry_after = 300
    world.fail["public.bybit.com/trading/T000USDT/"] = [429]  # a request with a stable key
    root = tmp_path / "run"
    dying = Clock(die_over=5.0)
    f, _ = fetcher(world, root, clock=dying)
    with pytest.raises(RuntimeError):
        s1.run_go1(f)  # the listing's 429 is written, then the process dies before the wait
    later = Clock(wall_offset=dying.wall() - 1_800_000_000.0 + 1.0)
    f2, clock = fetcher(world, root, clock=later)
    s1.run_go1(f2)
    assert 290 <= max(clock.sleeps) <= 300, clock.sleeps[:3]
    assert clock.sleeps[0] > 5  # the first thing it did was the rest of the wait, not 10 s


def test_the_not_before_time_holds_for_a_different_request_after_a_restart(
    tmp_path: Path,
) -> None:
    world = small_world(2)
    world.retry_after = 120
    world.fail["instruments-info"] = [429]
    root = tmp_path / "run"
    first = Clock()
    f, _ = fetcher(world, root, clock=first)
    f.sleep = lambda s: (_ for _ in ()).throw(RuntimeError("died"))
    with pytest.raises(RuntimeError):
        s1.run_go1(f)
    clock = Clock(wall_offset=first.wall() - 1_800_000_000.0 + 5.0)
    f2, _ = fetcher(world, root, clock=clock)
    assert f2.not_before == pytest.approx(first.wall() + 120, abs=1.0)


# The budget of each go is kept apart


def write_prior_root(root: Path, cls: str, files: int, size: int) -> None:
    for seq in range(1, files + 1):
        key = f"{cls}-{seq}"
        publish_once(root / "attempts" / f"{seq:05d}.json", {"seq": seq, "cls": cls, "key": key})
        receipt = {"seq": seq, "cls": cls, "key": key, "body_bytes": size, "outcome": "ok"}
        publish_once(root / "receipts" / f"{seq:05d}.json", receipt)


def test_prior_usage_is_kept_per_go_so_old_go_2_files_do_not_block_go_1(tmp_path: Path) -> None:
    """P2 regression: five old 64 MiB D2 files blocked a new go 1 under its 300 MiB cap."""
    prior_root = tmp_path / "old"
    write_prior_root(prior_root, "D2", 5, 64 * s1.MIB)
    usage = s1.prior_usage([prior_root])
    assert usage["1"] == {"attempts": 0, "bytes": 0}
    assert usage["2"] == {"attempts": 5, "bytes": 5 * 64 * s1.MIB}
    world = small_world(3)
    f, _ = fetcher(world, tmp_path / "new", prior=usage)
    summary = s1.run_go1(f)  # not blocked: the old go 1 spent nothing
    assert summary["bytes"] < 300 * s1.MIB
    f2, _ = fetcher(world, tmp_path / "new", go=2, prior=usage)
    assert f2.totals()[1] >= 5 * 64 * s1.MIB  # go 2 still carries its own earlier cost


def test_prior_usage_of_go_1_classes_counts_only_against_go_1(tmp_path: Path) -> None:
    prior_root = tmp_path / "old"
    write_prior_root(prior_root, "D1", 4, 1000)
    usage = s1.prior_usage([prior_root])
    assert usage["1"] == {"attempts": 4, "bytes": 4000}
    assert usage["2"] == {"attempts": 0, "bytes": 0}


# The step byte ceiling is hard


def many_bar_requests(count: int) -> list[s1.Request]:
    t0 = ms("2024-11-01T10:00:00")
    out: list[s1.Request] = []
    for i in range(count):
        candidate = {
            "symbol": f"S{i}USDT",
            "launch_ms": 1,
            "t0_ms": t0,
            "entry_ms": t0 + 7 * 86_400_000,
            "exit_ms": t0 + 14 * 86_400_000,
        }
        out.append(s1.bar_requests([candidate])[0])  # the D3 entry bar of each symbol
    return out


def small_step_contract(max_bytes: int) -> dict[str, Any]:
    contract = json.loads(json.dumps(s1.build_contract()))
    contract["go_limits"]["2"]["max_bytes"] = max_bytes
    return rehash(contract)


def run_until_the_budget_stops(
    tmp_path: Path, max_bytes: int, streams: list[CountingStream], headers: dict[str, str]
) -> tuple[int, s1.Fetcher]:
    """Send D3 requests with the given bodies until the step budget stops the run. Returns the
    bytes pulled from every stream."""
    calls = iter(streams)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=next(calls))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    f = s1.Fetcher(tmp_path / "run", small_step_contract(max_bytes), client, go=2)
    try:
        for request in many_bar_requests(len(streams)):
            f.run_request(request)
    except s1.BudgetExceededError:
        pass
    return sum(stream.pulled for stream in streams), f


def test_received_bytes_never_exceed_the_step_ceiling_without_content_length(
    tmp_path: Path,
) -> None:
    """P2 regression: ceiling 1,048,576, received 1,048,577 (and journaled the same)."""
    ceiling = 1_048_576
    streams = [CountingStream(80_000, piece=5_000) for _ in range(40)]  # no Content-Length
    pulled, f = run_until_the_budget_stops(tmp_path, ceiling, streams, {})
    assert 0 < pulled <= ceiling  # received never exceeds the ceiling
    assert f.totals()[1] == pulled  # and every received byte is in the journal's account
    assert "byte cap" in json.loads((tmp_path / "run" / "stop.json").read_text())["reason"]


def test_a_declared_length_over_what_remains_reads_nothing_near_the_ceiling(
    tmp_path: Path,
) -> None:
    ceiling = 1_048_576
    mixed = []
    for i in range(30):
        mixed.append(
            CountingStream(80_000, piece=5_000) if i % 3 == 0 else CountingStream(2_000_000)
        )
    declared = {"content-length": "2000000"}
    pulled, f = run_until_the_budget_stops(tmp_path, ceiling, mixed, declared)
    assert pulled == 0 and f.totals()[1] == 0  # a declared length over the cap reads nothing
    # and without a declared length the same bodies still stop at the ceiling
    fresh = [CountingStream(2_000_000, piece=60_000) for _ in range(40)]
    pulled2, f2 = run_until_the_budget_stops(tmp_path / "b", ceiling, fresh, {})
    assert pulled2 <= ceiling and f2.totals()[1] == pulled2


def test_a_transport_chunk_over_the_reserve_is_counted_and_stops_the_run(tmp_path: Path) -> None:
    stream = CountingStream(200_000, piece=200_000)  # one chunk larger than the 64 KiB reserve
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    )
    f = s1.Fetcher(tmp_path / "run", s1.build_contract(), client, go=2)
    with pytest.raises(s1.FetchStoppedError, match="larger than the read reserve"):
        f.run_request(many_bar_requests(1)[0])
    assert f.totals()[1] == stream.pulled == 200_000  # the overshoot is counted, not hidden
    assert (tmp_path / "run" / "stop.json").exists()


def test_the_reserve_and_the_rule_are_part_of_the_contract() -> None:
    limits = s1.build_contract()["limits"]
    assert limits["read_reserve_bytes"] == 65_536 == s1.READ_RESERVE_BYTES
    assert "used + response cap + read reserve" in limits["byte_rule"]


# The per-file cap of 512 MiB and the Go 2 budgets

MEASURED_FILES = 258  # the Go 1 run: 258 first daily files
MEASURED_TOTAL = 6737590608  # 6,425.47 MiB in bytes
MEASURED_MAX = 428_549_908  # the largest, 408.70 MiB


def d2_request(symbol: str = "AAAUSDT") -> s1.Request:
    url = s1.first_file_url(symbol, "2024-11-01")
    return s1.Request("D2", "GET", url, symbol=symbol, meta=(("first_day", "2024-11-01"),))


class CountingReducer:
    """Stands in for the gzip reader: counts the bytes it is fed, so a 400 MiB body can be
    streamed in a test without building a real archive."""

    fed = 0

    def feed(self, chunk: bytes) -> None:
        type(self).fed += len(chunk)

    def finish(self) -> dict[str, Any]:
        return {"rows": 1, "min_ms": 1, "max_ms": 1, "first_ms": 1, "last_ms": 1}


def test_the_per_file_cap_is_512_mib_and_the_measured_files_fit_it() -> None:
    assert s1.PER_FILE_CAP_BYTES == 512 * s1.MIB == s1.CLASS_BY_ID["D2"].max_response_bytes
    heads = {
        "BIGUSDT": {"outcome": "ok", "facts": {"content_length": MEASURED_MAX}},
        "EDGEUSDT": {"outcome": "ok", "facts": {"content_length": 512 * s1.MIB}},
        "OVERUSDT": {"outcome": "ok", "facts": {"content_length": 513 * s1.MIB}},
        "JUSTOVERUSDT": {"outcome": "ok", "facts": {"content_length": 512 * s1.MIB + 1}},
    }
    plan = s1.preflight(heads)
    assert set(plan["sizes"]) == {"BIGUSDT", "EDGEUSDT"}  # a 408 MiB file is read
    assert plan["over_per_file_cap"] == ["JUSTOVERUSDT", "OVERUSDT"]  # a 513 MiB one is not


def test_a_408_mib_first_file_is_read_and_a_513_mib_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s1, "FirstFileReducer", CountingReducer)
    CountingReducer.fed = 0
    big = CountingStream(MEASURED_MAX, piece=64 * 1024)
    headers = {"content-length": str(MEASURED_MAX)}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=big))
    )
    f = s1.Fetcher(tmp_path / "ok", s1.build_contract(), client, go=2)
    result = f.run_request(d2_request())
    assert result["outcome"] == "ok" and result["body_bytes"] == MEASURED_MAX == big.pulled
    assert CountingReducer.fed == MEASURED_MAX
    # 513 MiB declared: nothing is read
    over = CountingStream(513 * s1.MIB, piece=64 * 1024)
    declared = {"content-length": str(513 * s1.MIB)}
    client2 = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=declared, stream=over))
    )
    f2 = s1.Fetcher(tmp_path / "over", s1.build_contract(), client2, go=2)
    assert f2.run_request(d2_request())["outcome"] == "too_large" and over.pulled == 0
    # 513 MiB with no declared length: the read ends just past the cap, and all of it counts
    blind = CountingStream(513 * s1.MIB, piece=64 * 1024)
    client3 = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=blind))
    )
    f3 = s1.Fetcher(tmp_path / "blind", s1.build_contract(), client3, go=2)
    result = f3.run_request(d2_request())
    assert result["outcome"] == "too_large" and result["body_bytes"] == blind.pulled
    assert 512 * s1.MIB < blind.pulled <= 512 * s1.MIB + s1.READ_RESERVE_BYTES


def charge_file(f: s1.Fetcher, symbol: str, size: int) -> None:
    """Charge one D2 request and record a receipt of `size` bytes, as a finished download."""
    request = d2_request(symbol)
    seq = f._charge(request)
    f.receipts.append(
        {"seq": seq, "cls": "D2", "key": request.key, "body_bytes": size, "outcome": "ok"}
    )


def test_go_2_budget_holds_all_258_measured_files_and_the_other_classes(tmp_path: Path) -> None:
    contract = s1.build_contract()
    assert contract["go_limits"]["2"]["max_bytes"] == 8704 * s1.MIB
    big = [MEASURED_MAX] + [(3050141434 - MEASURED_MAX) // 21] * 21
    small = [3_687_449_174 // 236] * 236
    sizes = big + small
    assert len(sizes) == MEASURED_FILES and abs(sum(sizes) - MEASURED_TOTAL) < 300
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    f = s1.Fetcher(tmp_path / "run", contract, client, go=2)
    ceiling = contract["go_limits"]["2"]["max_bytes"]
    for i, size in enumerate(sizes):
        charge_file(f, f"F{i}USDT", size)
        assert f.totals()[1] <= ceiling  # never above the step ceiling
    used = f.totals()[1]
    assert used == sum(sizes) and used < s1.CLASS_BY_ID["D2"].max_class_bytes
    # every other class can still run to its own class cap inside the step budget
    others = sum(c.max_class_bytes for c in s1.CLASSES if c.go == 2 and c.cls != "D2")
    assert used + others < ceiling  # 6,425 MiB + 624 MiB < 8,704 MiB
    # and the D2 class cap is a hard stop for one more file than the measured set can need
    with pytest.raises(s1.BudgetExceededError, match="D2: class byte cap"):
        for j in range(20):
            charge_file(f, f"X{j}USDT", MEASURED_MAX)


def test_the_read_reserve_still_guards_a_512_mib_request(tmp_path: Path) -> None:
    need = s1.reserve(s1.CLASS_BY_ID["D2"])
    assert need == 512 * s1.MIB + s1.READ_RESERVE_BYTES
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    tight = small_step_contract(need - 1)
    f = s1.Fetcher(tmp_path / "tight", tight, client, go=2)
    with pytest.raises(s1.BudgetExceededError, match="byte cap"):
        f._charge(d2_request())
    enough = small_step_contract(need)
    f2 = s1.Fetcher(tmp_path / "enough", enough, client, go=2)
    f2._charge(d2_request())  # exactly cap plus reserve is allowed


# Replaying the real D2 queue: the measured sizes, in queue order, with retries


def replay_d2_queue(tmp_path: Path, retried: tuple[int, ...] = ()) -> s1.Fetcher:
    """Charge every first file in queue order against the real contract. A file index in
    `retried` is downloaded a second time in full, right after its first attempt, as a
    complete retry would be."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    f = s1.Fetcher(tmp_path, s1.build_contract(), client, go=2)
    for index, kib in enumerate(SIZES_KIB):
        for _ in range(2 if index in retried else 1):
            charge_file(f, f"Q{index}USDT", kib * 1024)
    return f


def largest(count: int) -> tuple[int, ...]:
    return tuple(sorted(range(len(SIZES_KIB)), key=lambda i: -SIZES_KIB[i])[:count])


def test_the_replayed_queue_is_the_measured_one() -> None:
    assert len(SIZES_KIB) == MEASURED_FILES
    assert abs(sum(SIZES_KIB) * 1024 - MEASURED_TOTAL) < 300 * 1024  # rounded up to KiB
    assert max(SIZES_KIB) * 1024 - MEASURED_MAX < 1024


def test_the_d2_class_cap_covers_a_full_retry_of_the_largest_file_in_queue_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 regression: with the 512 MiB + 64 KiB reserve before every request, a 7,168 MiB D2
    class cap stopped the run, after a full retry of the largest file, before the last files."""
    f = replay_d2_queue(tmp_path / "none")  # no retry: all 258 files fit
    assert len(f.attempts) == MEASURED_FILES
    one = replay_d2_queue(tmp_path / "one", largest(1))  # one full retry of the largest
    assert len(one.attempts) == MEASURED_FILES + 1
    monkeypatch.setitem(
        s1.CLASS_BY_ID,
        "D2",
        dataclasses.replace(s1.CLASS_BY_ID["D2"], max_class_bytes=7168 * s1.MIB),
    )
    with pytest.raises(s1.BudgetExceededError, match="D2: class byte cap"):
        replay_d2_queue(tmp_path / "old", largest(1))  # the old cap stops before the end


def test_the_covered_retry_scenarios_are_exactly_the_ones_the_request_list_states(
    tmp_path: Path,
) -> None:
    # one full retry of each of the three largest files, in the real order, still completes
    three = replay_d2_queue(tmp_path / "three", largest(3))
    assert len(three.attempts) == MEASURED_FILES + 3
    used = three.totals()[1]
    others = sum(c.max_class_bytes for c in s1.CLASSES if c.go == 2 and c.cls != "D2")
    # the step cap holds even if D2 reaches its class cap and D3 to D6 reach theirs
    step = three.contract["go_limits"]["2"]["max_bytes"]
    assert used + others <= step
    assert s1.CLASS_BY_ID["D2"].max_class_bytes + others <= step
    # a full retry of the four largest is not covered, and stops cleanly by the class cap
    with pytest.raises(s1.BudgetExceededError, match="D2: class byte cap"):
        replay_d2_queue(tmp_path / "four", largest(4))


def test_the_go_2_attempt_cap_covers_four_funding_pages_for_every_candidate() -> None:
    contract = s1.build_contract()
    candidates = 258  # the Go 1 manifest's upper bound
    worst_first_attempts = candidates * (1 + 3 + s1.FUNDING_MAX_PAGES)  # D2, D3 to D5, D6
    assert worst_first_attempts == 2064
    cap = contract["go_limits"]["2"]["max_attempts"]
    assert cap >= worst_first_attempts * 1.15  # at least 15% headroom for retries
    d6 = next(c for c in contract["classes"] if c["cls"] == "D6")
    assert d6["max_requests"] >= candidates * s1.FUNDING_MAX_PAGES  # 1,032 of 1,200
    go2 = [c for c in contract["classes"] if c["go"] == 2]
    assert sum(c["max_requests"] for c in go2) >= worst_first_attempts
