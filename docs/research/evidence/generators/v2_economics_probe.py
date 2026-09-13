# EXPLORATORY economics probe for the net-buy accumulation v2 signal: does the
# executable (tradeable) fire set carry any after-cost edge at 240m hold? This READS
# RETURNS, so it is NOT outcome-blind and NOT the frozen formal verdict; it is a fast
# go/no-go on whether the direction is worth formalizing (report metrics, uncertainty
# gate). Labeled exploratory on purpose.
#
# Entry (rev.7 economic proxy): close(t) of the fire bar, which approximates the open
# of the first bar starting after decision_at (~ t+5s). Exit: close(t+240). Hold 240m.
# Diagnostic upper bound (v1): entry close(t-1), exit close(t+239). Long only
# (accumulation -> expect up). Net return = gross - round_trip_cost.
#
# Tradeable filter reuses the frozen sizing: a fire is tradeable at a target size when
# min(target, cap * p25 trailing flow) >= min economic notional. Availability-OFF (no
# created_at in the subset; on/off parity is delta 0).
#
# Run: uv run --package schurfer-analytics python \
#   docs/research/evidence/generators/v2_economics_probe.py
import json, hashlib, subprocess, statistics
from datetime import datetime, timezone
from pathlib import Path
import duckdb
from schurfer_analytics.net_buy_accumulation_v2_calibration import (
    BARS_MARKET_TYPE, BARS_CAPTURE_VERSION, BASELINE_ACTIVITY_FLOOR_USD,
    DEFAULT_B_COMPLETENESS_MIN_FRACTION, PRIMARY_MAG, PRIMARY_SHAPE, dedup_cooldown)
from schurfer_analytics.net_buy_accumulation_v2_liquidity import (
    FireFlow, PARTICIPATION_CAP, MIN_ECONOMIC_NOTIONAL_USD)

REPO = Path(__file__).resolve().parents[4]
PARQUET = str(REPO / "cold-bars-window" / "bars-window.parquet")
CAL_START = datetime(2026, 8, 18, tzinfo=timezone.utc)
CAL_END = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
THETA = 0.25
FRAC = DEFAULT_B_COMPLETENESS_MIN_FRACTION
HOLD_MIN = 240
ROUND_TRIP_COST = 0.0022  # 22 bps, from ECONOMICS.md
TARGET_USD = 300.0        # the size the capacity curve found realistic


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True).stdout.strip()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SQL = f"""
WITH src AS (
    SELECT exchange, symbol, bucket_start,
        (buy_total_notional_usd - sell_total_notional_usd) AS net_buy,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity,
        trades_complete
    FROM read_parquet($parquet)
    WHERE market_type = '{BARS_MARKET_TYPE}' AND capture_version = '{BARS_CAPTURE_VERSION}'
      AND bucket_start >= $cal_start - INTERVAL 11520 MINUTE AND bucket_start < $cal_end
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
        (w_count = 1440 AND w_complete = 1440
         AND b_count = 10080 AND b_complete >= {FRAC} * 10080 AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {BASELINE_ACTIVITY_FLOOR_USD}) AS eligible_m,
        (w_count = 1440 AND w_complete = 1440 AND w_shape_ready = 1440
         AND b_count = 10080 AND b_complete >= {FRAC} * 10080 AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {BASELINE_ACTIVITY_FLOOR_USD}) AS eligible_s,
        CASE WHEN b_complete > 0 THEN w_net_buy / ((b_act_sum_complete / b_complete) * 1440) END AS score_m,
        w_elevated / 1440.0 AS score_s
    FROM rolled
)
"""

CROSSINGS = """
WITH s AS (SELECT * FROM scored),
eligible AS (SELECT exchange, symbol, bucket_start, {score_col} AS score FROM s WHERE {eligible_col}),
crossings AS (
    SELECT e.*, lag(score) OVER (PARTITION BY exchange, symbol ORDER BY bucket_start) AS prev_score
    FROM eligible e)
SELECT exchange, symbol, bucket_start AS fire_ts
FROM crossings
WHERE bucket_start >= $cal_start AND bucket_start < $cal_end
  AND score >= {theta} AND prev_score < {theta}
ORDER BY exchange, symbol, bucket_start
"""

TRAILING = """
WITH tw AS (
    SELECT fs.exchange, fs.symbol, fs.fire_ts, s.activity,
        date_diff('minute', s.bucket_start, fs.fire_ts) AS lag_min
    FROM fireset fs JOIN scored s ON s.exchange = fs.exchange AND s.symbol = fs.symbol
        AND s.bucket_start < fs.fire_ts AND s.bucket_start >= fs.fire_ts - INTERVAL 60 MINUTE)
SELECT exchange, symbol, fire_ts,
    quantile_cont(activity, 0.25) FILTER (WHERE lag_min <= 15) AS p25_15,
    quantile_cont(activity, 0.25) AS p25_60
FROM tw GROUP BY exchange, symbol, fire_ts
"""

# Entry/exit close prices with completeness, per fire. Economic: close(t) -> close(t+240).
# Diagnostic: close(t-1) -> close(t+239).
PRICES = f"""
SELECT fs.exchange, fs.symbol, fs.fire_ts,
    en.close_price AS entry_close, en.price_complete AS entry_ok,
    ex.close_price AS exit_close, ex.price_complete AS exit_ok,
    pv.close_price AS prev_close, pv.price_complete AS prev_ok,
    dx.close_price AS diag_exit_close, dx.price_complete AS diag_exit_ok
FROM fireset fs
LEFT JOIN prices en ON en.exchange = fs.exchange AND en.symbol = fs.symbol AND en.bucket_start = fs.fire_ts
LEFT JOIN prices ex ON ex.exchange = fs.exchange AND ex.symbol = fs.symbol AND ex.bucket_start = fs.fire_ts + INTERVAL {HOLD_MIN} MINUTE
LEFT JOIN prices pv ON pv.exchange = fs.exchange AND pv.symbol = fs.symbol AND pv.bucket_start = fs.fire_ts - INTERVAL 1 MINUTE
LEFT JOIN prices dx ON dx.exchange = fs.exchange AND dx.symbol = fs.symbol AND dx.bucket_start = fs.fire_ts + INTERVAL {HOLD_MIN - 1} MINUTE
"""

con = duckdb.connect()
con.execute("SET memory_limit='6GB'")
con.execute("SET threads=4")
con.execute("CREATE TEMP TABLE scored AS " + SQL + " SELECT * FROM scored",
            {"parquet": PARQUET, "cal_start": CAL_START, "cal_end": CAL_END})
con.execute(f"""CREATE TEMP TABLE prices AS
    SELECT exchange, symbol, bucket_start, close_price, price_complete
    FROM read_parquet($parquet)
    WHERE market_type = '{BARS_MARKET_TYPE}' AND capture_version = '{BARS_CAPTURE_VERSION}'
      AND bucket_start >= $cal_start AND bucket_start < $cal_end + INTERVAL {HOLD_MIN + 5} MINUTE""",
    {"parquet": PARQUET, "cal_start": CAL_START, "cal_end": CAL_END})


def summarize(nets):
    if not nets:
        return {"n": 0}
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(nets),
        "mean_net": statistics.fmean(nets),
        "median_net": statistics.median(nets),
        "win_rate": wins / len(nets),
        "p25_net": statistics.quantiles(nets, n=4)[0] if len(nets) > 1 else nets[0],
        "p75_net": statistics.quantiles(nets, n=4)[2] if len(nets) > 1 else nets[0],
    }


PRIMARIES = {PRIMARY_MAG: ("score_m", "eligible_m"), PRIMARY_SHAPE: ("score_s", "eligible_s")}
result = {}
for prim, (score_col, eligible_col) in PRIMARIES.items():
    raw = [(str(r[0]), str(r[1]), r[2]) for r in con.execute(
        CROSSINGS.format(score_col=score_col, eligible_col=eligible_col, theta=THETA),
        {"cal_start": CAL_START, "cal_end": CAL_END}).fetchall()]
    deduped = dedup_cooldown(raw)
    con.execute("CREATE OR REPLACE TEMP TABLE fireset(exchange VARCHAR, symbol VARCHAR, fire_ts TIMESTAMPTZ)")
    con.executemany("INSERT INTO fireset VALUES (?, ?, ?)", deduped)

    trail = {(str(r[0]), str(r[1]), r[2]): (float(r[3] or 0.0), float(r[4] or 0.0))
             for r in con.execute(TRAILING).fetchall()}
    prices = {(str(r[0]), str(r[1]), r[2]): r[3:] for r in con.execute(PRICES).fetchall()}

    econ_nets, diag_nets, dollar_pnl = [], [], 0.0
    tradeable = 0
    unresolved = 0
    for ex, sym, ts in deduped:
        p25_15, p25_60 = trail.get((ex, sym, ts), (0.0, 0.0))
        flow = min(p25_15, p25_60)
        deliverable = min(TARGET_USD, PARTICIPATION_CAP * flow) if flow > 0 else 0.0
        if deliverable < MIN_ECONOMIC_NOTIONAL_USD:
            continue  # not tradeable at this size
        tradeable += 1
        pr = prices.get((ex, sym, ts))
        if pr is None:
            unresolved += 1
            continue
        entry_close, entry_ok, exit_close, exit_ok, prev_close, prev_ok, dexit_close, dexit_ok = pr
        if not (entry_ok and exit_ok and entry_close and exit_close and entry_close > 0):
            unresolved += 1
            continue
        gross = float(exit_close) / float(entry_close) - 1.0
        econ_nets.append(gross - ROUND_TRIP_COST)
        dollar_pnl += deliverable * (gross - ROUND_TRIP_COST)
        if prev_ok and dexit_ok and prev_close and dexit_close and float(prev_close) > 0:
            diag_nets.append(float(dexit_close) / float(prev_close) - 1.0 - ROUND_TRIP_COST)

    result[prim] = {
        "deduped_fires": len(deduped),
        "tradeable_at_300": tradeable,
        "unresolved_no_price": unresolved,
        "economic_entry_close_t": summarize(econ_nets),
        "diagnostic_entry_close_t_minus_1": summarize(diag_nets),
        "portfolio_dollar_pnl_at_300_equalweight": dollar_pnl,
    }

out = {
    "label": "EXPLORATORY returns probe (reads outcomes; NOT the frozen formal verdict)",
    "source_parquet": "cold-bars-window/bars-window.parquet",
    "source_parquet_sha256": sha256_file(PARQUET),
    "git_rev": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "cal_window_utc": [CAL_START.isoformat(), CAL_END.isoformat()],
    "theta": THETA, "hold_minutes": HOLD_MIN, "round_trip_cost": ROUND_TRIP_COST,
    "target_notional_usd": TARGET_USD, "direction": "long",
    "per_primary": result,
}
out["fingerprint_sha256"] = hashlib.sha256(
    json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
print(json.dumps(out, indent=2, sort_keys=True, default=str))
