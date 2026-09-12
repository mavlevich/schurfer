"""DuckDB-over-frozen-Parquet repository for the net-buy accumulation scanner.

Reads the frozen cold-bar Parquet days (`cold_bar_export.py` output) and produces
one `FiredEpisode` per edge-triggered fire, for both primaries, over the frozen
decision window. All heavy per-minute work is single-pass SQL-side rolling
windows (RANGE frames on `bucket_start`), so it stays within the 4 GB host:

- `score_m(t) = sum(net_buy over W) / (sum(activity over B) / 7)`;
- `elevated_buy(m) = activity(m) > mean activity over [m-7d, m) AND net_buy(m) > 0`,
  `score_s(t) = count(elevated_buy over W) / 1440`;
- eligibility requires the W (1440) and B (10080) windows fully present and
  `trades_complete`, and `mean daily activity over B >= BASELINE_ACTIVITY_FLOOR`;
- a fire is the earliest eligible minute where a score crosses its threshold from
  below (`LAG` over eligible minutes); the 24h minimum-gap reset is applied in
  Python over the sparse crossings.

IMPORTANT: this data path cannot be exercised without the real cold-bar Parquet,
so it is validated on CI / the server against real data (like the HYP-024
repository SQL), not by a local unit run. The pure verdict logic in
`net_buy_accumulation.py` is separately unit-tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .net_buy_accumulation import (
    BASELINE_ACTIVITY_FLOOR_USD,
    PRIMARY_MAG,
    PRIMARY_SHAPE,
    THETA_M,
    THETA_S,
    FiredEpisode,
)

# The momentum capture writes market_type = "linear" for both venues and
# capture_version "v1" (see BYBIT_MOMENTUM_MARKET_TYPE / _CAPTURE_VERSION); the
# frozen Parquet carries the raw `bybit_momentum_bars_1m` columns.
BARS_MARKET_TYPE = "linear"
BARS_CAPTURE_VERSION = "v1"
COOLDOWN_MINUTES = 24 * 60
HORIZON_MINUTES = 240

# One row per candidate fire (before the 24h reset dedup). Scores, eligibility,
# entry (bar t-1) and exit (bar t+239) closes, and the baseline daily activity
# (for the liquidity segment) are all computed SQL-side. `:score_col` and
# `:theta` are substituted per primary.
_FIRE_SQL_TEMPLATE = """
WITH src AS (
    SELECT
        exchange,
        symbol,
        bucket_start,
        (buy_total_notional_usd - sell_total_notional_usd) AS net_buy,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity,
        close_price,
        trades_complete,
        price_complete,
        last_trade_received_at
    FROM read_parquet($parquet_glob)
    WHERE market_type = '{market_type}'
      AND capture_version = '{capture_version}'
),
per_minute AS (
    SELECT
        s.*,
        avg(activity) OVER (
            PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 10080 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING
        ) AS trailing7d_mean_activity,
        count(*) OVER (
            PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 10080 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING
        ) AS trailing7d_count
    FROM src s
),
flagged AS (
    SELECT
        *,
        CASE
            WHEN trailing7d_count >= 10080
                 AND activity > trailing7d_mean_activity
                 AND net_buy > 0
            THEN 1 ELSE 0
        END AS elevated_buy
    FROM per_minute
),
rolled AS (
    SELECT
        exchange, symbol, bucket_start, close_price,
        sum(net_buy) OVER w AS w_net_buy_sum,
        count(*) OVER w AS w_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w AS w_complete,
        sum(elevated_buy) OVER w AS w_elevated,
        sum(activity) OVER b AS b_activity_sum,
        count(*) OVER b AS b_count
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
        exchange, symbol, bucket_start, close_price,
        b_activity_sum / 7.0 AS baseline_daily_activity,
        (w_count = 1440 AND w_complete = 1440 AND b_count = 10080
         AND (b_activity_sum / 7.0) >= {baseline_floor}) AS eligible,
        CASE WHEN b_activity_sum > 0 THEN w_net_buy_sum / (b_activity_sum / 7.0) END AS score_m,
        w_elevated / 1440.0 AS score_s
    FROM rolled
),
eligible AS (
    -- Eligibility is computed over ALL minutes (not the cohort) so the edge
    -- crossing at a cohort boundary still sees the previous eligible minute.
    SELECT *, {score_col} AS score
    FROM scored
    WHERE eligible
),
crossings AS (
    SELECT
        e.*,
        lag(score) OVER (PARTITION BY exchange, symbol ORDER BY bucket_start) AS prev_score
    FROM eligible e
)
SELECT
    c.exchange,
    c.symbol,
    c.bucket_start AS fire_ts,
    c.score,
    c.baseline_daily_activity,
    entry.close_price AS entry_close,
    exit_bar.close_price AS exit_close
FROM crossings c
LEFT JOIN src entry
    ON entry.exchange = c.exchange AND entry.symbol = c.symbol
   AND entry.bucket_start = c.bucket_start - INTERVAL 1 MINUTE
   AND entry.price_complete
   AND entry.last_trade_received_at < c.bucket_start
LEFT JOIN src exit_bar
    ON exit_bar.exchange = c.exchange AND exit_bar.symbol = c.symbol
   AND exit_bar.bucket_start = c.bucket_start + INTERVAL 239 MINUTE
   AND exit_bar.price_complete
WHERE c.bucket_start >= $cohort_start
  AND c.bucket_start < $cohort_end
  AND c.score >= {theta}
  AND c.prev_score < {theta}  -- a real below-to-above crossing; NULL prev never fires
ORDER BY c.exchange, c.symbol, c.bucket_start
"""


def _base_of(symbol: str) -> str:
    """Cluster key: the base ticker. USDT-margined linear perps end in USDT; a
    mandatory collision audit lives in the report, this is only the key."""
    return symbol[:-4].upper() if symbol.upper().endswith("USDT") else symbol.upper()


def _iso_week(ts: datetime) -> str:
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _apply_cooldown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The 24h minimum-gap reset: within an instrument the edge crossings are
    already one-per-build, but suppress any crossing within 24h of the last kept
    fire on the same instrument."""
    kept: list[dict[str, Any]] = []
    last_fire: dict[str, datetime] = {}
    for row in sorted(rows, key=lambda r: (r["exchange"], r["symbol"], r["fire_ts"])):
        key = f"{row['exchange']}:{row['symbol']}"
        prev = last_fire.get(key)
        ts = row["fire_ts"]
        if prev is not None and ts - prev < timedelta(minutes=COOLDOWN_MINUTES):
            continue
        kept.append(row)
        last_fire[key] = ts
    return kept


def _covered_weeks(cohort_start: datetime, cohort_end: datetime) -> set[str]:
    """UTC weeks fully inside the decision window: a week is fully covered when
    all seven of its days fall in the window (partial boundary weeks are excluded
    from the per-week floor)."""
    days_per_week: dict[str, int] = {}
    probe = cohort_start
    while probe < cohort_end:
        week = _iso_week(probe)
        days_per_week[week] = days_per_week.get(week, 0) + 1
        probe += timedelta(days=1)
    return {week for week, days in days_per_week.items() if days >= 7}


def scan_fires(
    *,
    parquet_glob: str,
    cohort_start: datetime,
    cohort_end: datetime,
    connection: Any = None,
) -> list[FiredEpisode]:
    """Return the deduplicated fired episodes for both primaries. `connection`
    is an open DuckDB connection (injected in tests); when omitted a fresh
    in-process DuckDB connection is created. Read-only over the Parquet."""
    own = connection is None
    if own:
        import duckdb

        connection = duckdb.connect()
    try:
        covered = _covered_weeks(cohort_start, cohort_end)
        episodes: list[FiredEpisode] = []
        for primary, score_col, theta in (
            (PRIMARY_MAG, "score_m", THETA_M),
            (PRIMARY_SHAPE, "score_s", THETA_S),
        ):
            sql = _FIRE_SQL_TEMPLATE.format(
                market_type=BARS_MARKET_TYPE,
                capture_version=BARS_CAPTURE_VERSION,
                baseline_floor=BASELINE_ACTIVITY_FLOOR_USD,
                score_col=score_col,
                theta=theta,
            )
            rows = _fetch(connection, sql, parquet_glob, cohort_start, cohort_end)
            for row in _apply_cooldown(rows):
                if row["entry_close"] is None:
                    # No priceable entry bar (t-1 missing/unavailable): the fire
                    # cannot be entered at all, so it is dropped rather than
                    # counted -- never a fabricated entry.
                    continue
                ts: datetime = row["fire_ts"]
                episodes.append(
                    FiredEpisode(
                        primary=primary,
                        instrument=f"{row['exchange']}:{row['symbol']}",
                        cluster=_base_of(str(row["symbol"])),
                        exchange=str(row["exchange"]),
                        fire_ts=ts.isoformat(),
                        utc_day=ts.date().isoformat(),
                        utc_week=_iso_week(ts),
                        week_fully_covered=_iso_week(ts) in covered,
                        score=float(row["score"]),
                        entry_close=float(row["entry_close"]),
                        # exit_close None -> a valid unresolved episode (counted,
                        # never a negative).
                        exit_close=(
                            float(row["exit_close"]) if row["exit_close"] is not None else None
                        ),
                        baseline_daily_activity_usd=float(row["baseline_daily_activity"]),
                    )
                )
        return episodes
    finally:
        if own:
            connection.close()


def _fetch(
    connection: Any,
    sql: str,
    parquet_glob: str,
    cohort_start: datetime,
    cohort_end: datetime,
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        sql,
        {
            "parquet_glob": parquet_glob,
            "cohort_start": cohort_start,
            "cohort_end": cohort_end,
        },
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]


# --- eligibility funnel (STANDALONE coverage diagnostic) --------------------
#
# This is a self-contained diagnostic query: it shares NO SQL with the frozen v1
# fire path above and changes no verdict, no `formal_run`, and no scanner
# semantics. It answers only "where does each venue drop out of the eligibility
# pipeline?" -- absent capture, W/B incompleteness, unavailability, or the
# baseline-activity floor -- by counting, per exchange, the instrument-minutes
# and distinct instruments that survive each stage over the decision window.
#
# The stage counts are cumulative and diagnostic; they are NOT a statement of the
# frozen v1 eligibility rule (which is defined solely by the fire path). B
# `trades_complete` is surfaced only as two informational columns
# (`b_fully_complete_diag`, `b_ge_99pct_diag`), never as a gate here. Because the
# 1440/10080 rolling windows need history before each window-minute, `src` is
# bounded to `[cohort_start - 11520 min, cohort_end)` rather than scanning the
# whole Parquet.
_FUNNEL_SQL_TEMPLATE = """
WITH src AS (
    SELECT
        exchange, symbol, bucket_start,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity,
        trades_complete,
        last_trade_received_at
    FROM read_parquet($parquet_glob)
    WHERE market_type = '{market_type}'
      AND capture_version = '{capture_version}'
      AND bucket_start >= $cohort_start - INTERVAL 11520 MINUTE
      AND bucket_start < $cohort_end
),
rolled AS (
    SELECT
        exchange, symbol, bucket_start,
        count(*) OVER w AS w_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w AS w_complete,
        max(last_trade_received_at) OVER w AS w_max_received_at,
        count(*) OVER b AS b_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER b AS b_complete,
        sum(activity) OVER b AS b_activity_sum
    FROM src
    WINDOW
        w AS (
            PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 1440 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING
        ),
        b AS (
            PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 11520 MINUTE PRECEDING AND INTERVAL 1441 MINUTE PRECEDING
        )
)
SELECT
    exchange,
    count(*) AS minutes_in_window,
    count(DISTINCT symbol) AS instruments,
    count(*) FILTER (WHERE w_count = 1440) AS w_present,
    count(*) FILTER (WHERE w_count = 1440 AND w_complete = 1440) AS w_complete,
    count(*) FILTER (
        WHERE w_count = 1440 AND w_complete = 1440 AND b_count = 10080
    ) AS b_present,
    count(*) FILTER (
        WHERE w_count = 1440 AND w_complete = 1440 AND b_count = 10080
          AND (w_max_received_at IS NULL OR w_max_received_at < bucket_start)
    ) AS available,
    count(*) FILTER (
        WHERE w_count = 1440 AND w_complete = 1440 AND b_count = 10080
          AND (w_max_received_at IS NULL OR w_max_received_at < bucket_start)
          AND (b_activity_sum / 7.0) >= {baseline_floor}
    ) AS eligible,
    count(DISTINCT symbol) FILTER (
        WHERE w_count = 1440 AND w_complete = 1440 AND b_count = 10080
          AND (w_max_received_at IS NULL OR w_max_received_at < bucket_start)
          AND (b_activity_sum / 7.0) >= {baseline_floor}
    ) AS eligible_instruments,
    -- B trades_complete DIAGNOSTICS (never a gate here): among B-present minutes,
    -- how many have a fully / >=99%-complete baseline.
    count(*) FILTER (
        WHERE w_count = 1440 AND w_complete = 1440 AND b_count = 10080
          AND b_complete = 10080
    ) AS b_fully_complete_diag,
    count(*) FILTER (
        WHERE w_count = 1440 AND w_complete = 1440 AND b_count = 10080
          AND b_complete >= 0.99 * 10080
    ) AS b_ge_99pct_diag
FROM rolled
WHERE bucket_start >= $cohort_start
  AND bucket_start < $cohort_end
GROUP BY exchange
ORDER BY exchange
"""


def scan_eligibility_funnel(
    *,
    parquet_glob: str,
    cohort_start: datetime,
    cohort_end: datetime,
    connection: Any = None,
) -> list[dict[str, Any]]:
    """Per-exchange eligibility funnel over the decision window (see
    `_FUNNEL_SQL_TEMPLATE`). Standalone diagnostic: reads only the Parquet, shares
    no SQL with the frozen fire path, and changes no verdict. `connection` is an
    open DuckDB connection (injected in tests); a fresh one is created otherwise."""
    own = connection is None
    if own:
        import duckdb

        connection = duckdb.connect()
    try:
        sql = _FUNNEL_SQL_TEMPLATE.format(
            market_type=BARS_MARKET_TYPE,
            capture_version=BARS_CAPTURE_VERSION,
            baseline_floor=BASELINE_ACTIVITY_FLOOR_USD,
        )
        return _fetch(connection, sql, parquet_glob, cohort_start, cohort_end)
    finally:
        if own:
            connection.close()


__all__ = [
    "BARS_CAPTURE_VERSION",
    "BARS_MARKET_TYPE",
    "scan_eligibility_funnel",
    "scan_fires",
]
