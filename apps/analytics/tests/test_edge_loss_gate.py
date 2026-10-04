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
    """Flat at 1.0 for 23 days, then +1% every 30 s up to about 1.35, then flat."""
    trades = [(START + s, 1.0) for s in range(0, 23 * DAY, 600)]
    t, price = START + 23 * DAY, 1.0
    for _ in range(30):
        t += 30
        price *= 1.01
        trades.append((t, price))
    trades += [(t + s, price) for s in range(600, 3 * DAY, 600)]
    return trades


def test_a_gate_timeline_measures_shares_after_each_delay() -> None:
    tape = g.Tape(_pump_trades())
    cross_t = next(t for t, p in _pump_trades() if p >= 1.2)
    seen = datetime.fromtimestamp(cross_t + 70, UTC)
    got = g.timeline(tape, seen)
    assert got["status"] == "ok"
    row = got["moments"]["24h_0.2"]
    assert row["scanner_lag_s"] == pytest.approx(70.0)
    shares = [row[f"after_{d}s"] for d in g.DELAYS_S]
    assert shares == sorted(shares) and 0 < shares[0] < shares[-1] <= 1
    assert got["moments"]["5m_0.03"]["after_0s"] < row["after_0s"]


def test_a_gate_timeline_reports_censoring_and_missing_crossings() -> None:
    tape = g.Tape(_pump_trades())
    early = g.MONTH_START + timedelta(days=1)
    assert g.timeline(tape, early)["status"] == "insufficient_history"
    flat = g.MONTH_START + timedelta(days=10)
    assert g.timeline(tape, flat)["status"] == "no_tape_crossing"
    late = g.Tape([(START + s, 1.0) for s in range(0, 30 * DAY, 600)] + [(START + 30.5 * DAY, 1.5)])
    assert g.timeline(late, g.MONTH_START + timedelta(days=30.6))["status"] == "censored_peak"


def test_read_verifies_tapes_and_counts_sources(tmp_path: Path) -> None:
    stage, tapes_dir = tmp_path / "stage", tmp_path / "tapes"
    stage.mkdir()
    tapes_dir.mkdir()
    cross_t = next(t for t, p in _pump_trades() if p >= 1.2)
    seen = datetime.fromtimestamp(cross_t + 30, UTC)
    scanner = {
        "sources": [
            {
                "exchange": "gate",
                "symbol": "AAA/USDT:USDT",
                "event_id": 1,
                "first_seen_at": seen.isoformat(),
            },
            {
                "exchange": "gate",
                "symbol": "BBB/USDT:USDT",
                "event_id": 2,
                "first_seen_at": seen.isoformat(),
            },
            {
                "exchange": "mexc",
                "symbol": "AAA_USDT",
                "event_id": 1,
                "first_seen_at": seen.isoformat(),
            },
        ]
    }
    scanner_sha = write_once(stage / g.SCANNER_NAME, scanner)
    tape = tapes_dir / "AAA_USDT-202607.csv.gz"
    lines = "".join(f"{t:.6f},{i},{p},1\n" for i, (t, p) in enumerate(_pump_trades()))
    tape.write_bytes(gzip.compress(lines.encode()))
    write_once(
        stage / g.TAPES_NAME,
        {
            "scanner_sha256": scanner_sha,
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
    assert result["statuses"] == {"ok": 1, "no_tape": 1}
    assert result["moments"]["24h_0.2"]["after_0s"]["n"] == 1
    assert json.loads((stage / g.RESULT_NAME).read_text())["scanner_sources"] == 2
    assert g.native_symbol("IDOL/USDT:USDT") == "IDOL_USDT"
    assert g.native_symbol("IDOL_USDT") is None
    with pytest.raises(SystemExit, match="read once"):
        g.read(stage, tapes_dir)
