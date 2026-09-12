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
    PrimarySelection,
    ThresholdCount,
    assert_calibration_only,
    decide_window,
    dedup_cooldown,
    n_target,
    select_primary,
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
    shape_mode: bool = False,
) -> None:
    # P-MAG mode (default): activity flat 2000/min, net_buy 0 before the step and
    # 1000/min after, so score_m ramps and crosses low thetas.
    # shape_mode: activity 1000/min (net 200) before the step and 3000/min (net
    # 1000) after, so post-step minutes are above their trailing-7d mean and
    # elevated_buy -> score_s ramps and crosses low thetas.
    # created_at: normally bucket_start + 30s (timely). backfill_all_w makes every
    # bar late (bucket + 1 day). break_b_frac marks ~3% incomplete (B below 0.99).
    if shape_mode:
        buy_expr = "CASE WHEN ts >= $step THEN 2000.0 ELSE 600.0 END"
        sell_expr = "CASE WHEN ts >= $step THEN 1000.0 ELSE 400.0 END"
    else:
        buy_expr = "CASE WHEN ts >= $step THEN 1500.0 ELSE 1000.0 END"
        sell_expr = "CASE WHEN ts >= $step THEN 500.0 ELSE 1000.0 END"
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
                __BUY__ AS buy_total_notional_usd,
                __SELL__ AS sell_total_notional_usd,
                100.0 AS close_price,
                __COMPLETE__ AS trades_complete,
                true AS price_complete,
                ts + INTERVAL 30 SECOND AS last_trade_received_at,
                __CREATED__ AS created_at
            FROM (SELECT unnest(range($start, $end, INTERVAL 1 MINUTE)) AS ts)
        ) TO '__PATH__' (FORMAT PARQUET)
    """
    sql = (
        sql.replace("__BUY__", buy_expr)
        .replace("__SELL__", sell_expr)
        .replace("__COMPLETE__", complete_expr)
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


def test_v2_p_shape_fires_on_elevated_breadth() -> None:
    # In shape_mode the post-step minutes sit above their trailing-7d mean, so
    # elevated_buy breadth (score_s) ramps and crosses a low theta. This exercises
    # the P-SHAPE eligibility path (nested trailing-window completeness).
    import duckdb

    con = duckdb.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars.parquet")
            _write(con, path, shape_mode=True)
            # score_s ramps fast (each post-step minute adds breadth), so its low-
            # theta crossing lands earlier than P-MAG's; widen the window to catch it.
            fires = scan_crossings(
                parquet_glob=path,
                cal_start=_FIRE - timedelta(minutes=1900),
                cal_end=_CAL_END,
                score_col="score_s",
                eligible_col="eligible_s",
                theta=0.10,
                b_completeness_min_fraction=0.99,
                max_finalization_lag_seconds=120,
                connection=con,
            )
            assert len(fires) >= 1
    finally:
        con.close()


# --- deterministic calibration algorithm ------------------------------------


def _tc(
    primary: str, theta: float, fires: int, *, assets: int = 50, weeks: int = 4
) -> ThresholdCount:
    return ThresholdCount(primary, theta, fires, fires, assets, min(assets, 2), weeks, 0.05)


def test_n_target_grosses_up_for_unresolved_and_margin() -> None:
    # 100 resolved / (1 - 0.05) * 1.5 = 157.9...
    t = n_target(expected_unresolved_rate=0.05, sizing_margin=1.5)
    assert abs(t - (100 / 0.95 * 1.5)) < 1e-9


def test_select_primary_picks_most_selective_that_clears() -> None:
    by_theta = {
        0.10: _tc("P-MAG", 0.10, 400),
        0.20: _tc("P-MAG", 0.20, 200),
        0.30: _tc("P-MAG", 0.30, 50),
    }
    sel = select_primary(
        PRIMARY_MAG, by_theta, calibration_days=10.0, n_target_fires=150.0, max_window_days=100.0
    )
    # 0.30: 5/day -> 30 days (<=100). Most selective that clears within 100 days = 0.30.
    assert sel.chosen_theta == 0.30
    assert not sel.too_slow
    assert sel.required_window_days is not None and abs(sel.required_window_days - 30.0) < 1e-9


def test_select_primary_requires_diversity_floor() -> None:
    # 0.30 has the most fires reaching target but only 20 clusters (< 30); it is
    # disqualified, so the diversity-gated pick falls to 0.20 (50 clusters).
    by_theta = {
        0.20: _tc("P-MAG", 0.20, 200, assets=50),
        0.30: _tc("P-MAG", 0.30, 180, assets=20),
    }
    sel = select_primary(
        PRIMARY_MAG, by_theta, calibration_days=10.0, n_target_fires=150.0, max_window_days=100.0
    )
    assert sel.chosen_theta == 0.20  # 0.30 fails the cluster floor despite more fires


def test_select_primary_too_slow_when_nothing_clears() -> None:
    by_theta = {0.30: _tc("P-MAG", 0.30, 1)}  # 0.1 fires/day -> 1500 days; over ceiling
    sel = select_primary(
        PRIMARY_MAG, by_theta, calibration_days=10.0, n_target_fires=150.0, max_window_days=100.0
    )
    assert sel.too_slow and sel.chosen_theta is None


def test_decide_window_is_max_of_primaries_and_flags_too_slow() -> None:
    def _sel(primary: str, fires: int, max_days: float) -> PrimarySelection:
        return select_primary(
            primary,
            {0.2: _tc(primary, 0.2, fires)},
            calibration_days=10.0,
            n_target_fires=150.0,
            max_window_days=max_days,
        )

    a = _sel("P-MAG", 200, 100.0)
    b = _sel("P-SHAPE", 100, 100.0)
    dec = decide_window([a, b])
    assert not dec.too_slow
    # P-MAG 20/day -> 7.5d; P-SHAPE 10/day -> 15d; max -> ceil(15) = 15.
    assert dec.window_days == 15
    slow = _sel("P-SHAPE", 1, 5.0)
    assert decide_window([a, slow]).too_slow


def test_fingerprint_changes_when_algorithm_inputs_change() -> None:
    base = CalibrationArtifact(
        tool_version="t",
        generated_at="2026-01-01T00:00:00Z",
        code_revision="rev",
        calibration_window_start="a",
        calibration_window_end="b",
        b_completeness_min_fraction=0.99,
        max_finalization_lag_seconds=15,
        baseline_activity_floor_usd=100000.0,
        theta_m_grid=(0.1, 0.2),
        theta_s_grid=(0.1, 0.2),
        cold_bar_manifests={"m": "h"},
        counts=(ThresholdCount(PRIMARY_MAG, 0.2, 10, 5, 40, 1, 4, 0.1),),
        decision={"window_days": 90},
    )
    # A different algorithm DECISION must change the fingerprint (P1: the hash pins
    # the thing we act on, not just the counts).
    other = dataclasses.replace(base, decision={"window_days": 91})
    assert base.fingerprint() != other.fingerprint()


def test_manifest_provenance_fails_closed(tmp_path: Path) -> None:
    from schurfer_analytics.net_buy_accumulation_v2_calibration_report import (
        CalibrationInputError,
        _manifest_hashes,
    )

    with pytest.raises(CalibrationInputError):
        _manifest_hashes(str(tmp_path))  # no manifests at all
    (tmp_path / "bars-2026-08-10.manifest.json").write_text("{ not json")
    with pytest.raises(CalibrationInputError):
        _manifest_hashes(str(tmp_path))  # corrupt manifest


def test_cli_run_produces_fingerprinted_artifact_with_decision_and_coverage() -> None:
    import duckdb
    from schurfer_analytics.net_buy_accumulation_v2_calibration_report import run

    con = duckdb.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars-2026-08-26.parquet")
            _write(con, path)
            (Path(tmp) / "bars-2026-08-26.manifest.json").write_text('{"sha256": "abc123"}')
            con.close()  # release before run() opens its own connection
            out = run(
                cold_bars_dir=tmp,
                cal_start=_CAL_START,
                cal_end=_CAL_END,
                theta_m_grid=(0.10, 0.20, 0.30),
                theta_s_grid=(0.10, 0.20),
                b_completeness_min_fraction=0.99,
                max_finalization_lag_seconds=15,
                expected_unresolved_rate=0.05,
                sizing_margin=1.5,
                max_window_days=120.0,
                code_revision="test",
                memory_limit=None,
                threads=None,
            )
    finally:
        con.close()
    assert out["fingerprint_sha256"]  # present and non-empty
    assert out["tool_version"] == "net_buy_accumulation_v2_calibration"
    assert "window_days" in out["decision"]  # decision folded into the fingerprinted artifact
    assert "eligible_m_minutes" in out["coverage"]  # explainable-zero coverage present
    assert out["cold_bar_manifests"] == {"bars-2026-08-26.manifest.json": "abc123"}
