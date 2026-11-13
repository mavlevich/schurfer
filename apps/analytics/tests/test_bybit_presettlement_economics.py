from __future__ import annotations

import base64
import gzip
import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_presettlement_bounded_fetch as transport
from schurfer_analytics import bybit_presettlement_economic_report as report
from schurfer_analytics import bybit_presettlement_economics as source
from schurfer_analytics.bybit_post_funding_screen import MINUTE_MS
from schurfer_analytics.bybit_presettlement_feasibility import FIRST_MS

if TYPE_CHECKING:
    from pathlib import Path

T = FIRST_MS + 24 * 60 * MINUTE_MS
D = T - 30 * MINUTE_MS


def event(symbol: str = "TESTUSDT", t: int = T) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "forecast_settlement_ms": t,
        "decision_ms": t - 30 * MINUTE_MS,
        "interval_minutes": 240,
    }


def body(request: source.Request, *, price: float = 100) -> bytes:
    rows = [
        [str(at), str(price), str(price * 1.01), str(price * 0.99), str(price)]
        + (["10", "1000"] if request.kind == "price" else [])
        for at in range(request.start, request.end, MINUTE_MS)
    ]
    return json.dumps(
        {"retCode": 0, "result": {"category": "linear", "symbol": request.symbol, "list": rows}}
    ).encode()


def dataset(e: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    e = e or event()
    requests = source.requests_for([e])
    records = []
    for request in requests:
        raw = body(request)
        records.append(
            {
                "key": request.key,
                "request": asdict(request),
                "outcome": "complete",
                "body_base64": base64.b64encode(raw).decode(),
                "body_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    t, d = e["forecast_settlement_ms"], e["decision_ms"]
    history = {e["symbol"]: {"rates": {t: -0.003}, "coverage": [(d, t + 2 * MINUTE_MS)]}}
    return {"records": records}, history


def test_native_prices_and_mark_are_different_endpoints_and_decoders() -> None:
    requests = source.requests_for([event()])
    assert len(requests) == 3
    assert len({source.endpoint(r) for r in requests}) == 2
    for r in requests:
        assert source.validate(r, body(r), 200, None) == "complete"
        assert r.params["end"] == r.end - 1
    assert len(source.requests_for([event("BTCUSDT")])) == 2  # event/BTC role dedup


@pytest.mark.parametrize(
    "mutation", ["conflict", "nan", "null", "negative", "outside", "identity", "ohlc"]
)
def test_invalid_native_values_are_fatal_not_missing(mutation: str) -> None:
    r = source.requests_for([event()])[0]
    payload = json.loads(body(r))
    rows = payload["result"]["list"]
    if mutation == "conflict":
        duplicate = list(rows[0])
        duplicate[1] = "100.5"
        rows.append(duplicate)
    elif mutation == "nan":
        rows[0][1] = "nan"
    elif mutation == "null":
        rows[0][1] = None
    elif mutation == "negative":
        rows[0][3] = "-1"
    elif mutation == "outside":
        rows[0][0] = str(r.end)
    elif mutation == "identity":
        payload["result"]["symbol"] = "OTHERUSDT"
    else:
        rows[0][2] = "50"
    assert source.validate(r, json.dumps(payload).encode(), 200, None) == "invalid"


def test_gap_is_incomplete_and_zero_volume_is_unresolved() -> None:
    snapshot, history = dataset()
    native = report.paths(snapshot)
    req = source.Request("TESTUSDT", D, T + 2 * MINUTE_MS, "price")
    payload = json.loads(body(req))
    payload["result"]["list"].pop()
    assert source.validate(req, json.dumps(payload).encode(), 200, None) == "incomplete"
    price = native[req.key]
    assert price is not None
    price[D + MINUTE_MS] = (*price[D + MINUTE_MS][:4], 0.0, 0.0)
    assert (
        report.evaluate(event(), "before", native, history)["reason"] == "zero_volume_execution_bar"
    )


def test_funding_direction_held_interval_and_cash_normalization() -> None:
    snapshot, history = dataset()
    native = report.paths(snapshot)
    before = report.evaluate(event(), "before", native, history)
    through = report.evaluate(event(), "through", native, history)
    assert before["net30_bps"] == -30
    assert through["funding_bps"] == pytest.approx(30)
    assert through["net30_bps"] == pytest.approx(0)
    assert through["funding_worst_bps"] < through["funding_bps"] < through["funding_best_bps"]
    history["TESTUSDT"]["rates"][T] = 0.003
    paying = report.evaluate(event(), "through", native, history)
    assert paying["funding_bps"] == pytest.approx(-30)
    assert paying["net30_bps"] == pytest.approx(-60)
    assert paying["funding_worst_bps"] < paying["funding_best_bps"]


def test_future_funding_not_an_entry_filter_and_missing_history_not_zero() -> None:
    snapshot, history = dataset()
    native = report.paths(snapshot)
    history["TESTUSDT"]["rates"][T] = 0.5
    assert report.evaluate(event(), "before", native, history)["status"] == "complete"
    history["TESTUSDT"]["coverage"] = [(D, T - 2 * MINUTE_MS)]
    assert (
        report.evaluate(event(), "through", native, history)["reason"] == "funding_coverage_unknown"
    )


def test_native_open_at_next_minute_not_hour_or_close() -> None:
    snapshot, history = dataset()
    native = report.paths(snapshot)
    req = source.Request("TESTUSDT", D, T + 2 * MINUTE_MS, "price")
    price = native[req.key]
    assert price is not None
    price[D + MINUTE_MS] = (80, 101, 79, 100, 10, 1000)
    result = report.evaluate(event(), "before", native, history)
    assert result["gross_bps"] == pytest.approx(2500)


def test_btc_missing_does_not_hide_core_negative_return() -> None:
    snapshot, history = dataset()
    native = report.paths(snapshot)
    native[source.Request("BTCUSDT", D, T + 2 * MINUTE_MS, "price").key] = None
    row = report.evaluate(event(), "before", native, history)
    assert row["status"] == "complete" and row["btc_adjusted_net_bps"] is None
    rows = [{**row, "cluster": f"TEST:{i}"} for i in range(150)]
    summary = report.summarize(rows, draws=100)
    assert summary["verdict"] == "fail"  # diversity, BTC and power cannot mask negative EV
    assert summary["conditions"]["symbols"] is False


def test_incomplete_cluster_is_not_averaged_over_surviving_members() -> None:
    snapshot, history = dataset()
    row = report.evaluate(event(), "before", report.paths(snapshot), history)
    bad = {**row, "status": "unresolved", "reason": "missing"}
    assert report.clusters([row, bad]) == []


def test_portfolio_hash_ties_busy_slot_and_unknown_value_are_not_cash() -> None:
    snapshot, history = dataset()
    row = report.evaluate(event(), "before", report.paths(snapshot), history)
    rows = [{**row, "symbol": s, "net30_bps": 100} for s in ("A", "B")]
    p = report.portfolio(rows)
    assert p == report.portfolio(list(reversed(rows)))
    assert p["reason_counts"] == {"accepted": 1, "slot_busy": 1}
    assert p["final_cash_usd"] == pytest.approx(50.45)
    first = {**row, "status": "unresolved", "reason": "missing"}
    later = {**row, "entry_ms": row["exit_ms"], "decision_ms": row["decision_ms"] + 60_000}
    p = report.portfolio([first, later])
    assert p["final_cash_usd"] is None
    assert p["reason_counts"] == {"selected_path_unresolved": 1, "portfolio_value_unknown": 1}
    assert not p["gates_nomination"]


def test_whole_week_bootstrap_keeps_cluster_weights_and_is_deterministic() -> None:
    values = [{"week": w, "net30_bps": 20.0} for w in report.weeks() for _ in range(2)]
    assert report.bootstrap(values, "net30_bps", draws=100) == report.bootstrap(
        values, "net30_bps", draws=100
    )
    assert report.bootstrap(values, "net30_bps", draws=100, block=2)["lower"] == 20
    unequal = [
        {"week": w, "net30_bps": 0.0 if i < 26 else 100.0}
        for i, w in enumerate(report.weeks())
        for _ in range(100 if i < 26 else 1)
    ]
    upper = report.bootstrap(unequal, "net30_bps", draws=100)["upper"]
    assert upper is not None and 1 < upper < 5  # equal week means would be near 50


def test_two_events_in_one_cluster_do_not_get_twice_the_weight() -> None:
    snapshot, history = dataset()
    row = report.evaluate(event(), "before", report.paths(snapshot), history)
    a = [{**row, "net30_bps": n} for n in (0.0, 100.0)]
    b = {**row, "cluster": "OTHER:2024-10-02", "net30_bps": 100.0}
    grouped = report.clusters([*a, b])
    assert len(grouped) == 2
    assert report.mean(grouped) == 75.0


def test_complete_mocked_transport_seals_and_refuses_binding_drift(tmp_path: Path) -> None:
    requests = source.requests_for([event()])
    limits = transport.Limits(
        attempts=6, body_bytes=source.CAP, pilot_attempts=6, pilot_bytes=source.CAP, spacing=0
    )
    contract = {"rules": source.VERSION, "limits": asdict(limits)}
    root = tmp_path / "run"
    transport.prepare(root, contract, requests, [r.key for r in requests])

    def handle(request: httpx.Request) -> httpx.Response:
        kind = "mark" if request.url.path.endswith("mark-price-kline") else "price"
        r = next(r for r in requests if r.symbol == request.url.params["symbol"] and r.kind == kind)
        return httpx.Response(200, stream=httpx.ByteStream(body(r)))

    with (
        source.run(root, contract) as run,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        assert run.fetch("pilot", client)["passed"]
    sealed = source.sealed(root, contract, requests)
    assert len(sealed["records"]) == 3
    out = report.report([event()], sealed, dataset()[1], draws=100)
    assert out["budget_verdict"] == "budget_not_assessable"
    assert out["policies"]["through"]["resolved_events"] == 1
    with pytest.raises(ValueError, match="binding differs"):
        source.run(root, {**contract, "rules": "different"})
    receipt = next((root / "attempts").glob("*.json"))
    content = json.loads(receipt.read_bytes())
    content["body_sha256"] = "corrupt"
    receipt.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="checksum differs"):
        source.run(root, contract)


def test_failed_request_retries_without_resetting_durable_byte_budget(tmp_path: Path) -> None:
    requests = source.requests_for([event()])[:1]
    limits = transport.Limits(
        attempts=2, body_bytes=1024, pilot_attempts=2, pilot_bytes=1024, spacing=0
    )
    contract = {"rules": source.VERSION, "limits": asdict(limits)}
    root = tmp_path / "run"
    transport.prepare(root, contract, requests, [r.key for r in requests])
    with (
        source.run(root, contract) as run,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=httpx.ByteStream(b"x" * 2048))
            )
        ) as client,
    ):
        with pytest.raises(transport.BudgetExceededError):
            run.fetch("pilot", client)
        assert run.totals()[1] == 1024
    with source.run(root, contract) as resumed:
        assert resumed.totals()[1] == 1024


def test_conflicting_values_between_retry_responses_cannot_be_sealed(tmp_path: Path) -> None:
    requests = source.requests_for([event()])[:1]
    r = requests[0]
    contract = {"rules": source.VERSION, "limits": asdict(transport.Limits(spacing=0))}
    root = tmp_path / "run"
    transport.prepare(root, contract, requests, [r.key])
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        data = json.loads(body(r, price=100 if calls == 1 else 101))
        if calls == 1:
            data["result"]["list"].pop()
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    with (
        source.run(root, contract) as run,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        assert run.fetch("pilot", client)["passed"]
    with pytest.raises(ValueError, match="retry values conflict"):
        source.sealed(root, contract, requests)


def test_funding_paging_gap_is_not_zero_and_conflicts_are_fatal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    p = tmp_path / "index.gz"
    records: list[dict[str, Any]] = [
        {"kind": "stage", "symbol": None, "params": {}, "response": {"stage": "discovery"}},
        {
            "kind": "catalog",
            "symbol": None,
            "params": {},
            "response": {
                "list": [
                    {"symbol": "TESTUSDT", "contractType": "LinearPerpetual", "settleCoin": "USDT"}
                ]
            },
        },
        {
            "kind": "funding",
            "symbol": "TESTUSDT",
            "params": {
                "symbol": "TESTUSDT",
                "category": "linear",
                "startTime": D,
                "endTime": T + MINUTE_MS,
                "limit": 1,
            },
            "response": {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "TESTUSDT",
                            "fundingRateTimestamp": str(T),
                            "fundingRate": "-0.003",
                        }
                    ]
                },
            },
        },
    ]

    def save() -> None:
        p.write_bytes(gzip.compress(("\n".join(json.dumps(r) for r in records)).encode()))
        monkeypatch.setattr(report, "INDEX_SHA256", hashlib.sha256(p.read_bytes()).hexdigest())

    save()
    history = report.funding(p)
    assert history["TESTUSDT"]["coverage"] == [(T, T + MINUTE_MS)]
    snapshot, _ = dataset()
    assert (
        report.evaluate(event(), "through", report.paths(snapshot), history)["status"]
        == "unresolved"
    )
    records.append(json.loads(json.dumps(records[-1])))
    records[-1]["response"]["result"]["list"][0]["fundingRate"] = "0.001"
    save()
    with pytest.raises(ValueError, match="conflicting funding"):
        report.funding(p)


def test_publication_guard_blocks_unmerged_code_before_native_io(monkeypatch: Any) -> None:
    monkeypatch.setattr(transport, "clean_revision", lambda: "clean-fixture")
    monkeypatch.setattr(
        "schurfer_analytics.bybit_presettlement_economics.shutil.which", lambda _: "git"
    )
    monkeypatch.setattr(
        "schurfer_analytics.bybit_presettlement_economics.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1),
    )
    with pytest.raises(ValueError, match="not been published"):
        source.publication([])


def test_whole_report_budget_stop_includes_funding_and_never_proves_maker_ev() -> None:
    events, records, rates = [], {}, {}
    for i in range(150):
        e = event(t=T + i * 24 * 60 * MINUTE_MS)
        events.append(e)
        snapshot, _ = dataset(e)
        records.update({r["key"]: r for r in snapshot["records"]})
        rates[e["forecast_settlement_ms"]] = 0.0
    history = {"TESTUSDT": {"coverage": [(D, T + 151 * 24 * 60 * MINUTE_MS)], "rates": rates}}
    snapshot = {"records": list(records.values())}
    out = report.report(events, snapshot, history, draws=100)
    assert out["budget_verdict"] == "track_b_paused_budget"
    assert out["budget_is_not_maker_ev_bound"]
    assert out["policies"]["through"]["verdict"] == "fail"
    for at in rates:
        rates[at] = -0.003
    out = report.report(events, snapshot, history, draws=100)
    assert out["budget_verdict"] == "maker_source_review_needed"
    assert out["policies"]["before"]["budget_upper_pre_cost_best_bps"] == 0
    assert out["policies"]["through"]["budget_upper_pre_cost_best_bps"] > 30
    assert not out["promotes_to_live"] and not out["opens_exam"]
