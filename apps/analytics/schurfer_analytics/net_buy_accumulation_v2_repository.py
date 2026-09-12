"""DuckDB scanner for the net-buy accumulation v2 CALIBRATION tool.

Implements the rev.4 v2 eligibility over the frozen cold-bar Parquet and returns,
per (primary, threshold), the raw edge-crossing rows (before the 24h cooldown,
which is applied in Python). It is OUTCOME-BLIND: it computes scores and fires
only, never an entry/exit price or a return. All heavy per-minute work is
single-pass SQL-side rolling windows.

Eligibility (see `net-buy-accumulation-discovery-v2.md`, rev.4):

- `timely(m)`: `created_at(m) <= bucket_end(m) + MAX_FINALIZATION_LAG`, the
  non-backfill guard measured from bucket end (`bucket_end = bucket_start + 1 min`).
- W (1440): 100% present, 100% trades_complete, 100% timely.
- B (10080): 100% present, at least the completeness fraction trades_complete,
  100% timely; `baseline_daily_activity = mean(activity over present-and-complete
  B minutes) * 1440` and must clear `BASELINE_ACTIVITY_FLOOR`.
- P-SHAPE additionally requires every W minute's OWN trailing-7d window to be 100%
  present and at least the fraction complete (`shape_ready`), so a coverage miss is
  never a silent non-elevated 0.

`created_at` is required in the Parquet; the real `SELECT *` cold-bar export
carries it (the curated local window subset does not, so this path runs on the
full export / prod cold-bars).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- used in runtime-evaluated signatures
from typing import Any

from .net_buy_accumulation_v2_calibration import (
    BARS_CAPTURE_VERSION,
    BARS_MARKET_TYPE,
    BASELINE_ACTIVITY_FLOOR_USD,
)

# Base eligibility CTE. `{lag_seconds}`, `{frac}` and `{floor}` are frozen numeric
# constants substituted before execution (never external input).
_ELIGIBILITY_CTE = """
WITH src AS (
    SELECT
        exchange, symbol, bucket_start,
        (buy_total_notional_usd - sell_total_notional_usd) AS net_buy,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity,
        trades_complete,
        CASE
            WHEN created_at IS NOT NULL
                 AND created_at <= bucket_start + INTERVAL 1 MINUTE
                                   + INTERVAL {lag_seconds} SECOND
            THEN 1 ELSE 0
        END AS timely
    FROM read_parquet($parquet_glob)
    WHERE market_type = '{market_type}'
      AND capture_version = '{capture_version}'
      -- Bound to the calibration window plus just enough history for the rolling
      -- W (1440) + B (10080) windows (11520 minutes), so the scan does not read
      -- the whole growing cold-bar archive (rev.5, P2).
      AND bucket_start >= $cal_start - INTERVAL 11520 MINUTE
      AND bucket_start < $cal_end
),
per_minute AS (
    SELECT
        s.*,
        count(*) OVER t7 AS t7_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER t7 AS t7_complete,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER t7 AS t7_act_sum
    FROM src s
    WINDOW t7 AS (
        PARTITION BY exchange, symbol ORDER BY bucket_start
        RANGE BETWEEN INTERVAL 10080 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING
    )
),
flagged AS (
    SELECT
        *,
        CASE WHEN t7_count = 10080 AND t7_complete >= {frac} * 10080
             THEN 1 ELSE 0 END AS shape_ready,
        CASE WHEN t7_count = 10080 AND t7_complete >= {frac} * 10080
                  AND t7_complete > 0
                  AND activity > (t7_act_sum / t7_complete)
                  AND net_buy > 0
             THEN 1 ELSE 0 END AS elevated_buy
    FROM per_minute
),
rolled AS (
    SELECT
        exchange, symbol, bucket_start,
        count(*) OVER w AS w_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w AS w_complete,
        sum(timely) OVER w AS w_timely,
        sum(shape_ready) OVER w AS w_shape_ready,
        sum(net_buy) OVER w AS w_net_buy,
        sum(elevated_buy) OVER w AS w_elevated,
        count(*) OVER b AS b_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER b AS b_complete,
        sum(timely) OVER b AS b_timely,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER b AS b_act_sum_complete
    FROM flagged
    WINDOW
        w AS (
            PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 1440 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING
        ),
        b AS (
            PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 11520 MINUTE PRECEDING AND INTERVAL 1441 MINUTE PRECEDING
        )
),
scored AS (
    SELECT
        exchange, symbol, bucket_start,
        (b_act_sum_complete / b_complete) * 1440 AS baseline_daily_activity,
        (w_count = 1440 AND w_complete = 1440 AND w_timely = 1440
         AND b_count = 10080 AND b_complete >= {frac} * 10080 AND b_timely = 10080
         AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {floor}) AS eligible_m,
        (w_count = 1440 AND w_complete = 1440 AND w_timely = 1440 AND w_shape_ready = 1440
         AND b_count = 10080 AND b_complete >= {frac} * 10080 AND b_timely = 10080
         AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {floor}) AS eligible_s,
        CASE WHEN b_complete > 0
             THEN w_net_buy / ((b_act_sum_complete / b_complete) * 1440) END AS score_m,
        w_elevated / 1440.0 AS score_s
    FROM rolled
)
"""

_CROSSING_TAIL = """,
eligible AS (
    SELECT exchange, symbol, bucket_start, {score_col} AS score
    FROM scored
    WHERE {eligible_col}
),
crossings AS (
    SELECT
        e.*,
        lag(score) OVER (PARTITION BY exchange, symbol ORDER BY bucket_start) AS prev_score
    FROM eligible e
)
SELECT exchange, symbol, bucket_start AS fire_ts
FROM crossings
WHERE bucket_start >= $cal_start
  AND bucket_start < $cal_end
  AND score >= {theta}
  AND prev_score < {theta}
ORDER BY exchange, symbol, bucket_start
"""


def scan_crossings(
    *,
    parquet_glob: str,
    cal_start: datetime,
    cal_end: datetime,
    score_col: str,
    eligible_col: str,
    theta: float,
    b_completeness_min_fraction: float,
    max_finalization_lag_seconds: int,
    connection: Any = None,
    memory_limit: str | None = None,
    threads: int | None = None,
) -> list[tuple[str, str, datetime]]:
    """Return raw edge-crossing rows `(exchange, symbol, fire_ts)` for one primary
    (via `score_col`/`eligible_col`) at `theta`, over the calibration window.
    Outcome-blind. The 24h cooldown is applied by the caller."""
    own = connection is None
    if own:
        import duckdb

        connection = duckdb.connect()
        if memory_limit is not None:
            connection.execute(f"SET memory_limit='{memory_limit}'")
        if threads is not None:
            connection.execute(f"SET threads={int(threads)}")
    try:
        sql = (_ELIGIBILITY_CTE + _CROSSING_TAIL).format(
            market_type=BARS_MARKET_TYPE,
            capture_version=BARS_CAPTURE_VERSION,
            lag_seconds=int(max_finalization_lag_seconds),
            frac=float(b_completeness_min_fraction),
            floor=BASELINE_ACTIVITY_FLOOR_USD,
            score_col=score_col,
            eligible_col=eligible_col,
            theta=float(theta),
        )
        cursor = connection.execute(
            sql, {"parquet_glob": parquet_glob, "cal_start": cal_start, "cal_end": cal_end}
        )
        return [(str(r[0]), str(r[1]), r[2]) for r in cursor.fetchall()]
    finally:
        if own:
            connection.close()


_PRIMARY_COLS = {
    "P-MAG": ("score_m", "eligible_m"),
    "P-SHAPE": ("score_s", "eligible_s"),
}

_CROSSING_FROM_TABLE = """
WITH eligible AS (
    SELECT exchange, symbol, bucket_start, {score_col} AS score
    FROM v2_scored
    WHERE {eligible_col}
),
crossings AS (
    SELECT
        e.*,
        lag(score) OVER (PARTITION BY exchange, symbol ORDER BY bucket_start) AS prev_score
    FROM eligible e
)
SELECT exchange, symbol, bucket_start AS fire_ts
FROM crossings
WHERE bucket_start >= $cal_start
  AND bucket_start < $cal_end
  AND score >= {theta}
  AND prev_score < {theta}
ORDER BY exchange, symbol, bucket_start
"""


def scan_calibration_grid(
    *,
    parquet_glob: str,
    cal_start: datetime,
    cal_end: datetime,
    theta_grids: dict[str, tuple[float, ...]],
    b_completeness_min_fraction: float,
    max_finalization_lag_seconds: int,
    connection: Any = None,
    memory_limit: str | None = None,
    threads: int | None = None,
) -> tuple[dict[tuple[str, float], list[tuple[str, str, datetime]]], dict[str, int]]:
    """Materialize the heavy v2 eligibility ONCE into a temp table, then run the
    light per-threshold crossing query for each (primary, theta) in `theta_grids`.
    Returns `({(primary, theta): [(exchange, symbol, fire_ts), ...]}, coverage)`
    where coverage carries per-gate survivor counts so a zero fire count is
    explainable (no crossing vs everything filtered). Raw fires, before cooldown.
    Outcome-blind."""
    own = connection is None
    if own:
        import duckdb

        connection = duckdb.connect()
        if memory_limit is not None:
            connection.execute(f"SET memory_limit='{memory_limit}'")
        if threads is not None:
            connection.execute(f"SET threads={int(threads)}")
    try:
        materialize = (
            "CREATE TEMP TABLE v2_scored AS "  # noqa: S608 -- frozen numeric constants only
            + _ELIGIBILITY_CTE.format(
                market_type=BARS_MARKET_TYPE,
                capture_version=BARS_CAPTURE_VERSION,
                lag_seconds=int(max_finalization_lag_seconds),
                frac=float(b_completeness_min_fraction),
                floor=BASELINE_ACTIVITY_FLOOR_USD,
            )
            + " SELECT * FROM scored"
        )
        binds = {"parquet_glob": parquet_glob, "cal_start": cal_start, "cal_end": cal_end}
        connection.execute(materialize, binds)
        coverage = _coverage_counts(connection, cal_start, cal_end)
        out: dict[tuple[str, float], list[tuple[str, str, datetime]]] = {}
        for primary, thetas in theta_grids.items():
            score_col, eligible_col = _PRIMARY_COLS[primary]
            for theta in thetas:
                sql = _CROSSING_FROM_TABLE.format(
                    score_col=score_col, eligible_col=eligible_col, theta=float(theta)
                )
                cursor = connection.execute(sql, {"cal_start": cal_start, "cal_end": cal_end})
                out[(primary, theta)] = [(str(r[0]), str(r[1]), r[2]) for r in cursor.fetchall()]
        return out, coverage
    finally:
        if own:
            connection.close()


def _coverage_counts(connection: Any, cal_start: datetime, cal_end: datetime) -> dict[str, int]:
    """Per-gate survivor counts over the materialized `v2_scored`, so a zero fire
    count can be told apart from 'no minute was ever eligible'. Outcome-blind."""
    row = connection.execute(
        """
        SELECT
            count(*) FILTER (WHERE bucket_start >= $cal_start AND bucket_start < $cal_end)
                AS minutes_in_window,
            count(*) FILTER (WHERE eligible_m AND bucket_start >= $cal_start
                             AND bucket_start < $cal_end) AS eligible_m_minutes,
            count(*) FILTER (WHERE eligible_s AND bucket_start >= $cal_start
                             AND bucket_start < $cal_end) AS eligible_s_minutes,
            count(DISTINCT (exchange || '|' || symbol || '|' || cast(bucket_start AS DATE)))
                FILTER (WHERE eligible_m AND bucket_start >= $cal_start
                        AND bucket_start < $cal_end) AS eligible_m_instrument_days,
            count(DISTINCT (exchange || '|' || symbol || '|' || cast(bucket_start AS DATE)))
                FILTER (WHERE eligible_s AND bucket_start >= $cal_start
                        AND bucket_start < $cal_end) AS eligible_s_instrument_days
        FROM v2_scored
        """,
        {"cal_start": cal_start, "cal_end": cal_end},
    ).fetchone()
    keys = (
        "minutes_in_window",
        "eligible_m_minutes",
        "eligible_s_minutes",
        "eligible_m_instrument_days",
        "eligible_s_instrument_days",
    )
    return {k: int(v or 0) for k, v in zip(keys, row, strict=True)}


__all__ = ["scan_calibration_grid", "scan_crossings"]
