# Outcome-blind executability / capacity curve for the net-buy accumulation v2 signal
# at the selected theta=0.25, on the local cold-bar window subset.
#
# Sizing is POINT-IN-TIME: the conservative trailing flow is the p25 of per-minute
# traded notional over the trailing window that ends strictly BEFORE the fire minute
# (min of the trailing-15m and trailing-60m p25). The fire minute's own notional is
# carried as a DIAGNOSTIC only (it is not known at decision_at; using it to size or
# gate would be look-ahead). Dynamic sizing delivers min(target, cap * trailing_flow)
# and a fire is tradeable when that clears the minimum economic notional. No return is
# read, so nothing here is a verdict on edge.
#
# The subset carries no `created_at`, so eligibility runs AVAILABILITY-OFF; sound
# because the on/off fire-level parity on the full real export was delta 0
# (net-buy-accumulation-v2-calibration-onoff).
#
# Run: uv run --package schurfer-analytics python \
#   docs/research/evidence/generators/v2_liquidity_floor.py
import json, hashlib, subprocess
from datetime import datetime, timezone
from pathlib import Path
import duckdb
from schurfer_analytics.net_buy_accumulation_v2_calibration import (
    BARS_MARKET_TYPE, BARS_CAPTURE_VERSION, BASELINE_ACTIVITY_FLOOR_USD,
    DEFAULT_B_COMPLETENESS_MIN_FRACTION, PRIMARY_MAG, PRIMARY_SHAPE, dedup_cooldown)
from schurfer_analytics.net_buy_accumulation_v2_liquidity import (
    FireFlow, capacity_curve, TARGET_NOTIONALS_USD, PARTICIPATION_CAP,
    MIN_ECONOMIC_NOTIONAL_USD)

EV = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[4]
PARQUET = str(REPO / "cold-bars-window" / "bars-window.parquet")
CAL_START = datetime(2026, 8, 18, tzinfo=timezone.utc)
CAL_END = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
THETA = 0.25
FRAC = DEFAULT_B_COMPLETENESS_MIN_FRACTION


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()


# Availability-OFF eligibility (same rolling windows as the maintained scanner, timely
# term dropped), carrying per-minute activity so trailing p25 can be computed later.
SQL = f"""
WITH src AS (
    SELECT exchange, symbol, bucket_start,
        (buy_total_notional_usd - sell_total_notional_usd) AS net_buy,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity,
        trades_complete
    FROM read_parquet($parquet)
    WHERE market_type = '{BARS_MARKET_TYPE}' AND capture_version = '{BARS_CAPTURE_VERSION}'
      AND bucket_start >= $cal_start - INTERVAL 11520 MINUTE
      AND bucket_start < $cal_end
),
per_minute AS (
    SELECT s.*,
        count(*) OVER t7 AS t7_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER t7 AS t7_complete,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER t7 AS t7_act_sum
    FROM src s
    WINDOW t7 AS (PARTITION BY exchange, symbol ORDER BY bucket_start
        RANGE BETWEEN INTERVAL 10080 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING)
),
flagged AS (
    SELECT *,
        CASE WHEN t7_count = 10080 AND t7_complete >= {FRAC} * 10080 THEN 1 ELSE 0 END AS shape_ready,
        CASE WHEN t7_count = 10080 AND t7_complete >= {FRAC} * 10080 AND t7_complete > 0
              AND activity > (t7_act_sum / t7_complete) AND net_buy > 0 THEN 1 ELSE 0 END AS elevated_buy
    FROM per_minute
),
rolled AS (
    SELECT exchange, symbol, bucket_start, activity,
        count(*) OVER w AS w_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w AS w_complete,
        sum(shape_ready) OVER w AS w_shape_ready,
        sum(net_buy) OVER w AS w_net_buy,
        sum(elevated_buy) OVER w AS w_elevated,
        count(*) OVER b AS b_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER b AS b_complete,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER b AS b_act_sum_complete
    FROM flagged
    WINDOW
        w AS (PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 1440 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING),
        b AS (PARTITION BY exchange, symbol ORDER BY bucket_start
            RANGE BETWEEN INTERVAL 11520 MINUTE PRECEDING AND INTERVAL 1441 MINUTE PRECEDING)
),
scored AS (
    SELECT exchange, symbol, bucket_start, activity,
        (b_act_sum_complete / b_complete) * 1440 AS baseline_daily_activity,
        (w_count = 1440 AND w_complete = 1440
         AND b_count = 10080 AND b_complete >= {FRAC} * 10080 AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {BASELINE_ACTIVITY_FLOOR_USD}) AS eligible_m,
        (w_count = 1440 AND w_complete = 1440 AND w_shape_ready = 1440
         AND b_count = 10080 AND b_complete >= {FRAC} * 10080 AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {BASELINE_ACTIVITY_FLOOR_USD}) AS eligible_s,
        CASE WHEN b_complete > 0
             THEN w_net_buy / ((b_act_sum_complete / b_complete) * 1440) END AS score_m,
        w_elevated / 1440.0 AS score_s
    FROM rolled
)
"""

CROSSINGS = """
WITH s AS (SELECT * FROM scored),
eligible AS (
    SELECT exchange, symbol, bucket_start, activity, {score_col} AS score
    FROM s WHERE {eligible_col}
),
crossings AS (
    SELECT e.*, lag(score) OVER (PARTITION BY exchange, symbol ORDER BY bucket_start) AS prev_score
    FROM eligible e
)
SELECT exchange, symbol, bucket_start AS fire_ts, activity AS fire_minute_notional
FROM crossings
WHERE bucket_start >= $cal_start AND bucket_start < $cal_end
  AND score >= {theta} AND prev_score < {theta}
ORDER BY exchange, symbol, bucket_start
"""

# p25 of per-minute traded notional over trailing 15m and 60m, strictly BEFORE the
# fire minute (point-in-time; no look-ahead).
TRAILING = """
WITH tw AS (
    SELECT fs.exchange, fs.symbol, fs.fire_ts, s.activity,
        date_diff('minute', s.bucket_start, fs.fire_ts) AS lag_min
    FROM fireset fs
    JOIN scored s ON s.exchange = fs.exchange AND s.symbol = fs.symbol
        AND s.bucket_start < fs.fire_ts
        AND s.bucket_start >= fs.fire_ts - INTERVAL 60 MINUTE
)
SELECT exchange, symbol, fire_ts,
    quantile_cont(activity, 0.25) FILTER (WHERE lag_min <= 15) AS p25_15,
    quantile_cont(activity, 0.25) AS p25_60
FROM tw GROUP BY exchange, symbol, fire_ts
"""

con = duckdb.connect()
con.execute("SET memory_limit='6GB'")
con.execute("SET threads=4")
con.execute("CREATE TEMP TABLE scored AS " + SQL + " SELECT * FROM scored",
            {"parquet": PARQUET, "cal_start": CAL_START, "cal_end": CAL_END})

PRIMARIES = {PRIMARY_MAG: ("score_m", "eligible_m"), PRIMARY_SHAPE: ("score_s", "eligible_s")}
result = {}
for prim, (score_col, eligible_col) in PRIMARIES.items():
    rows = con.execute(
        CROSSINGS.format(score_col=score_col, eligible_col=eligible_col, theta=THETA),
        {"cal_start": CAL_START, "cal_end": CAL_END}).fetchall()
    raw = [(str(r[0]), str(r[1]), r[2]) for r in rows]
    fire_minute = {(str(r[0]), str(r[1]), r[2]): float(r[3]) for r in rows}
    deduped = dedup_cooldown(raw)

    con.execute("CREATE OR REPLACE TEMP TABLE fireset(exchange VARCHAR, symbol VARCHAR, fire_ts TIMESTAMPTZ)")
    con.executemany("INSERT INTO fireset VALUES (?, ?, ?)", deduped)
    trail = {(str(r[0]), str(r[1]), r[2]): (float(r[3] or 0.0), float(r[4] or 0.0))
             for r in con.execute(TRAILING).fetchall()}

    fires = []
    for ex, sym, ts in deduped:
        p25_15, p25_60 = trail.get((ex, sym, ts), (0.0, 0.0))
        conservative = min(p25_15, p25_60)
        fires.append(FireFlow(ex, sym, conservative, fire_minute[(ex, sym, ts)]))

    curve = capacity_curve(prim, THETA, fires)
    result[prim] = {"raw_crossings": len(raw), "deduped_fires": len(deduped),
                    "capacity_curve": [cp.__dict__ for cp in curve]}

out = {
    "source_parquet": "cold-bars-window/bars-window.parquet",
    "source_parquet_sha256": sha256_file(PARQUET),
    "git_rev": git("rev-parse", "HEAD"),
    "git_dirty": bool(git("status", "--porcelain")),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "cal_window_utc": [CAL_START.isoformat(), CAL_END.isoformat()],
    "availability": "off (created_at absent in subset; on/off parity is delta 0)",
    "theta": THETA, "b_completeness_min_fraction": FRAC,
    "sizing": {
        "method": "dynamic: min(target, cap * conservative_trailing_flow)",
        "conservative_trailing_flow": "min(p25 trailing-15m, p25 trailing-60m), point-in-time (< fire minute)",
        "participation_cap": PARTICIPATION_CAP,
        "min_economic_notional_usd": MIN_ECONOMIC_NOTIONAL_USD,
        "target_notionals_usd": list(TARGET_NOTIONALS_USD),
        "note": "fire-minute notional is diagnostic only; returns NOT read (outcome-blind)",
    },
    "per_primary": result,
}
out["fingerprint_sha256"] = hashlib.sha256(
    json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
print(json.dumps(out, indent=2, sort_keys=True, default=str))
