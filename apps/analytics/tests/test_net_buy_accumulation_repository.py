"""Local validation of the DuckDB scanner SQL against a synthetic Parquet.

DuckDB is available locally, so we generate a fully-present bar series for one
instrument -- seven baseline days, a 24h net-buy ramp filling `W`, and a 240m
exit bar -- crafted so `score_m` crosses `THETA_M` at exactly one known minute.
This exercises the RANGE-frame windows, the eligibility gate, the edge crossing,
and the entry/exit price join end to end (the boundaries are the easiest place to
get an off-by-one). The real cold-bar data is timezone-aware; the RANGE/LAG/join
logic under test is identical on naive timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from schurfer_analytics.net_buy_accumulation import PRIMARY_MAG
from schurfer_analytics.net_buy_accumulation_repository import scan_fires

_FIRE = datetime(2026, 8, 26, 0, 0)  # the single expected P-MAG fire minute
_RAMP_START = _FIRE - timedelta(minutes=1440)  # ramp fills W of _FIRE
_EXIT = _FIRE + timedelta(minutes=239)
# Two extra minutes before the earliest B window so _FIRE has an eligible,
# below-threshold predecessor (a real crossing, not a NULL-prev boundary).
_SERIES_START = _FIRE - timedelta(minutes=11520 + 2)
_SERIES_END = _FIRE + timedelta(minutes=240)  # range() end is exclusive


def _write_fixture(connection: object, path: str) -> None:
    # activity is a flat 2000/min everywhere (buy+sell), so the baseline daily
    # activity is 2000*10080/7 = 2.88M; net_buy is 2000/min only inside the ramp,
    # so the 1440-minute W sum reaches exactly 2.88M and score_m = 1.0 at _FIRE.
    connection.execute(  # type: ignore[attr-defined]
        """
        COPY (
            SELECT
                'bybit' AS exchange,
                'TESTUSDT' AS symbol,
                'linear' AS market_type,
                'v1' AS capture_version,
                ts AS bucket_start,
                CASE WHEN ts >= $ramp_start AND ts < $ramp_end THEN 2000.0 ELSE 1000.0 END
                    AS buy_total_notional_usd,
                CASE WHEN ts >= $ramp_start AND ts < $ramp_end THEN 0.0 ELSE 1000.0 END
                    AS sell_total_notional_usd,
                CASE WHEN ts = $exit_ts THEN 104.0 ELSE 100.0 END AS close_price,
                true AS trades_complete,
                true AS price_complete,
                ts + INTERVAL 30 SECOND AS last_trade_received_at
            FROM (SELECT unnest(range($start, $end, INTERVAL 1 MINUTE)) AS ts)
        ) TO '{path}' (FORMAT PARQUET)
        """.replace("{path}", path),
        {
            "ramp_start": _RAMP_START,
            "ramp_end": _FIRE,
            "exit_ts": _EXIT,
            "start": _SERIES_START,
            "end": _SERIES_END,
        },
    )


def test_scanner_finds_the_single_crafted_p_mag_fire() -> None:
    import duckdb

    connection = duckdb.connect()
    try:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars.parquet")
            _write_fixture(connection, path)
            episodes = scan_fires(
                parquet_glob=path,
                cohort_start=datetime(2026, 8, 25, 0, 0),
                cohort_end=datetime(2026, 8, 27, 0, 0),
                connection=connection,
            )
            mag = [e for e in episodes if e.primary == PRIMARY_MAG]
            assert len(mag) == 1, [e.fire_ts for e in mag]
            fire = mag[0]
            assert fire.fire_ts == _FIRE.isoformat()
            assert fire.instrument == "bybit:TESTUSDT"
            assert fire.cluster == "TEST"
            assert abs(fire.score - 1.0) < 1e-6
            assert fire.entry_close == 100.0
            assert fire.exit_close == 104.0
            assert fire.resolved is True
            assert fire.adj_return_pct is not None and fire.adj_return_pct > 0
    finally:
        connection.close()


def test_report_renders_from_synthetic_cold_bars() -> None:
    # End-to-end: generate a tz-aware fixture (real cold-bars are timestamptz),
    # run generate_report over the cold-bars dir, and render markdown.
    import tempfile
    from datetime import UTC
    from pathlib import Path

    import duckdb
    from schurfer_analytics.net_buy_accumulation_report import (
        generate_report,
        render_json,
        render_markdown,
    )

    fire = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    ramp_start = fire - timedelta(minutes=1440)
    exit_ts = fire + timedelta(minutes=239)
    series_start = fire - timedelta(minutes=11520 + 2)
    series_end = fire + timedelta(minutes=240)

    connection = duckdb.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bars-2026-08-26.parquet")
            connection.execute(
                """
                COPY (
                    SELECT 'bybit' AS exchange, 'TESTUSDT' AS symbol, 'linear' AS market_type,
                        'v1' AS capture_version, ts AS bucket_start,
                        CASE WHEN ts >= $ramp_start AND ts < $ramp_end THEN 2000.0 ELSE 1000.0 END
                            AS buy_total_notional_usd,
                        CASE WHEN ts >= $ramp_start AND ts < $ramp_end THEN 0.0 ELSE 1000.0 END
                            AS sell_total_notional_usd,
                        CASE WHEN ts = $exit_ts THEN 104.0 ELSE 100.0 END AS close_price,
                        true AS trades_complete, true AS price_complete,
                        ts + INTERVAL 30 SECOND AS last_trade_received_at
                    FROM (SELECT unnest(range($start, $end, INTERVAL 1 MINUTE)) AS ts)
                ) TO '{path}' (FORMAT PARQUET)
                """.replace("{path}", path),
                {
                    "ramp_start": ramp_start,
                    "ramp_end": fire,
                    "exit_ts": exit_ts,
                    "start": series_start,
                    "end": series_end,
                },
            )
            report = generate_report(
                cold_bars_dir=tmp,
                cohort_start=datetime(2026, 8, 25, tzinfo=UTC),
                cohort_end=datetime(2026, 8, 27, tzinfo=UTC),
                code_revision="test",
                working_tree_dirty=True,
            )
    finally:
        connection.close()

    assert report.results["P-MAG"].fires == 1
    assert report.results["P-MAG"].resolved_fires == 1
    md = render_markdown(report)
    assert "net-buy accumulation discovery" in md
    assert "P-MAG" in md
    assert render_json(report)  # serializes without error
