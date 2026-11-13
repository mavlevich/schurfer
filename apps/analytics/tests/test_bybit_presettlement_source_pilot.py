from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from schurfer_analytics import bybit_presettlement_source_fetch as f
from schurfer_analytics import bybit_presettlement_source_quality as q
from schurfer_analytics.bybit_compressed_spring_tape import BudgetExceededError
from schurfer_analytics.bybit_presettlement_book_scope import END, FIRST


def contract() -> dict[str, Any]:
    return {
        "byte_cap": 10 * f.CHUNK,
        "tasks": [
            {
                "id": "test",
                "symbol": "TESTUSDT",
                "day": "2024-10-01",
                "kind": "trades",
                "url": "https://example.test/a.csv.gz",
                "bytes": 4,
                "etag": '"test"',
            }
        ],
    }


def test_download_reuses_sealed_native_body_and_refuses_corruption(tmp_path: Path) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200, headers={"content-length": "4", "etag": '"test"'}, stream=httpx.ByteStream(b"body")
        )

    root = tmp_path / "source"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        one = f.fetch(root, contract(), client)
        assert one["attempts"] == one["complete"] == 1
        assert f.fetch(root, contract(), client) == one
        assert calls == ["GET"]
        body = next((root / "bodies").glob("*"))
        body.write_bytes(b"oops")
        with pytest.raises(ValueError, match="corrupt"):
            f.fetch(root, contract(), client)


def test_cap_stops_before_body_read_and_survives_resume(tmp_path: Path) -> None:
    c = contract()
    c["byte_cap"] = f.CHUNK

    class NoBody(httpx.SyncByteStream):
        def __iter__(self) -> NoBody:
            return self

        def __next__(self) -> bytes:
            raise AssertionError("budget must reject before iterator is read")

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": "4", "etag": '"test"'}, stream=NoBody()
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(BudgetExceededError),
    ):
        f.fetch(tmp_path / "source", c, client)
    assert not list((tmp_path / "source/bodies").iterdir())


def test_changed_source_etag_does_not_become_native_evidence(tmp_path: Path) -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-length": "4", "etag": '"changed"'},
                    stream=httpx.ByteStream(b"body"),
                )
            )
        ) as client,
        pytest.raises(ValueError, match="version"),
    ):
        f.fetch(tmp_path / "source", contract(), client)
    assert not list((tmp_path / "source/bodies").iterdir())


def test_native_receipt_can_move_between_worktrees_without_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "moved"
    (root / "bodies").mkdir(parents=True)
    expected = root / "bodies/test.csv.gz"
    expected.write_bytes(b"body")
    receipt = {"task": contract()["tasks"][0], "path": "old/worktree/bodies/test.csv.gz"}
    assert f.body_path(root, receipt).read_bytes() == b"body"
    receipt["path"] = "old/wrong.csv.gz"
    with pytest.raises(ValueError, match="filename"):
        f.body_path(root, receipt)
    expected.unlink()
    outside = tmp_path / "outside.csv.gz"
    outside.write_bytes(b"body")
    expected.symlink_to(outside)
    receipt["path"] = "old/test.csv.gz"
    with pytest.raises(ValueError, match="escapes"):
        f.body_path(root, receipt)


def test_partial_retry_is_charged_and_publication_gap_recovers(tmp_path: Path) -> None:
    calls = []

    def handle(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            headers={"content-length": "4", "etag": '"test"'},
            stream=httpx.ByteStream(b"bo" if len(calls) == 1 else b"body"),
        )

    root = tmp_path / "source"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = f.fetch(root, contract(), client)
        assert result["attempts"] == 2
        assert result["reserved_bytes"] == 4 * f.CHUNK
        (root / "completed/test.json").unlink()
        (root / "manifest.json").unlink()
        assert f.fetch(root, contract(), client)["complete"] == 1
        assert len(calls) == 2
        (root / "budget.jsonl").write_text("")
        with pytest.raises(ValueError, match="without byte reservation"):
            f.fetch(root, contract(), client)


def test_failed_attempt_limit_survives_restart(tmp_path: Path) -> None:
    calls = []

    def handle(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="two attempts"):
                f.fetch(tmp_path / "source", contract(), client)
    assert len(calls) == 2


def message(
    at: int, kind: str, uid: int, bids: list[list[str]], asks: list[list[str]]
) -> dict[str, Any]:
    return {
        "type": kind,
        "ts": at,
        "cts": at,
        "data": {"s": "TESTUSDT", "u": uid, "seq": uid, "b": bids, "a": asks},
    }


def test_monotonic_id_jump_is_not_an_invented_sequence_gap() -> None:
    at = FIRST + 10 * 60_000
    book = q.Book("TESTUSDT")
    book.apply(message(at, "snapshot", 10, [["100", "3"], ["99", "3"]], [["101", "3"]]))
    book.apply(message(at + 1, "delta", 15, [["100", "0"], ["98", "1"]], []))
    assert book.valid and book.counts["update_id_jumps"] == 1
    assert book.best_bid == max(book.bids) == 99
    assert book.best_ask == min(book.asks) == 101


def test_cached_extrema_match_full_reconstruction() -> None:
    at = FIRST + 10 * 60_000
    b = q.Book("TESTUSDT")
    b.apply(message(at, "snapshot", 1, [["100", "3"], ["99", "3"]], [["101", "3"], ["102", "3"]]))
    changes = [
        ([["98", "1"]], []),
        ([["100", "0"], ["97", "1"]], []),
        ([], [["101", "0"], ["103", "2"]]),
        ([["100", "2"]], [["100.5", "1"]]),
    ]
    for i, (bids, asks) in enumerate(changes, 2):
        b.apply(message(at + i, "delta", i, bids, asks))
        assert b.best_bid == max(b.bids) and b.best_ask == min(b.asks)


def test_real_zip_csv_quality_keeps_masked_values_opaque(tmp_path: Path) -> None:
    start = FIRST
    books = [
        message(start + 5 * 60_000, "snapshot", 1, [["100", "3"]], [["101", "3"]]),
        message(start + 5 * 60_000 + 100, "delta", 5, [["100", "2"]], []),
    ]
    book = tmp_path / "book.zip"
    with zipfile.ZipFile(book, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("book.data", "\n".join(json.dumps(m) for m in books) + "\n")
    tape = tmp_path / "trades.csv.gz"
    with gzip.open(tape, "wt") as out:
        out.write("timestamp,symbol,side,size,price,homeNotional,foreignNotional\n")
        out.write(f"{start / 1000},TESTUSDT,UNKNOWN,INVALID,INVALID,INVALID,INVALID\n")
        out.write(f"{(start + 5 * 60_000 + 50) / 1000},TESTUSDT,Sell,1,100,1,100\n")
    r = q.inspect_pair(book, tape, "TESTUSDT", q.InflatedBudget())
    assert r["book_clock"] == "cts"
    assert r["trade_counts"]["reserved_rows_only_counted"] == 1
    assert r["trade_counts"]["unit_mismatches"] == 0
    assert r["trade_counts"]["class_unknown"] == 1
    assert r["strict_prior_quote_alignment"]["consistent"] == 1
    assert not r["maker_volume_identified"]


def test_native_day_trailer_is_counted_without_applying_prices(tmp_path: Path) -> None:
    start = FIRST + 5 * 60_000
    book = tmp_path / "book.zip"
    rows = [
        message(start, "snapshot", 1, [["100", "3"]], [["101", "3"]]),
        message(FIRST + 86_400_000 + 1000, "snapshot", 2, [["DO_NOT_PARSE", "x"]], []),
    ]
    with zipfile.ZipFile(book, "w") as out:
        out.writestr("data", "\n".join(json.dumps(m) for m in rows) + "\n")
    tape = tmp_path / "trades.csv.gz"
    with gzip.open(tape, "wt") as out:
        out.write("timestamp,symbol,side,size,price\n")
        out.write(f"{(start + 50) / 1000},TESTUSDT,Sell,1,100\n")
    r = q.inspect_pair(book, tape, "TESTUSDT", q.InflatedBudget(), "2024-10-01")
    assert r["book_counts"]["messages"] == 1
    assert r["book_counts"]["off_day_rows_ignored"] == 1
    assert r["strict_prior_quote_alignment"]["consistent"] == 1


def test_outside_discovery_refused_before_prices_are_parsed() -> None:
    b = q.Book("TESTUSDT")
    m = message(END, "snapshot", 1, [["DO_NOT_PARSE", "1"]], [["x", "1"]])
    with pytest.raises(ValueError, match="outside registered"):
        b.apply(m)


def test_fixed_plan_is_24_native_files() -> None:
    p = (
        Path(__file__).resolve().parents[3]
        / "docs/research/evidence/b2-archive-scope-2026-11-13/results/pilot-body-plan.json"
    )
    assert f.file_sha(p) == f.PLAN_SHA
    tasks = f.tasks(json.loads(p.read_bytes()))
    assert len(tasks) == 24 and sum(t["bytes"] for t in tasks) == 1_555_848_890
