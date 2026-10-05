from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import edge_loss_gate as g
from schurfer_analytics.source_lead_multi_source_report import write_once

if TYPE_CHECKING:
    from pathlib import Path

START = g.MONTH_START.timestamp()
DAY = 86_400


def _pump_trades() -> list[tuple[float, float]]:
    """A trade every 10 s at 1.0 for 23 days, then +1% every 10 s to about 1.35, then a
    trade every 10 s at that level."""
    trades = [(START + s, 1.0) for s in range(0, 23 * DAY, 10)]
    t, price = START + 23 * DAY, 1.0
    for _ in range(30):
        t += 10
        price *= 1.01
        trades.append((t, price))
    trades += [(t + s, price) for s in range(10, 3 * DAY, 10)]
    return trades


def test_a_price_is_never_taken_from_a_later_trade_in_the_same_second() -> None:
    tape = g.Tape([(START + 60.2, 1.0), (START + 60.9, 2.0)])
    assert tape.at(60) is None  # no trade at or before 60.0
    assert tape.at(61) == 2.0 and tape.age[61] == pytest.approx(0.1)
    late = g.Tape([(START + 10, 1.0)])
    assert late.at(10 + g.MAX_PRICE_AGE_S) == 1.0
    assert late.at(11 + g.MAX_PRICE_AGE_S) is None  # stale


def test_a_gate_timeline_measures_shares_after_each_delay() -> None:
    trades = _pump_trades()
    tape = g.Tape(trades)
    cross_t = next(t for t, p in trades if p >= 1.2)
    seen = datetime.fromtimestamp(cross_t + 70, UTC)
    got = g.timeline(tape, seen)
    assert got["status"] == "ok"
    row = got["moments"]["24h_0.2"]
    assert row["scanner_lag_s"] == pytest.approx(70.0)
    shares = [row[f"after_{d}s"] for d in g.DELAYS_S]
    assert shares == sorted(shares) and 0 < shares[0] < shares[-1] <= 1
    assert all(row[f"after_{d}s_price_age_s"] < 10 for d in g.DELAYS_S)
    assert got["moments"]["5m_0.03"]["after_0s"] < row["after_0s"]


def test_a_gate_timeline_reports_censoring_and_missing_crossings() -> None:
    tape = g.Tape(_pump_trades())
    assert g.timeline(tape, g.MONTH_START + timedelta(days=1))["status"] == "insufficient_history"
    assert g.timeline(tape, g.MONTH_START + timedelta(days=10))["status"] == "no_tape_crossing"
    late = g.Tape(
        [(START + s, 1.0) for s in range(0, 30 * DAY + DAY // 2, 10)]
        + [(START + 30 * DAY + DAY // 2 + s, 1.5) for s in range(0, DAY // 2, 10)]
    )  # crosses on July 31 at noon: the 24h peak window leaves July
    assert g.timeline(late, g.MONTH_START + timedelta(days=30.6))["status"] == "censored_peak"


def test_identity_comes_from_the_stored_market_id() -> None:
    ok = {"market_id": "IDOL_USDT", "market_type": "swap", "identity_conflict": False}
    assert g.resolve_identity(ok) == ("IDOL_USDT", "ok")
    assert g.resolve_identity({**ok, "market_id": None}) == (None, "no_market_id")
    assert g.resolve_identity({**ok, "identity_conflict": True}) == (None, "identity_conflict")
    assert g.resolve_identity({**ok, "market_type": None}) == (None, "not_swap")
    assert g.resolve_identity({**ok, "market_id": "IDOL/USDT:USDT"})[1] == (
        "unexpected_market_id_form"
    )


def test_read_verifies_tapes_and_counts_sources(tmp_path: Path) -> None:
    stage, tapes_dir = tmp_path / "stage", tmp_path / "tapes"
    stage.mkdir()
    tapes_dir.mkdir()
    trades = _pump_trades()
    cross_t = next(t for t, p in trades if p >= 1.2)
    seen = datetime.fromtimestamp(cross_t + 30, UTC).isoformat()
    base = {"market_type": "swap", "identity_conflict": False, "first_seen_at": seen}
    identity = {
        "sources": [
            {**base, "event_id": 1, "symbol": "AAA/USDT:USDT", "market_id": "AAA_USDT"},
            {**base, "event_id": 2, "symbol": "BBB/USDT:USDT", "market_id": "BBB_USDT"},
            {**base, "event_id": 3, "symbol": "CCC/USDT:USDT", "market_id": None},
        ]
    }
    identity_sha = write_once(stage / g.IDENTITY_NAME, identity)
    tape = tapes_dir / "AAA_USDT-202607.csv.gz"
    lines = "".join(f"{t:.6f},{i},{p},1\n" for i, (t, p) in enumerate(trades))
    tape.write_bytes(gzip.compress(lines.encode()))
    write_once(
        stage / g.TAPES_NAME,
        {
            "identity_sha256": identity_sha,
            "files": {
                "AAA_USDT": {
                    "status": "ok",
                    "sha256": hashlib.sha256(tape.read_bytes()).hexdigest(),
                },
                "BBB_USDT": {"status": "not_in_archive"},
            },
        },
    )
    result = g.read(stage, tapes_dir)
    assert result["statuses"] == {"ok": 1, "no_tape": 1, "identity_no_market_id": 1}
    assert result["moments"]["24h_0.2"]["after_0s"]["n"] == 1
    assert json.loads((stage / g.RESULT_NAME).read_text())["scanner_sources"] == 3
    with pytest.raises(SystemExit, match="read once"):
        g.read(stage, tapes_dir)
