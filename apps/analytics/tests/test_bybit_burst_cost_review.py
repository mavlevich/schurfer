"""Regressions from design review 1 (reviewer-written; annotated, assertions kept)."""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_burst_cost as c
from schurfer_analytics.edge_loss_study import sha256_file
from schurfer_analytics.source_lead_multi_source_report import write_once

if TYPE_CHECKING:
    from pathlib import Path

BAR = 1789000000


def book(price: float) -> dict[str, Any]:
    return {"status": "ok", "age_ms": 0, "bids": [(price, 1000)], "asks": [(price, 1000)]}


def row(symbol: str, day: str, *, resolved: bool) -> dict[str, Any]:
    cells = {}
    for d in c.ENTRY_DELAYS_S:
        cells[f"{d:g}"] = (
            {
                "status": "ok",
                "half_spread_bps": 20,
                "entry_impact_bps": 20,
                "exit_impact_bps": 20,
                "round_trip_cost_bps": 51,
                "gross_exec_bps": -89,
                "net_bps": -100,
            }
            if resolved
            else {"status": "no_book"}
        )
    return {
        "symbol": symbol,
        "day": day,
        "m": {"exit_status": "ok" if resolved else "no_book", "entries": cells},
    }


def test_review_one_resolved_firing_cannot_park_the_population() -> None:
    rows = [row("AAA", "2026-08-14", resolved=True)] + [
        row("ZZZ", "2026-08-15", resolved=False) for _ in range(731)
    ]
    result = c.summarize(rows, 1, {})
    # below the registered minimums the population is never parked
    assert result["decision"] == "insufficient_data", result["decision"]


def test_review_cache_is_counted_before_any_new_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "2026-08-14_ZUSDT_ob200.data.zip"
    existing.write_bytes(b"x" * 90)
    monkeypatch.setattr(c, "MAX_BYTES", 100)
    monkeypatch.setattr(
        c, "book_days", lambda _: [("AUSDT", "2026-08-14"), ("ZUSDT", "2026-08-14")]
    )
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"y" * 20))
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    with pytest.raises(SystemExit, match="cap"):
        c.fetch_books([], tmp_path)
    used = sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
    assert (
        used <= c.MAX_BYTES
    ), f"download cap {c.MAX_BYTES} was exceeded; files remain using {used} bytes"


def test_review_exit_fee_is_charged_on_exit_notional() -> None:
    firing = {"symbol": "AAAUSDT", "bar_start": BAR}
    m = c.moments(firing)
    books = {ms: book(1.0) for name, ms in m.items() if name != "exit"}
    books[m["exit"]] = book(2.0)
    result = c.measure(firing, books, {"status": "ok", "settlements": []})["entries"]["5"]
    # The same quantity bought for $50 is sold for $100.
    fees_bps = c.FEE_BPS * (1 + 2.0)
    expected = (2.0 / 1.0 - 1) * 10000 - fees_bps
    assert result["net_bps"] == pytest.approx(
        expected
    ), f"exit fee assumes $50 proceeds: net={result['net_bps']} expected={expected}"


def test_review_missing_funding_instrument_is_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firing = {"symbol": "AAAUSDT", "bar_start": BAR}
    monkeypatch.setattr(c, "load_firings", lambda _: [firing])
    monkeypatch.setattr(c, "trade_proxy_means", lambda _: {})
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    moments = c.moments(firing)
    name = f"{c.day_of(moments['entry_0'])}_AAAUSDT_ob200.data.zip"
    path = books_dir / name
    messages = []
    for i, ts in enumerate(sorted(set(moments.values()))):
        messages.append(
            {
                "topic": "orderbook.200.AAAUSDT",
                "type": "snapshot",
                "ts": ts,
                "data": {
                    "s": "AAAUSDT",
                    "u": i + 1,
                    "seq": i + 1,
                    "b": [["1", "1000"]],
                    "a": [["1", "1000"]],
                },
            }
        )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("book.data", "".join(json.dumps(x) + "\n" for x in messages))
    write_once(
        tmp_path / c.BOOKS_NAME,
        {
            "contract_sha256": c.contract_sha256(),
            "files": {name: {"status": "ok", "sha256": sha256_file(path)}},
        },
    )
    write_once(
        tmp_path / c.FUNDING_NAME, {"contract_sha256": c.contract_sha256(), "instruments": {}}
    )
    try:
        result = c.read(
            tmp_path,
            books_dir,
            tmp_path / "firings.json",
            tmp_path / "decay.json",
            "synthetic-review",
        )
    except (ValueError, SystemExit):
        return
    net = result["by_entry_delay"]["5"]["net_bps"]["mean"]
    assert net is None, f"missing funding still produces resolved net return: {net} bps"
