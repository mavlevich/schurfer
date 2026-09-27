from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import mexc_early_trigger_hyp029 as h
from schurfer_analytics import source_lead_multi_source_report as r
from schurfer_analytics.source_lead_multi_source import BybitInstrument

if TYPE_CHECKING:
    from pathlib import Path

LO = int(h.WINDOW_START.timestamp())
NOW = datetime(2026, 9, 29, 2, tzinfo=UTC)


def _minutes(start: int, n: int, **at: dict[str, float]) -> list[dict[str, float]]:
    out = []
    for k in range(n):
        m: dict[str, float] = {"t": start + k * 60, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}
        m["a"] = 2_000.0
        m.update(at.get(str(k), {}))
        out.append(m)
    return out


def test_five_minute_bars_need_all_five_minutes() -> None:
    minutes = _minutes(LO, 10, **{"1": {"h": 1.3}, "4": {"c": 1.1}})
    bars = h.five_minute_bars(minutes)
    assert [b["t"] for b in bars] == [LO, LO + 300]
    assert (bars[0]["o"], bars[0]["h"], bars[0]["c"], bars[0]["a"]) == (1.0, 1.3, 1.1, 10_000.0)
    assert [b["t"] for b in h.five_minute_bars(minutes[:4] + minutes[5:])] == [LO + 300]


def _bars5(
    n: int, spike: int, ret: float = 0.09, turnover: float = 100_000.0
) -> list[dict[str, float]]:
    start = LO - h.DAY_BARS * h.BAR
    bars = []
    for i in range(n):
        b: dict[str, float] = {"t": start + i * h.BAR, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}
        b["a"] = 10_000.0
        if i == spike:
            b.update({"c": 1.0 + ret, "h": 1.0 + ret, "a": turnover})
        bars.append(b)
    return bars


SPIKE = h.DAY_BARS + 5


def test_only_the_registered_rule_triggers() -> None:
    [t] = h.triggers_for("AAA_USDT", _bars5(h.DAY_BARS * 3, SPIKE))
    assert t["close_t"] == LO + 5 * h.BAR + h.BAR
    assert h.triggers_for("AAA_USDT", _bars5(h.DAY_BARS * 3, SPIKE, ret=0.07)) == []
    assert h.triggers_for("AAA_USDT", _bars5(h.DAY_BARS * 3, SPIKE, turnover=30_000.0)) == []


def test_no_exit_bar_reaches_the_blind_end() -> None:
    hi = int(h.BLIND_END.timestamp())
    n_to_end = (hi - (LO - h.DAY_BARS * h.BAR)) // h.BAR
    # the latest allowed trigger closes at hi - (sensitivity delay + hold)
    latest_close = hi - (h.SENSITIVITY_DELAY + h.HOLD_MINUTES * 60)
    latest_index = (latest_close - h.BAR - (LO - h.DAY_BARS * h.BAR)) // h.BAR
    ok = h.triggers_for("AAA_USDT", _bars5(n_to_end, latest_index))
    late = h.triggers_for("AAA_USDT", _bars5(n_to_end, latest_index + 1))
    assert len(ok) == 1 and late == []


def _inst(native: str, base: str, launch: int = 0, delivery: int = 0) -> BybitInstrument:
    return BybitInstrument(native, base, launch, delivery)


def test_the_route_is_one_live_perp_known_at_the_signal() -> None:
    trig = {"symbol": "AAA_USDT", "close_t": LO + 3600}
    assert h.route_for(trig, [_inst("AAAUSDT", "AAA")]) == ("AAAUSDT", "route")
    assert h.route_for(trig, [])[1] == "no_bybit_perp"
    two = [_inst("AAAUSDT", "AAA"), _inst("AAA2USDT", "AAA")]
    assert h.route_for(trig, two)[1] == "ambiguous_bybit_route"
    # Known at the signal: trading at the entry is enough; a later delisting is not a
    # selection criterion (the reviewer's case: it closes 30 minutes into the hold).
    delisted_mid_hold = [_inst("AAAUSDT", "AAA", delivery=(LO + 3600 + 1800) * 1000)]
    assert h.route_for(trig, delisted_mid_hold) == ("AAAUSDT", "route")
    not_yet_listed = [_inst("AAAUSDT", "AAA", launch=(LO + 7200) * 1000)]
    assert h.route_for(trig, not_yet_listed)[1] == "no_bybit_perp"


def test_a_delisting_during_the_hold_stays_in_the_denominator_as_unresolved() -> None:
    close_t = LO + 3600
    leg = _leg(close_t, delivery_ms=(close_t + 1800) * 1000)
    assert h.leg_return(leg, h.ENTRY_DELAY) == (None, "delisted_during_hold")
    payload = h.compute_result(
        {"legs": [leg, _leg(close_t + 86_400)], "code_revision": "x"},
        "sha",
        {"contract_sha256": "c", "claimed_at": "t", "reader_code_revision": "r"},
    )
    assert payload["leg_status"] == {"delisted_during_hold": 1, "leg": 1}
    assert payload["unresolved_fraction"] == pytest.approx(0.5)


def _leg(close_t: int, symbol: str = "AAA_USDT", move: float = 2.0, **kw: Any) -> dict[str, Any]:
    entry_ms = (close_t + h.ENTRY_DELAY) * 1000
    rows = []
    for k in range(h.HOLD_MINUTES + 2):
        ts = entry_ms + k * 60_000
        price = 1.0 + move / 100 if k == h.HOLD_MINUTES - 1 else 1.0
        rows.append([str(ts), "1.0", "1.0", "1.0", str(price), "1"])
    leg = {
        "symbol": symbol,
        "bar_t": close_t - 300,
        "close_t": close_t,
        "mexc_close": 1.0,
        "route": "AAAUSDT",
        "route_status": "route",
        "bybit_bars": rows,
    }
    leg.update(kw)
    return leg


def test_leg_return_enters_one_minute_after_the_close_and_holds_an_hour() -> None:
    gross, status = h.leg_return(_leg(LO + 3600), h.ENTRY_DELAY)
    assert status == "leg" and gross == pytest.approx(2.0)
    assert h.leg_return(_leg(LO + 3600, bybit_bars=None), h.ENTRY_DELAY)[1] == "bybit_fetch_failed"
    assert h.leg_return(_leg(LO + 3600, mexc_close=5.0), h.ENTRY_DELAY)[1] == "price_level_mismatch"
    assert h.leg_return(_leg(LO + 3600, route=None, route_status="no_bybit_perp"), 60)[1] == (
        "no_bybit_perp"
    )
    short = _leg(LO + 3600)
    short["bybit_bars"] = short["bybit_bars"][:10]
    assert h.leg_return(short, h.ENTRY_DELAY)[1] == "missing_exit_bar"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((40, 25, 0.3, 0.0, 1.0, 0.2), "candidate"),
        ((40, 25, 0.3, 0.0, 1.0, -0.1), "fail"),  # CI lower bound not above zero
        ((40, 25, 0.3, 0.0, -0.5, -1.0), "fail"),  # mature and negative
        ((40, 10, 0.3, 0.0, -0.5, -1.0), "fail"),  # mature negative even below the asset floor
        ((20, 15, 0.3, 0.0, 1.0, 0.2), "insufficient_data"),
        ((40, 25, 0.5, 0.0, 1.0, 0.2), "insufficient_data"),  # one week dominates
        ((40, 25, 0.3, 0.2, 1.0, 0.2), "insufficient_data"),  # too many unresolved legs
        ((0, 0, None, 1.0, None, None), "insufficient_data"),
        ((30, 1, 0.4, 0.0, -0.5, None), "fail"),  # the reviewer's case: no CI, still a fail
    ],
)
def test_the_verdict_order(args: tuple[Any, ...], expected: str) -> None:
    assert h.verdict(*args) == expected


def _stage(tmp_path: Path, legs: list[dict[str, Any]]) -> Path:
    stage = tmp_path / "hyp029"
    stage.mkdir()
    r.write_once(
        stage / r.INPUTS_NAME,
        {"family_version": h.FAMILY_VERSION, "code_revision": "prep", "legs": legs},
    )
    return stage


def test_the_read_is_claimed_once_pins_contract_and_reader_and_waits_for_maturity(
    tmp_path: Path,
) -> None:
    legs = [
        _leg(LO + 86_400 * (k % 20) + 3600, symbol=f"S{k % 25}_USDT", move=2.0) for k in range(40)
    ]
    stage = _stage(tmp_path, legs)
    with pytest.raises(ValueError, match="opens at"):
        h.read(stage, datetime(2026, 9, 29, 0, 30, tzinfo=UTC), "rev", dirty=False)
    payload = h.read(stage, NOW, "rev", dirty=False)
    assert payload["legs"] == 40 and payload["assets"] == 25
    assert payload["net_mean_pct_primary_cost"] == pytest.approx(2.0 - h.COST_PRIMARY_PCT)
    claim = json.loads((stage / r.CLAIM_NAME).read_text())
    assert claim["contract_sha256"] == h.contract_sha256()
    assert claim["reader_code_revision"] == "rev"
    with pytest.raises(r.AlreadyReadError):
        h.read(stage, NOW, "rev", dirty=False)


def test_a_resume_with_another_reader_is_refused(tmp_path: Path) -> None:
    stage = _stage(tmp_path, [_leg(LO + 3600)])
    h.read(stage, NOW, "rev", dirty=False)
    (stage / r.RESULT_NAME).unlink()
    (stage / f"{r.RESULT_NAME}.sha256").unlink()
    with pytest.raises(ValueError, match="another reader_code_revision"):
        h.read(stage, NOW, "other", dirty=False)


def _archive(tmp_path: Path) -> Path:
    from schurfer_analytics import mexc_kline_archive as a

    target = tmp_path / "Min1"
    target.mkdir()
    lo = LO - h.DAY_BARS * h.BAR
    minutes = []
    for k in range((int(h.BLIND_END.timestamp()) - lo) // 60):
        t = lo + k * 60
        m: dict[str, float] = {"t": t, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "a": 2_000.0}
        if LO + 3600 <= t < LO + 3600 + 300:  # one 5m bar: +9% on heavy turnover
            m.update({"c": 1.09 if t == LO + 3600 + 240 else 1.0, "h": 1.09, "a": 60_000.0})
        minutes.append(m)
    digest = a.write_once(target / "AAA_USDT.jsonl.gz", minutes)
    manifest = {
        "interval": "Min1",
        "window": ["2026-08-28T00:00:00+00:00", "2026-09-29T00:00:00+00:00"],
        "symbols": {"AAA_USDT": {"status": "complete", "sha256": digest}},
    }
    (target / "manifest.json").write_text(json.dumps(manifest))
    return target


def _catalogue() -> list[dict[str, Any]]:
    return [{"symbol": "AAAUSDT", "baseCoin": "AAA", "launchTime": "0", "deliveryTime": "0"}]


def test_prepare_aborts_without_publishing_when_bybit_keeps_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def catalogue() -> list[dict[str, Any]]:
        return _catalogue()

    async def broken(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("bybit retCode=10006 too many visits")  # after the retries

    monkeypatch.setattr(h, "fetch_bybit_instruments", catalogue)
    monkeypatch.setattr(h, "_get_json", broken)
    with pytest.raises(RuntimeError, match="10006"):
        asyncio.run(h.prepare_inputs(_archive(tmp_path), NOW, "rev"))


def test_prepare_freezes_the_route_and_the_bars_after_a_retried_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    calls = {"n": 0}

    async def catalogue() -> list[dict[str, Any]]:
        return _catalogue()

    async def flaky(_client: Any, _url: str, params: dict[str, str]) -> dict[str, Any]:
        calls["n"] += 1
        start = int(params["start"])
        rows = [[str(start + k * 60_000), "1", "1", "1", "1.02", "1"] for k in range(70)]
        return {"retCode": 0, "result": {"list": list(reversed(rows))}}

    monkeypatch.setattr(h, "fetch_bybit_instruments", catalogue)
    monkeypatch.setattr(h, "_get_json", flaky)
    inputs = asyncio.run(h.prepare_inputs(_archive(tmp_path), NOW, "rev"))
    [leg] = inputs["legs"]
    assert leg["route"] == "AAAUSDT" and leg["delivery_ms"] == 0
    assert [int(r[0]) for r in leg["bybit_bars"]] == sorted(int(r[0]) for r in leg["bybit_bars"])
    assert inputs["bybit_catalogue"] == _catalogue()
    assert calls["n"] == 1


def test_the_formal_phases_need_an_explicit_clean_head(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    with pytest.raises(SystemExit, match="required"):
        h.verified_revision(None)

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        out = "abc123\n" if "rev-parse" in argv else fake_run.status  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    fake_run.status = ""  # type: ignore[attr-defined]
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="is not HEAD"):
        h.verified_revision("unknown")
    assert h.verified_revision("abc123") == "abc123"
    fake_run.status = " M file.py\n"  # type: ignore[attr-defined]
    with pytest.raises(SystemExit, match="not clean"):
        h.verified_revision("abc123")
