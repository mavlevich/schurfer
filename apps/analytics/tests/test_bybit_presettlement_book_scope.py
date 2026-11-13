from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from schurfer_analytics import bybit_presettlement_book_scope as b


def sample() -> dict[str, Any]:
    settlement = b.FIRST + 12 * 60 * 60_000
    return {
        "report_sha256": b.REPORT_SHA,
        "events_by_decision": {
            f"d{d}": [
                {
                    "symbol": "TESTUSDT",
                    "decision_ms": settlement - d * 60_000,
                    "interval_start_ms": settlement - 240 * 60_000,
                    "forecast_settlement_ms": settlement,
                    "interval_minutes": 240,
                    "premium_observed_until_ms": settlement - (d + 1) * 60_000,
                }
            ]
            for d in (30, 15, 10)
        },
    }


def test_all_offsets_share_a_day_without_duplicate_requests() -> None:
    plan = b.scope(sample())
    assert plan["symbol_days"] == 1
    assert len(plan["tasks"]) == 3
    assert {r["kind"] for r in plan["tasks"]} == {"ob500", "ob200", "trades"}
    assert plan["max_attempts"] == 6 and not plan["body_download_allowed"]


@pytest.mark.parametrize("change", ["window", "identity", "duplicate", "offset", "source"])
def test_refuses_scope_and_identity_drift(change: str) -> None:
    p = copy.deepcopy(sample())
    if change == "window":
        for rows in p["events_by_decision"].values():
            for row in rows:
                for field in (
                    "decision_ms",
                    "interval_start_ms",
                    "forecast_settlement_ms",
                    "premium_observed_until_ms",
                ):
                    row[field] += 500 * 86_400_000
    elif change == "identity":
        p["events_by_decision"]["d30"][0]["symbol"] = "../BTCUSDT"
    elif change == "duplicate":
        p["events_by_decision"]["d30"] *= 2
    elif change == "offset":
        del p["events_by_decision"]["d15"]
    else:
        p["report_sha256"] = "wrong"
    with pytest.raises(ValueError):
        b.scope(p)


def test_unknown_size_is_never_zero_and_403_is_not_missing() -> None:
    r = b.response_record("x", httpx.Response(200))
    assert r["state"] == "available" and r["size_bytes"] is None
    r = b.response_record("x", httpx.Response(403, headers={"content-length": "400"}))
    assert r["state"] == "unknown" and r["size_bytes"] is None
    r = b.response_record(
        "x",
        httpx.Response(
            200, headers={"content-length": "400", "content-type": "text/html; charset=utf-8"}
        ),
    )
    assert r["state"] == "unknown" and r["size_bytes"] is None


async def test_heads_never_read_body_and_resume_uses_saved_metadata(tmp_path: Path) -> None:
    plan = b.scope(sample())
    calls: list[str] = []

    class ForbiddenBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            raise AssertionError("a HEAD inventory must never consume a body")
            yield b""  # pragma: no cover

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-length": "777"}, stream=ForbiddenBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await b.probe(plan, tmp_path / "head", client, "frozen")
        assert result["attempts"] == 3 and result["combined_bytes_known"] == 1554
        assert result["selected_book_depths"] == {"ob500": 1}
        assert await b.probe(plan, tmp_path / "head", client, "frozen") == result
        assert len(calls) == 3
        with pytest.raises(ValueError, match="binding"):
            await b.probe(plan, tmp_path / "head", client, "changed")


async def test_killed_attempt_is_consumed_before_retry(tmp_path: Path) -> None:
    plan = b.scope(sample())
    plan["tasks"] = plan["tasks"][:1]
    plan["max_attempts"] = 2

    def killed(_: httpx.Request) -> httpx.Response:
        raise RuntimeError("kill before response publication")

    async with httpx.AsyncClient(transport=httpx.MockTransport(killed)) as client:
        with pytest.raises(RuntimeError, match="kill"):
            await b.probe(plan, tmp_path / "head", client, "frozen")
    assert len(list((tmp_path / "head/attempts").glob("*.json"))) == 1
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        result = await b.probe(plan, tmp_path / "head", client, "frozen")
        assert result["attempts"] == 2


def test_frozen_b1_events_have_exact_bounded_scope() -> None:
    path = Path(__file__).resolve().parents[3] / (
        "docs/research/evidence/b1-prior-only-4h-2026-11-13/read/events.json.gz"
    )
    raw = path.read_bytes()
    assert b.sha(raw) == b.EVENTS_SHA
    plan = b.scope(json.loads(gzip.decompress(raw)))
    assert plan["symbol_days"] == 724 and plan["symbols"] == 151
    assert len(plan["tasks"]) == 2172 and plan["max_attempts"] == 4344
