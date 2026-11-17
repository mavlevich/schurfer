"""I25 stage 1 fetcher: two gos, persisted stops, binding, journal and bounded reads.

Every test runs the real fetcher against `new_perp_world`, an httpx mock transport: no
network. Prices, rates and tick sizes in its bodies are sentinels, and so is a launch after
the cut.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import TYPE_CHECKING

import httpx
import pytest
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics import new_perp_stage1_verdict as v1

from .new_perp_world import (
    PRICE,
    SENTINELS,
    World,
    fetcher,
    ms,
    rehash,
    run_go,
    run_world,
    streamed,
)

if TYPE_CHECKING:
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


def test_a_body_over_the_cap_is_cut_at_the_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-6 regression: a 65,537 byte body used to be counted in full against a 65,536 cap."""
    cap = 65_536
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D3", dataclasses.replace(s1.CLASS_BY_ID["D3"], max_response_bytes=cap)
    )
    world = small_world(2)
    root = tmp_path / "run"
    run_go(world, root, 1)
    world.raw["/v5/market/kline"] = b"x" * (cap + 1)
    run_go(world, root, 2)
    receipts = [json.loads(p.read_text()) for p in (root / "receipts").glob("*.json")]
    big = [r for r in receipts if r["outcome"] == "too_large"]
    assert len(big) == 1 and big[0]["body_bytes"] == cap  # the byte past the cap is not counted


def test_the_first_file_stream_is_cut_at_the_response_cap(
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
    assert {r["body_bytes"] for r in receipts if r["cls"] == "D2"} == {40}


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
