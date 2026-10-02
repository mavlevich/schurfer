from __future__ import annotations

import gzip
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import pre_move_source_probe as probe

if TYPE_CHECKING:
    from pathlib import Path

JULY_END = datetime(2026, 8, 1, tzinfo=UTC)
PRICE = "98765.4321"  # a sentinel price that must never reach the artifact


# --- the registered request boundary ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"{probe.GATE_API}/futures/usdt/contracts",
        f"{probe.GATE_API}/futures/usdt/contracts/BTC_USDT",
        f"{probe.GATE_API}/futures/usdt/tickers",
        f"{probe.BYBIT}/v5/market/tickers",
        f"{probe.BYBIT}/v5/market/orderbook",
        f"{probe.BLOFIN}/api/v1/market/trades",
        "https://contract.mexc.com/api/v1/contract/deals/BTC_USDT",
        f"{probe.BINANCE_FAPI}/fapi/v1/openInterest",
        f"{probe.BINANCE_FAPI}/fapi/v1/premiumIndex",
    ],
)
def test_current_data_endpoints_are_refused_even_for_history(url: str) -> None:
    with pytest.raises(probe.ProtocolViolationError, match="forbidden"):
        probe.check_request("GET", url, data_end=JULY_END)


def test_only_registered_endpoints_with_a_data_end_before_the_boundary_pass() -> None:
    assert (
        probe.check_request("GET", f"{probe.BYBIT}/v5/market/instruments-info", data_end=None)
        == "metadata"
    )
    assert (
        probe.check_request("HEAD", f"{probe.GATE_ARCHIVE}/futures_usdt/trades/x", data_end=None)
        == "archive"
    )
    assert (
        probe.check_request("GET", f"{probe.GATE_API}/futures/usdt/trades", data_end=JULY_END)
        == "historical"
    )
    with pytest.raises(probe.ProtocolViolationError, match="before boundary"):
        probe.check_request("GET", f"{probe.GATE_ARCHIVE}/futures_usdt/trades/x", data_end=None)
    with pytest.raises(probe.ProtocolViolationError, match="before the boundary"):
        probe.check_request(
            "GET",
            f"{probe.GATE_API}/futures/usdt/trades",
            data_end=JULY_END + timedelta(seconds=1),
        )
    with pytest.raises(probe.ProtocolViolationError, match="plain GET"):
        probe.check_request("GET", f"{probe.BINANCE_FAPI}/fapi/v1/exchangeInfo", data_end=JULY_END)
    with pytest.raises(probe.ProtocolViolationError, match="not in the registered protocol"):
        probe.check_request("GET", "https://example.com/data", data_end=JULY_END)
    # The boundary also keeps the unread HYP-012b holdout (from 2026-08-31) out.
    assert datetime(2026, 8, 31, tzinfo=UTC) > probe.BOUNDARY


# --- limits ------------------------------------------------------------------------------


def _prober(handler: Any, tmp_path: Path) -> tuple[probe.Prober, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return probe.Prober(client, tmp_path, sleep=sleeps.append), sleeps


META = f"{probe.BYBIT}/v5/market/instruments-info"


def test_rate_limit_stops_the_source(tmp_path: Path) -> None:
    p, _ = _prober(lambda request: httpx.Response(429), tmp_path)
    with pytest.raises(probe.SourceStoppedError, match="rate-limited"):
        p.request("bybit", "GET", META)
    with pytest.raises(probe.SourceStoppedError, match="stopped"):
        p.request("bybit", "GET", META)
    assert p.budget.stopped == {"bybit": "HTTP 429"}


def test_server_errors_retry_with_the_registered_backoff(tmp_path: Path) -> None:
    calls = iter([500, 502, 200])
    p, sleeps = _prober(lambda request: httpx.Response(next(calls), json={}), tmp_path)
    assert p.request("bybit", "GET", META).status == 200
    assert sleeps == list(probe.RETRY_BACKOFF_SECONDS)
    assert p.budget.requests["bybit"] == 3


def test_request_and_byte_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "MAX_REQUESTS_PER_SOURCE", 2)
    p, _ = _prober(lambda request: httpx.Response(200, content=b"x" * 10), tmp_path)
    p.request("bybit", "GET", META)
    p.request("bybit", "GET", META)
    with pytest.raises(probe.SourceStoppedError, match="request limit"):
        p.request("bybit", "GET", META)
    with pytest.raises(probe.ProtocolViolationError, match="over 5 bytes"):
        p.request("binance", "GET", f"{probe.BINANCE_FAPI}/fapi/v1/exchangeInfo", max_bytes=5)


# --- selection ---------------------------------------------------------------------------


def _instrument(base: str, launch: datetime, delivery: int = 0) -> dict[str, Any]:
    return {
        "baseCoin": base,
        "quoteCoin": "USDT",
        "contractType": "LinearPerpetual",
        "launchTime": str(int(launch.timestamp() * 1000)),
        "deliveryTime": str(delivery),
    }


def test_universe_and_sample_ignore_everything_but_the_catalogue() -> None:
    old = datetime(2025, 1, 1, tzinfo=UTC)
    rows = [_instrument(f"A{i}", old) for i in range(30)]
    rows += [_instrument("BTC", old), _instrument("ETH", old)]
    rows.append(_instrument("NEW", datetime(2026, 7, 2, tzinfo=UTC)))  # launched too late
    delivered = int(datetime(2026, 7, 20, tzinfo=UTC).timestamp() * 1000)
    rows.append(_instrument("GONE", old, delivered))  # delivered before August
    rows.append({**_instrument("INV", old), "quoteCoin": "USD"})
    universe = probe.bybit_universe(rows)
    assert "NEW" not in universe and "GONE" not in universe and "INV" not in universe
    chosen = probe.sample(universe)
    assert chosen == probe.sample(list(reversed(universe)))
    assert chosen[-2:] == ["BTC", "ETH"] and len(chosen) == probe.SAMPLE_SIZE + 2
    expected = sorted(
        (b for b in universe if b not in probe.ANCHORS),
        key=lambda b: hashlib.sha256((probe.SAMPLE_SALT + b).encode()).hexdigest(),
    )[: probe.SAMPLE_SIZE]
    assert chosen[:-2] == expected


# --- parsers -----------------------------------------------------------------------------


def _gate_trades(rows: list[tuple[float, int, float]]) -> bytes:
    text = "\n".join(f"{t},{i},{PRICE},{s}" for t, i, s in rows) + "\n"
    return gzip.compress(text.encode())


def test_gate_trade_archive_structure_and_hour_ids() -> None:
    hour = probe.PROBE_HOUR
    t0 = hour[0].timestamp()
    body = _gate_trades(
        [(t0 - 1, 10, 5), (t0 + 1, 11, -3), (t0 + 2, 13, 2), (hour[1].timestamp(), 14, 1)]
    )
    structure, ids = probe.gate_trades_structure(body, hour)
    assert ids == {11, 13}
    assert structure["rows"] == 4 and structure["probe_hour_rows"] == 2
    assert structure["size_sign"] == {"positive": 3, "negative": 1, "zero": 0}
    assert structure["ids"] == {"gaps": 1, "non_increasing": 0}
    assert structure["timestamp_unit"] == "seconds"
    assert PRICE not in json.dumps(structure)


def _zip(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def test_binance_archive_structures() -> None:
    agg = _zip(
        "a.csv",
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        f"1,{PRICE},1,10,11,1752580800000,true\n2,{PRICE},1,12,12,1752580800001,false\n"
        f"4,{PRICE},1,14,15,1752580800002,false\n",
    )
    s = probe.binance_agg_trades_structure(agg)
    assert s["rows"] == 3 and s["timestamp_unit"] == "milliseconds"
    assert s["agg_ids"]["gaps"] == 1 and s["underlying_trade_id_gaps"] == 1
    assert s["is_buyer_maker_values"] == {"true": 1, "false": 2}
    metrics = _zip(
        "m.csv",
        "create_time,symbol,sum_open_interest\n2026-07-15 00:00:00,X,1\n"
        "2026-07-15 00:05:00,X,2\n2026-07-15 00:10:00,X,\n",
    )
    m = probe.binance_table_structure(metrics)
    assert m["time_format"] == "iso" and m["step_seconds_top"][0] == (300, 2)
    assert m["empty_cells"] == 1
    assert PRICE not in json.dumps([s, m])


# --- end to end --------------------------------------------------------------------------


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    path = request.url.path
    old = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    if path.endswith("/instruments-info"):
        if request.url.params.get("status") != "Trading":
            return httpx.Response(200, json={"result": {"list": [], "nextPageCursor": ""}})
        bases = [f"T{i}" for i in range(20)] + ["BTC", "ETH"]
        items = [
            {
                "baseCoin": b,
                "quoteCoin": "USDT",
                "contractType": "LinearPerpetual",
                "launchTime": str(old),
                "deliveryTime": "0",
            }
            for b in bases
        ]
        return httpx.Response(200, json={"result": {"list": items, "nextPageCursor": ""}})
    if path.endswith("/exchangeInfo"):
        syms = [
            {
                "symbol": f"T{i}USDT",
                "baseAsset": f"T{i}",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": old,
            }
            for i in range(20)
        ]
        return httpx.Response(200, json={"symbols": syms})
    if path.endswith("/market/instruments"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "instId": "T1-USDT",
                        "baseCurrency": "T1",
                        "quoteCurrency": "USDT",
                        "listTime": old,
                        "state": "live",
                    }
                ]
            },
        )
    if path.endswith("/contract/detail"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "symbol": "T2_USDT",
                        "baseCoin": "T2",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "state": 0,
                    }
                ]
            },
        )
    hour0 = probe.PROBE_HOUR[0].timestamp()
    if "download.gatedata.org" in url and "/trades/" in url:
        body = _gate_trades([(hour0 + 1, 1, 2), (hour0 + 2, 2, -1)])
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body))})
        return httpx.Response(200, content=body)
    if "download.gatedata.org" in url:
        return httpx.Response(404)
    if path.endswith("/futures/usdt/trades"):
        return httpx.Response(
            200, json=[{"id": 1, "create_time": hour0 + 1, "price": PRICE, "size": 2}]
        )
    if path.endswith("/contract_stats"):
        return httpx.Response(
            200, json=[{"time": int(hour0), "open_interest": "5", "mark_price": PRICE}]
        )
    if "s3-ap-northeast-1" in url:
        prefix = request.url.params["prefix"]
        symbol = prefix.rstrip("/").rsplit("/", 1)[-1]
        keys = "".join(
            f"<Key>{prefix}{symbol}-aggTrades-2026-07-{d:02d}.zip</Key>" for d in (14, 15)
        )
        return httpx.Response(
            200,
            content=f"<ListBucketResult><IsTruncated>false</IsTruncated><Contents>{keys}</Contents></ListBucketResult>".encode(),
        )
    if "data.binance.vision" in url:
        if "aggTrades" in url:
            body = _zip("a.csv", f"1,{PRICE},1,1,1,1752580800000,true\n")
        elif "metrics" in url:
            body = _zip("m.csv", "create_time,symbol,sum_open_interest\n2026-07-15 00:00:00,X,1\n")
        else:
            body = _zip(
                "d.csv", "timestamp,percentage,depth,notional\n2026-07-15 00:00:00,-1,2,3\n"
            )
        if url.endswith(".CHECKSUM"):
            target = _handler(httpx.Request("GET", url.removesuffix(".CHECKSUM"))).content
            return httpx.Response(
                200, content=f"{hashlib.sha256(target).hexdigest()}  f.zip\n".encode()
            )
        return httpx.Response(200, content=body)
    if "public.bybit.com" in url:
        body = gzip.compress(
            f"timestamp,symbol,side,size,price\n1752580800.1,X,Buy,1,{PRICE}\n".encode()
        )
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body))})
        return httpx.Response(200, content=body)
    if path.endswith("/open-interest"):
        return httpx.Response(
            200,
            json={
                "result": {"list": [{"openInterest": PRICE, "timestamp": str(int(hour0 * 1000))}]}
            },
        )
    raise AssertionError(f"unexpected request {request.method} {url}")


def test_a_full_run_respects_the_protocol_and_keeps_no_price(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    result = probe.run(client, tmp_path / "raw", code_revision="a" * 40, working_tree_dirty=False)
    assert result["catalogue"]["universe_size"] == 22
    assert len(result["catalogue"]["sample"]) == probe.SAMPLE_SIZE + 2
    assert result["gate"]["g3"] and all(
        v["only_archive"] == 1 for v in result["gate"]["g3"].values()
    )
    assert all(v["checksum_ok"] for v in result["binance"]["b2"].values())
    assert result["budget"]["requests_total"] <= probe.MAX_REQUESTS
    assert all(
        entry["kind"] in ("metadata", "archive", "historical") for entry in result["request_log"]
    )
    artifact = json.dumps(result)
    assert PRICE not in artifact  # prices are parsed for structure only, never kept
    assert any((tmp_path / "raw").iterdir())  # raw files stay in the gitignored directory
