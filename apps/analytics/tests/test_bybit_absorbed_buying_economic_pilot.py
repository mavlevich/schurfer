from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_absorbed_buying as ab
from schurfer_analytics import bybit_absorbed_buying_economic_pilot as pilot
from schurfer_analytics import bybit_presettlement_bounded_fetch as transport
from schurfer_analytics import bybit_presettlement_economics as b3
from schurfer_analytics.bybit_compressed_spring import HOUR_MS
from schurfer_analytics.bybit_post_funding_screen import MINUTE_MS

if TYPE_CHECKING:
    from pathlib import Path

T = ab.BLOCKS[0][0] + 5 * HOUR_MS  # a January pilot decision hour
JULY = ab.BLOCKS[1][0] + 5 * HOUR_MS
ENTRY, EXIT = T + MINUTE_MS, T + MINUTE_MS + 4 * HOUR_MS
SETTLE = T + 2 * HOUR_MS


def ev(symbol: str = "AAAUSDT", t: int = T) -> dict[str, Any]:
    return {"symbol": symbol, "t": t}


def history(
    *events: dict[str, Any], rate: float = 0.0, inside: bool = True
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        t = e["t"]
        target = out.setdefault(e["symbol"], {"rates": {}, "coverage": []})
        if inside:
            target["rates"][t + 2 * HOUR_MS] = rate
        target["coverage"].append((t - HOUR_MS, t + 6 * HOUR_MS))
    return out


def body(
    request: pilot.Request,
    *,
    first: float = 100.0,
    last: float = 100.0,
    drop: tuple[int, ...] = (),
    zero_volume: tuple[int, ...] = (),
) -> bytes:
    rows = []
    for at in range(request.start, request.end, MINUTE_MS):
        if at in drop:
            continue
        o = c = 100.0
        if at == request.start:
            o = first
        if at == request.end - MINUTE_MS:
            c = last
        h, low = max(o, c) * 1.01, min(o, c) * 0.99
        row = [str(at), str(o), str(h), str(low), str(c)]
        if request.kind == "price":
            row += ["0" if at in zero_volume else "10", "1000"]
        rows.append(row)
    return json.dumps(
        {"retCode": 0, "result": {"category": "linear", "symbol": request.symbol, "list": rows}}
    ).encode()


def snapshot(
    events: list[dict[str, Any]],
    hist: dict[str, dict[str, Any]],
    bodies: dict[tuple[str, str], dict[str, Any]] | None = None,
    missing: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    records = []
    for r in pilot.requests_for(events, hist):
        if (r.symbol, r.kind) in missing:
            continue
        raw = body(r, **(bodies or {}).get((r.symbol, r.kind), {}))
        records.append(
            {
                "key": r.key,
                "request": asdict(r),
                "outcome": b3.validate(r, raw, 200, None),
                "body_base64": base64.b64encode(raw).decode(),
                "body_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {"records": records}


def rows_for(
    events: list[dict[str, Any]],
    hist: dict[str, dict[str, Any]],
    bodies: dict[tuple[str, str], dict[str, Any]] | None = None,
    missing: tuple[tuple[str, str], ...] = (),
) -> list[dict[str, Any]]:
    native = pilot.paths(snapshot(events, hist, bodies, missing))
    return [pilot.evaluate(e, native, hist) for e in events]


def test_request_windows_mark_only_with_a_settlement_and_btc_per_hour() -> None:
    a, b = ev("AAAUSDT"), ev("BBBUSDT")
    hist = {**history(a), **history(b, inside=False)}
    requests = pilot.requests_for([a, b], hist)
    assert pilot.counts(requests) == {"price": 2, "mark": 1, "btc": 1, "total": 4}
    for r in requests:
        assert (r.start, r.end) == (ENTRY, EXIT)
        assert r.params["end"] == EXIT - 1  # 240 bars: t + 1 min ... t + 4 h
        assert b3.validate(r, body(r), 200, None) == "complete"
    assert requests[0].key != b3.Request(**asdict(requests[0])).key  # own identity


def test_entry_open_and_exit_close_alignment_for_the_short() -> None:
    e = ev()
    hist = history(e)
    [row] = rows_for([e], hist, {("AAAUSDT", "price"): {"first": 100.0, "last": 99.0}})
    assert row["status"] == "complete"
    assert (row["entry_price"], row["exit_price"]) == (100.0, 99.0)
    assert row["gross_bps"] == pytest.approx(100.0)  # a falling price pays the short
    assert row["net30_bps"] == pytest.approx(70.0)
    assert row["net50_bps"] == pytest.approx(50.0)
    assert row["pre_cost_bps"] == pytest.approx(100.0)


def test_short_receives_positive_funding_strictly_inside_the_hold() -> None:
    e = ev()
    hist = history(e, rate=0.0001)
    hist["AAAUSDT"]["rates"][ENTRY] = 0.5  # at the boundary: not held
    hist["AAAUSDT"]["rates"][T - HOUR_MS] = 0.5  # before entry: not held
    [row] = rows_for([e], hist)
    assert row["settlements_held"] == [SETTLE]
    assert row["funding_bps"] == pytest.approx(1.0)  # +r x mark(s) / entry, mark open 100
    assert row["net30_bps"] == pytest.approx(-29.0)
    assert row["net30_mark_worst_bps"] < row["net30_bps"] < row["net30_mark_best_bps"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"bodies": {("AAAUSDT", "price"): {"drop": (ENTRY,)}}}, "entry_bar_missing"),
        ({"bodies": {("AAAUSDT", "price"): {"drop": (EXIT - MINUTE_MS,)}}}, "exit_bar_missing"),
        (
            {"bodies": {("AAAUSDT", "price"): {"zero_volume": (ENTRY,)}}},
            "zero_volume_execution_bar",
        ),
        ({"missing": (("AAAUSDT", "price"),)}, "price_unavailable"),
        ({"missing": (("AAAUSDT", "mark"),)}, "funding_mark_unavailable"),
        ({"bodies": {("AAAUSDT", "mark"): {"drop": (SETTLE,)}}}, "funding_mark_unavailable"),
        ({"coverage": [(T - HOUR_MS, EXIT - 1)]}, "funding_coverage_unproven"),
    ],
)
def test_completeness_reasons_are_explicit(change: dict[str, Any], reason: str) -> None:
    e = ev()
    hist = history(e, rate=0.0001)
    if "coverage" in change:
        hist["AAAUSDT"]["coverage"] = change["coverage"]
    [row] = rows_for([e], hist, change.get("bodies"), change.get("missing", ()))
    assert (row["status"], row["reason"]) == ("incomplete", reason)


def test_a_middle_gap_does_not_hide_a_resolvable_trade() -> None:
    e = ev()
    [row] = rows_for([e], history(e), {("AAAUSDT", "price"): {"drop": (ENTRY + MINUTE_MS,)}})
    assert row["status"] == "complete"


def test_no_settlement_is_zero_funding_only_after_proven_coverage() -> None:
    e = ev()
    hist = history(e, inside=False)
    [row] = rows_for([e], hist)
    assert (row["status"], row["funding_bps"], row["settlements_held"]) == ("complete", 0.0, [])
    hist["AAAUSDT"]["coverage"] = []
    [row] = rows_for([e], hist)
    assert row["reason"] == "funding_coverage_unproven"


def test_missing_btc_never_makes_net_incomplete() -> None:
    e = ev()
    [row] = rows_for([e], history(e), missing=(("BTCUSDT", "price"),))
    assert (row["status"], row["btc_status"], row["btc_reason"]) == (
        "complete",
        "incomplete",
        "btc_unavailable",
    )
    assert row["btc_adjusted_bps"] is None
    decision = pilot.decide([row])
    assert decision["verdict"] != "not_assessable"


def test_btc_adjustment_adds_btc_price_return_without_fee_or_funding() -> None:
    e = ev()
    bodies = {
        ("AAAUSDT", "price"): {"first": 100.0, "last": 99.0},
        ("BTCUSDT", "price"): {"first": 100.0, "last": 101.0},
    }
    [row] = rows_for([e], history(e), bodies)
    assert row["btc_price_return_bps"] == pytest.approx(100.0)
    assert row["btc_adjusted_bps"] == pytest.approx(70.0 + 100.0)


def cluster_row(symbol: str, value: float, t: int = T, status: str = "complete") -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "decision_ms": t,
        "entry_ms": t + MINUTE_MS,
        "exit_ms": t + MINUTE_MS + 4 * HOUR_MS,
        "cluster": f"{symbol}:{pilot.day(t)}",
        "week": pilot.week(t),
        "block": "january" if t < pilot.JULY_BLOCK_START else "july",
        "status": status,
        "reason": None if status == "complete" else "price_unavailable",
        "btc_status": "complete",
        "btc_reason": None,
        "btc_adjusted_bps": value,
    }
    for f in pilot.FIELDS:
        row[f] = value
    return row


def test_cluster_mean_weights_symbol_days_equally_and_drops_incomplete_clusters() -> None:
    rows = [
        cluster_row("AAAUSDT", 10.0),
        cluster_row("AAAUSDT", 30.0, T + HOUR_MS),
        cluster_row("BBBUSDT", 100.0),
        cluster_row("CCCUSDT", 500.0),
        cluster_row("CCCUSDT", 0.0, T + HOUR_MS, status="incomplete"),
    ]
    grouped = pilot.clusters(rows)
    assert [c["cluster"].split(":")[0] for c in grouped] == ["AAAUSDT", "BBBUSDT"]
    assert pilot.mean(grouped) == pytest.approx((20.0 + 100.0) / 2)  # no zero fill for CCC


def test_top5_removal_ranks_by_contribution_with_byte_order_ties() -> None:
    rows = [cluster_row(s, 50.0) for s in ("FFFUSDT", "EEEUSDT", "DDDUSDT", "CCCUSDT")]
    rows += [cluster_row("BBBUSDT", 50.0), cluster_row("AAAUSDT", 50.0)]
    rows += [cluster_row("ZZZUSDT", -10.0)]
    trimmed = pilot.top_removed(pilot.clusters(rows))
    assert trimmed["removed"] == ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"]
    assert trimmed["net30_bps"] == pytest.approx(20.0)  # FFF 50 and ZZZ -10, reweighted


def many(values: list[float]) -> list[dict[str, Any]]:
    return [cluster_row(f"S{i:02}USDT", v) for i, v in enumerate(values)]


def test_decision_branches_are_fixed_in_order() -> None:
    # Positive and still positive without the top five symbols.
    assert pilot.decide(many([10.0] * 20))["verdict"] == "continue_to_discovery_protocol"
    # Positive only because of five symbols.
    assert pilot.decide(many([500.0] * 5 + [-10.0] * 15))["verdict"] == "stop_spending"
    # Not positive.
    assert pilot.decide(many([0.0] * 20))["verdict"] == "stop_spending"
    # Too incomplete, even with a positive mean: events or clusters below 95%.
    rows = many([10.0] * 19)
    rows.append(cluster_row("XXXUSDT", 10.0, status="incomplete"))
    assert pilot.decide(rows)["verdict"] == "continue_to_discovery_protocol"  # exactly 95%
    rows.append(cluster_row("YYYUSDT", 10.0, status="incomplete"))
    assert pilot.decide(rows)["verdict"] == "not_assessable"


def test_summary_reports_blocks_btc_counter_and_both_standard_errors() -> None:
    rows = [cluster_row("AAAUSDT", 10.0), cluster_row("BBBUSDT", 30.0, JULY)]
    rows[1]["btc_status"], rows[1]["btc_reason"], rows[1]["btc_adjusted_bps"] = (
        "incomplete",
        "btc_bar_missing",
        None,
    )
    out = pilot.summary(rows)
    assert out["by_block"]["january"]["clusters"] == 1
    assert out["by_block"]["july"]["cluster_mean_net30_bps"] == pytest.approx(30.0)
    assert out["btc_diagnostic"]["clusters"] == 1
    assert out["btc_diagnostic"]["incomplete_reasons"] == {"btc_bar_missing": 1}
    assert out["uncertainty"]["audit_planning_standard_error_bps"] == pytest.approx(27.6, abs=0.1)
    assert out["break_even_round_trip_bps"] == pytest.approx(20.0)


def test_portfolio_orders_simultaneous_signals_by_this_study_hash() -> None:
    e1, e2 = ev("AAAUSDT"), ev("BBBUSDT")
    hist = {**history(e1), **history(e2)}
    out = pilot.report([e1, e2], snapshot([e1, e2], hist), hist)
    first = min(
        ("AAAUSDT", "BBBUSDT"),
        key=lambda s: hashlib.sha256(f"hyp036-econ-v1|{s}|{T}".encode()).hexdigest(),
    )
    decisions = out["portfolio"]["decisions"]
    assert decisions[0]["symbol"] == first
    assert [d["reason"] for d in decisions] == ["accepted", "slot_busy"]
    assert out["portfolio"]["gates_nomination"] is False
    assert out["confirms_strategy"] is False and out["opens_exam"] is False


def test_mocked_transport_seals_with_the_study_identity(tmp_path: Path) -> None:
    e = ev()
    hist = history(e, rate=0.0001)
    requests = pilot.requests_for([e], hist)
    limits = transport.Limits(
        attempts=6,
        body_bytes=pilot.CAP_BYTES,
        pilot_attempts=6,
        pilot_bytes=pilot.CAP_BYTES,
        spacing=0,
    )
    contract = {"rules": pilot.VERSION, "limits": asdict(limits)}
    root = tmp_path / "run"
    transport.prepare(root, contract, requests, [r.key for r in requests])

    def handle(request: httpx.Request) -> httpx.Response:
        kind = "mark" if request.url.path.endswith("mark-price-kline") else "price"
        r = next(r for r in requests if r.symbol == request.url.params["symbol"] and r.kind == kind)
        return httpx.Response(200, stream=httpx.ByteStream(body(r)))

    with (
        b3.run(root, contract, request_factory=pilot.Request) as state,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        assert state.fetch("pilot", client)["passed"]
    sealed = pilot.sealed(root, contract, requests)
    assert sealed["version"] == pilot.VERSION
    assert {r["key"] for r in sealed["records"]} == {r.key for r in requests}
    out = pilot.report([e], sealed, hist)
    assert out["events"][0]["status"] == "complete"
    with pytest.raises(ValueError, match="binding differs"):
        b3.run(root, {**contract, "rules": "different"}, request_factory=pilot.Request)


def test_b3a_defaults_keep_their_request_identity_and_order_tag() -> None:
    r = b3.Request("AAAUSDT", ENTRY, EXIT, "price")
    assert r.key == transport.digest(f"{b3.VERSION}:price:AAAUSDT:{ENTRY}:{EXIT}".encode())
    assert pilot.Request(**asdict(r)).key != r.key


def test_publication_guard_runs_before_any_native_io(monkeypatch: Any, tmp_path: Path) -> None:
    def refuse(paths: list[str]) -> dict[str, str]:
        assert pilot.SCOPE in paths and pilot.CONTRACT in paths and pilot.REGISTRY in paths
        raise ValueError("current revision has not been published in main")

    monkeypatch.setattr(b3, "publication", refuse)
    with pytest.raises(ValueError, match="published in main"):
        pilot.binding(tmp_path / "e", tmp_path / "i", tmp_path / "s")


def test_net_without_funding_pays_the_cost_and_is_not_gross() -> None:
    e = ev()
    hist = history(e, rate=0.0001)
    [row] = rows_for([e], hist, {("AAAUSDT", "price"): {"first": 100.0, "last": 99.0}})
    assert row["gross_bps"] == pytest.approx(100.0)
    assert row["net30_without_funding_bps"] == pytest.approx(70.0)
    out = pilot.summary([row])
    assert out["cluster_mean_net30_without_funding_bps"] == pytest.approx(
        out["cluster_mean_gross_bps"] - 30
    )
    assert "cluster_mean_net_without_funding_bps" not in out
    january = out["by_block"]["january"]
    assert january["cluster_mean_net30_without_funding_bps"] == pytest.approx(70.0)
    assert january["funding_share_of_net30"] == pytest.approx(1.0 / 71.0)
    assert out["by_block"]["july"]["funding_share_of_net30"] is None


@pytest.mark.parametrize("command", ["prepare", "fetch", "seal", "read"])
def test_cli_refuses_native_io_before_publication(
    command: str, monkeypatch: Any, tmp_path: Path
) -> None:
    def refuse(paths: list[str]) -> dict[str, str]:
        raise ValueError("current revision has not been published in main")

    monkeypatch.setattr(b3, "publication", refuse)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("network client opened"))
    root = tmp_path / "run"
    argv = [command, "--index", str(tmp_path / "missing.gz"), "--root", str(root)]
    with pytest.raises(ValueError, match="published in main"):
        pilot.main(argv)
    assert not root.exists()
