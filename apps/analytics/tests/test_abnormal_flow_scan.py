"""Tests for the outcome-blind abnormal-flow calibration scan command.

Exercises the whole path on synthetic cold-bar Parquet + manifests + provenance + a
point-in-time identity snapshot: inventory/window selection, memory-bounded streaming,
the artifact (README + manifest + JSON), and gap-stop. Nothing reads a forward return.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from schurfer_analytics.abnormal_flow_replay import MinuteBar
from schurfer_analytics.abnormal_flow_scan import inventory_window, run_scan
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract
from schurfer_analytics.cold_bar_export import (
    EXPORT_VERSION,
    SCHEMA_VERSION,
    SOURCE_TABLE,
    sha256_file,
)

_START = date(2026, 8, 14)


def _bars_for_day(day: date, minute_offset: int) -> list[MinuteBar]:
    """One instrument's bars for a single UTC day; ``minute_offset`` continues the OI /
    price ramp across days so windows join cleanly at midnight."""
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    bars: list[MinuteBar] = []
    for i in range(1440):
        bucket = day_start + timedelta(minutes=i)
        g = minute_offset + i
        price = 100.0 * (1 + 0.00002 * g)
        oi = 1000.0 + g
        bars.append(
            MinuteBar(
                exchange="bybit",
                market_type="linear",
                native_market_id="ZUSDT",
                capture_version="v1",
                symbol="ZUSDT",
                bucket_start=bucket,
                created_at=bucket + timedelta(seconds=30),
                open_price=price,
                high_price=price * 1.0005,
                low_price=price * 0.9995,
                close_price=price,
                buy_notional_usd=700.0,
                sell_notional_usd=300.0,
                open_interest=oi,
                open_interest_value=oi * price,
                open_interest_observed_at=bucket,
                last_trade_received_at=bucket + timedelta(seconds=20),
                price_complete=True,
                trades_complete=True,
                open_interest_complete=True,
            )
        )
    return bars


def _write_parquet(path: str, bars: list[MinuteBar]) -> None:
    import duckdb

    con = duckdb.connect()
    try:
        con.execute(
            """CREATE TABLE bars(
                exchange VARCHAR, market_type VARCHAR, symbol VARCHAR, capture_version VARCHAR,
                bucket_start TIMESTAMPTZ, created_at TIMESTAMPTZ,
                open_price DOUBLE, high_price DOUBLE, low_price DOUBLE, close_price DOUBLE,
                buy_total_notional_usd DOUBLE, sell_total_notional_usd DOUBLE,
                open_interest DOUBLE, open_interest_value DOUBLE,
                open_interest_observed_at TIMESTAMPTZ, last_trade_received_at TIMESTAMPTZ,
                price_complete BOOLEAN, trades_complete BOOLEAN, open_interest_complete BOOLEAN,
                open_interest_event_at TIMESTAMPTZ, last_bid_price DOUBLE, last_ask_price DOUBLE)"""
        )
        con.executemany(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    b.exchange,
                    b.market_type,
                    b.symbol,
                    b.capture_version,
                    b.bucket_start,
                    b.created_at,
                    b.open_price,
                    b.high_price,
                    b.low_price,
                    b.close_price,
                    b.buy_notional_usd,
                    b.sell_notional_usd,
                    b.open_interest,
                    b.open_interest_value,
                    b.open_interest_observed_at,
                    b.last_trade_received_at,
                    b.price_complete,
                    b.trades_complete,
                    b.open_interest_complete,
                    b.open_interest_observed_at,  # open_interest_event_at (event == observed here)
                    (b.open_price or 0.0) * 0.999,  # last_bid_price
                    (b.open_price or 0.0) * 1.001,  # last_ask_price
                )
                for b in bars
            ],
        )
        con.execute(f"COPY bars TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_cold_day(directory, day: date, bars: list[MinuteBar]) -> None:  # type: ignore[no-untyped-def]
    parquet = directory / f"bars-{day.isoformat()}.parquet"
    _write_parquet(str(parquet), bars)
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    fp = f"cbfp_{day.isoformat()}"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "export_version": EXPORT_VERSION,
        "source_table": SOURCE_TABLE,
        "day": day.isoformat(),
        "bucket_start_from": day_start.isoformat(),
        "bucket_start_until": (day_start + timedelta(days=1)).isoformat(),
        "row_count": len(bars),
        "file_name": parquet.name,
        "file_bytes": parquet.stat().st_size,
        "sha256": sha256_file(parquet),
        "data_keys": [],
        "exported_at": day_start.isoformat(),
        "source_fingerprint": fp,
        "file_fingerprint": fp,
        "fidelity_verified": True,
    }
    (directory / f"bars-{day.isoformat()}.manifest.json").write_text(json.dumps(manifest))
    provenance = {
        "day": day.isoformat(),
        "borg_archive": f"bars-{day.isoformat()}T04:30:00",
        "archive_member_path": f"runtime/cold-bars/{parquet.name}",
        "status": "provable",
    }
    (directory / f"bars-{day.isoformat()}.provenance.json").write_text(json.dumps(provenance))


def _ok_verifier(day: date, manifest) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Stub archive verifier: pretends the Borg member SHA matched the manifest."""
    return True, f"bars-{day.isoformat()}T04:30:00"


def _verifier_failing_on(bad_day: date):  # type: ignore[no-untyped-def]
    def verify(day: date, manifest) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
        return (day != bad_day, f"bars-{day.isoformat()}T04:30:00")

    return verify


def _identity_snapshot(path) -> None:  # type: ignore[no-untyped-def]
    path.write_text(
        json.dumps(
            [
                {
                    "exchange": "bybit",
                    "market_type": "linear",
                    "native_market_id": "ZUSDT",
                    "valid_from": "2026-08-01T00:00:00+00:00",
                    "valid_to": None,
                    "canonical_asset": "Z",
                }
            ]
        )
    )


def _contract() -> AbnormalFlowContract:
    return AbnormalFlowContract(
        min_oi_growth_pct=5.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.1,
        min_oi_notional_usd=50_000.0,
        position_usd=300.0,
        max_participation_frac=0.1,
        entry_execution_window_minutes=5,
        oi_freshness_limit_seconds_bybit=120,
        oi_freshness_limit_seconds_binance=300,
        scan_lag_minutes=2,
    )


def test_scan_produces_a_full_outcome_blind_artifact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cold = tmp_path / "cold"
    cold.mkdir()
    _write_cold_day(cold, _START, _bars_for_day(_START, 0))
    _write_cold_day(cold, date(2026, 8, 15), _bars_for_day(date(2026, 8, 15), 1440))
    snapshot = tmp_path / "identity.json"
    _identity_snapshot(snapshot)

    artifact_dir = run_scan(
        cold_bars_dir=cold,
        provenance_dir=cold,
        identity_snapshot=snapshot,
        contract=_contract(),
        out_root=tmp_path / "evidence",
        start=_START,
        end=date(2026, 8, 15),
        verify_archive=_ok_verifier,
        run_id="testrun",
    )

    assert (artifact_dir / "README.md").exists()
    manifest = json.loads((artifact_dir / "manifest.json").read_text())
    assert manifest["gap"] is None
    assert manifest["chosen_end_day"] == "2026-08-15"
    assert len(manifest["days"]) == 2
    assert manifest["days"][0]["borg_archive"].startswith("bars-2026-08-14T")
    assert manifest["days"][0]["archive_verified"] is True

    scan = json.loads((artifact_dir / "scan.json").read_text())
    assert scan["stopped"] is False
    assert scan["counts"]["eligible"] > 0
    assert scan["primary_episodes"] >= 1
    assert scan["distinct_eligible_assets"] == 1  # only canonical "Z"
    assert scan["per_venue_available_decisions"]["bybit"] > 0
    assert sum(scan["distributions"]["buy_pressure"]["counts"]) > 0
    # The deterministic freeze proposal is present and non-circular.
    freeze = scan["proposed_freeze"]["proposed_thresholds"]
    assert freeze["min_oi_notional_usd"] is not None
    assert freeze["min_oi_growth_pct"] is not None


def test_scan_stops_and_records_a_gap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cold = tmp_path / "cold"
    cold.mkdir()
    _write_cold_day(cold, _START, _bars_for_day(_START, 0))
    # 2026-08-15 is missing entirely -> a gap right after the first day.
    snapshot = tmp_path / "identity.json"
    _identity_snapshot(snapshot)

    artifact_dir = run_scan(
        cold_bars_dir=cold,
        provenance_dir=cold,
        identity_snapshot=snapshot,
        contract=_contract(),
        out_root=tmp_path / "evidence",
        start=_START,
        end=date(2026, 8, 15),  # 08-15 is missing entirely -> a real gap inside the window
        verify_archive=_ok_verifier,
        run_id="gaprun",
    )
    manifest = json.loads((artifact_dir / "manifest.json").read_text())
    assert manifest["chosen_end_day"] == "2026-08-14"
    assert manifest["gap"] is not None and "2026-08-15" in manifest["gap"]
    scan = json.loads((artifact_dir / "scan.json").read_text())
    assert scan["stopped"] is True


def test_inventory_stops_when_archive_unverified(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cold = tmp_path / "cold"
    cold.mkdir()
    _write_cold_day(cold, _START, _bars_for_day(_START, 0))
    # Day 2 is present and fidelity-verified, but its Borg archive SHA does not verify.
    _write_cold_day(cold, date(2026, 8, 15), _bars_for_day(date(2026, 8, 15), 1440))
    inventory, gap, chosen_end = inventory_window(
        cold,
        start=_START,
        end=date(2026, 8, 15),
        verify_archive=_verifier_failing_on(date(2026, 8, 15)),
    )
    # The window ends at the last fully-verified day; the unverified day is a gap.
    assert chosen_end == _START
    assert gap is not None and "2026-08-15" in gap
    assert len(inventory) == 2
    assert inventory[1].archive_verified is False
