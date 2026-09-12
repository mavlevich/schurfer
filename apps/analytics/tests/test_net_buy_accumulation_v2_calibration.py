"""Validation for the net-buy accumulation v2 calibration tool.

Pure-logic tests for the formal-run lock, cooldown dedup and artifact fingerprint,
plus a synthetic-Parquet end-to-end test of the v2 eligibility SQL (the timely /
non-backfill guard, the B-completeness fraction, and the baseline floor) against
real DuckDB. The Parquet carries `created_at`, which the real cold-bar export has
but the curated local window subset does not.
"""

from __future__ import annotations

import dataclasses
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics.net_buy_accumulation_v2_calibration import (
    PRIMARY_MAG,
    CalibrationArtifact,
    FormalRunLockError,
    ThresholdCount,
    assert_calibration_only,
    dedup_cooldown,
    summarize_threshold,
)
from schurfer_analytics.net_buy_accumulation_v2_repository import scan_crossings


def test_formal_run_is_locked() -> None:
    assert_calibration_only(formal_run=False)  # ok
    with pytest.raises(FormalRunLockError):
        assert_calibration_only(formal_run=True)


def test_cooldown_dedup_suppresses_within_24h() -> None:
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    rows = [
        ("bybit", "AAAUSDT", t0),
        ("bybit", "AAAUSDT", t0 + timedelta(hours=1)),  # within 24h -> dropped
        ("bybit", "AAAUSDT", t0 + timedelta(hours=25)),  # kept
        ("bybit", "BBBUSDT", t0),  # different instrument -> kept
    ]
    kept = dedup_cooldown(rows)
    assert len(kept) == 3


def test_summarize_counts_assets_venues_weeks_and_concentration() -> None:
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    rows = [
        ("bybit", "AAAUSDT", t0),
        ("binance", "BBBUSDT", t0 + timedelta(days=2)),
        ("bybit", "AAAUSDT", t0 + timedelta(days=2)),  # same cluster AAA again
    ]
    tc = summarize_threshold(PRIMARY_MAG, 0.2, rows)
    assert tc.dedup_fires == 3
    assert tc.distinct_assets == 2  # AAA, BBB
    assert tc.distinct_venues == 2
    assert abs(tc.top_cluster_share - 2 / 3) < 1e-9  # AAA has 2 of 3


def test_artifact_fingerprint_is_deterministic_and_excludes_walltime() -> None:
    a = CalibrationArtifact(
        tool_version="t",
        generated_at="2026-01-01T00:00:00Z",
        code_revision="rev",
        calibration_window_start="a",
        calibration_window_end="b",
        b_completeness_min_fraction=0.99,
        max_finalization_lag_seconds=120,
        baseline_activity_floor_usd=100000.0,
        theta_m_grid=(0.1, 0.2),
        theta_s_grid=(0.1, 0.2),
        cold_bar_manifests={"m": "h"},
        counts=(ThresholdCount(PRIMARY_MAG, 0.2, 10, 5, 4, 1, 2, 0.4),),
    )
    b = dataclasses.replace(a, generated_at="2026-12-31T23:59:59Z")
    assert a.fingerprint() == b.fingerprint()  # wall clock excluded


# --- synthetic-Parquet eligibility end-to-end -------------------------------

_FIRE = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
_SERIES_START = _FIRE - timedelta(minutes=15000)
_SERIES_END = _FIRE + timedelta(minutes=2)
_STEP = _FIRE - timedelta(minutes=2000)  # net_buy steps up here
_CAL_START = _FIRE - timedelta(minutes=1500)
_CAL_END = _FIRE


def _write(
    connection: object,
    path: str,
    *,
    backfill_all_w: bool = False,
    break_b_frac: bool = False,
) -> None:
    # activity flat 2000/min -> baseline_daily = 2000*1440 = 2.88M. net_buy 0 before
    # the step, 1000/min after, so score_m ramps and crosses low thetas at an
    # eligible minute inside [_CAL_START, _CAL_END).
    # created_at: normally bucket_start + 30s (timely). backfill_all_w makes every
    # bar late (bucket + 1 day) -> timely=0 everywhere. break_b_frac marks ~3% of
    # bars incomplete -> B completeness below 0.99.
    created_expr = (
        "bucket_start + INTERVAL 1 DAY" if backfill_all_w else "bucket_start + INTERVAL 30 SECOND"
    )
    complete_expr = (
        "(CASE WHEN (epoch(ts)/60)::BIGINT % 33 = 0 THEN false ELSE true END)"
        if break_b_frac
        else "true"
    )
    # Plain template with .replace() for the structural parts (no f-string, so no
    # S608); $step/$start/$end stay DuckDB bind parameters.
    sql = """
        COPY (
            SELECT
                'bybit' AS exchange, 'TESTUSDT' AS symbol,
                'linear' AS market_type, 'v1' AS capture_version,
                ts AS bucket_start,
                CASE WHEN ts >= $step THEN 1500.0 ELSE 1000.0 END AS buy_total_notional_usd,
                CASE WHEN ts >= $step THEN 500.0 ELSE 1000.0 END AS sell_total_notional_usd,
                100.0 AS close_price,
                __COMPLETE__ AS trades_complete,
                true AS price_complete,
                ts + INTERVAL 30 SECOND AS last_trade_received_at,
                __CREATED__ AS created_at
            FROM (SELECT unnest(range($start, $end, INTERVAL 1 MINUTE)) AS ts)
        ) TO '__PATH__' (FORMAT PARQUET)
    """
    sql = (
        sql.replace("__COMPLETE__", complete_expr)
        .replace("__CREATED__", created_expr)
        .replace("__PATH__", path)
    )
    connection.execute(  # type: ignore[attr-defined]
        sql, {"step": _STEP, "start": _SERIES_START, "end": _SERIES_END}
    )


def _scan(connection: object, path: str, theta: float) -> list[tuple[str, str, datetime]]:
    return scan_crossings(
        parquet_glob=path,
        cal_start=_CAL_START,
        cal_end=_CAL_END,
        score_col="score_m",
        eligible_col="eligible_m",
        theta=theta,
        b_completeness_min_fraction=0.99,
        max_finalization_lag_seconds=120,
        connection=connection,
    )


def test_v2_p_mag_fires_when_all_bars_timely_and_complete() -> None:
    import duckdb

    con = duckdb.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars.parquet")
            _write(con, path)
            fires = _scan(con, path, 0.20)
            assert len(fires) >= 1
            assert all(f[0] == "bybit" and f[1] == "TESTUSDT" for f in fires)
    finally:
        con.close()


def test_v2_backfilled_window_bars_block_all_fires() -> None:
    import duckdb

    con = duckdb.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars.parquet")
            _write(con, path, backfill_all_w=True)  # created_at = bucket + 1 day
            fires = _scan(con, path, 0.20)
            assert fires == []  # nothing is timely -> nothing eligible
    finally:
        con.close()


def test_v2_baseline_below_fraction_blocks_fires() -> None:
    import duckdb

    con = duckdb.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars.parquet")
            _write(con, path, break_b_frac=True)  # ~3% incomplete -> below 0.99
            fires = _scan(con, path, 0.20)
            assert fires == []
    finally:
        con.close()
