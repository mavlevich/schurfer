# EXPLORATORY: accumulation-LONG forward-return DISTRIBUTION across multiple holds
# (4h .. 3d), to test whether the "long is dead" verdict was an artifact of the 240m
# hold. The LSK example (accumulation on Sep 9-10 -> ~20x on Sep 12-13, outside our
# window) suggests the payoff horizon is DAYS, and the edge (if any) is a fat right
# tail (let winners run), not a mean at a 4h hold. READS RETURNS; not a verdict.
#
# Long gross return at hold H = close(t+H)/close(t) - 1. Reports the full distribution
# (mean/median/p90/p95/max, win rate, fraction above +10%/+25%) per primary per hold.
# Data ends ~2026-09-11, so long holds only resolve for early fires (n shrinks with H;
# reported). Availability-OFF (on/off parity delta 0).
#
# Run: uv run --package schurfer-analytics python \
#   docs/research/evidence/generators/v2_long_multihold_probe.py
import json, hashlib, subprocess, statistics
from datetime import datetime, timezone
from pathlib import Path
import duckdb
from schurfer_analytics.net_buy_accumulation_v2_calibration import (
    BARS_MARKET_TYPE, BARS_CAPTURE_VERSION, BASELINE_ACTIVITY_FLOOR_USD,
    DEFAULT_B_COMPLETENESS_MIN_FRACTION, PRIMARY_MAG, PRIMARY_SHAPE, dedup_cooldown)

REPO = Path(__file__).resolve().parents[4]
PARQUET = str(REPO / "cold-bars-window" / "bars-window.parquet")
CAL_START = datetime(2026, 8, 18, tzinfo=timezone.utc)
CAL_END = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
THETA = 0.25
FRAC = DEFAULT_B_COMPLETENESS_MIN_FRACTION
HOLDS = (240, 720, 1440, 2880, 4320)  # 4h, 12h, 1d, 2d, 3d
COST = 0.0022


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
        (buy_total_notional_usd + sell_total_notional_usd) AS activity, trades_complete
    FROM read_parquet($parquet)
    WHERE market_type = '{BARS_MARKET_TYPE}' AND capture_version = '{BARS_CAPTURE_VERSION}'
      AND bucket_start >= $cal_start - INTERVAL 11520 MINUTE AND bucket_start < $cal_end
),
per_minute AS (
    SELECT s.*, count(*) OVER t7 AS t7_count,
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
    SELECT exchange, symbol, bucket_start,
        count(*) OVER w AS w_count,
        sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w AS w_complete,
        sum(shape_ready) OVER w AS w_shape_ready,
        sum(net_buy) OVER w AS w_net_buy, sum(elevated_buy) OVER w AS w_elevated,
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
    SELECT exchange, symbol, bucket_start,
        (w_count = 1440 AND w_complete = 1440 AND b_count = 10080 AND b_complete >= {FRAC} * 10080
         AND b_complete > 0 AND (b_act_sum_complete / b_complete) * 1440 >= {BASELINE_ACTIVITY_FLOOR_USD}) AS eligible_m,
        (w_count = 1440 AND w_complete = 1440 AND w_shape_ready = 1440 AND b_count = 10080
         AND b_complete >= {FRAC} * 10080 AND b_complete > 0
         AND (b_act_sum_complete / b_complete) * 1440 >= {BASELINE_ACTIVITY_FLOOR_USD}) AS eligible_s,
        CASE WHEN b_complete > 0 THEN w_net_buy / ((b_act_sum_complete / b_complete) * 1440) END AS score_m,
        w_elevated / 1440.0 AS score_s
    FROM rolled
)
"""
CROSSINGS = """
WITH s AS (SELECT * FROM scored),
eligible AS (SELECT exchange, symbol, bucket_start, {score_col} AS score FROM s WHERE {eligible_col}),
crossings AS (SELECT e.*, lag(score) OVER (PARTITION BY exchange, symbol ORDER BY bucket_start) AS prev_score FROM eligible e)
SELECT exchange, symbol, bucket_start AS fire_ts FROM crossings
WHERE bucket_start >= $cal_start AND bucket_start < $cal_end AND score >= {theta} AND prev_score < {theta}
ORDER BY exchange, symbol, bucket_start
"""

con = duckdb.connect()
con.execute("SET memory_limit='6GB'")
con.execute("SET threads=4")
con.execute("CREATE TEMP TABLE scored AS " + SQL + " SELECT * FROM scored",
            {"parquet": PARQUET, "cal_start": CAL_START, "cal_end": CAL_END})
con.execute(f"""CREATE TEMP TABLE prices AS
    SELECT exchange, symbol, bucket_start, close_price, price_complete FROM read_parquet($parquet)
    WHERE market_type = '{BARS_MARKET_TYPE}' AND capture_version = '{BARS_CAPTURE_VERSION}'
      AND bucket_start >= $cal_start""",
    {"parquet": PARQUET, "cal_start": CAL_START})

holds_sel = ",\n    ".join(
    f"ex{h}.close_price AS c{h}, ex{h}.price_complete AS ok{h}" for h in HOLDS)
holds_join = "\n".join(
    f"LEFT JOIN prices ex{h} ON ex{h}.exchange=fs.exchange AND ex{h}.symbol=fs.symbol "
    f"AND ex{h}.bucket_start = fs.fire_ts + INTERVAL {h} MINUTE" for h in HOLDS)


def dist(vals):
    if not vals:
        return {"n": 0}
    vals = sorted(vals)
    q = statistics.quantiles(vals, n=100) if len(vals) > 1 else [vals[0]] * 99
    return {"n": len(vals), "mean": statistics.fmean(vals), "median": statistics.median(vals),
            "p90": q[89], "p95": q[94], "max": vals[-1],
            "win_rate": sum(1 for x in vals if x > 0) / len(vals),
            "frac_gt_10pct": sum(1 for x in vals if x > 0.10) / len(vals),
            "frac_gt_25pct": sum(1 for x in vals if x > 0.25) / len(vals)}


PRIMARIES = {PRIMARY_MAG: ("score_m", "eligible_m"), PRIMARY_SHAPE: ("score_s", "eligible_s")}
result = {}
for prim, (score_col, eligible_col) in PRIMARIES.items():
    raw = [(str(r[0]), str(r[1]), r[2]) for r in con.execute(
        CROSSINGS.format(score_col=score_col, eligible_col=eligible_col, theta=THETA),
        {"cal_start": CAL_START, "cal_end": CAL_END}).fetchall()]
    deduped = dedup_cooldown(raw)
    con.execute("CREATE OR REPLACE TEMP TABLE fireset(exchange VARCHAR, symbol VARCHAR, fire_ts TIMESTAMPTZ)")
    con.executemany("INSERT INTO fireset VALUES (?,?,?)", deduped)
    rows = con.execute(f"""
        SELECT fs.exchange, fs.symbol, fs.fire_ts, en.close_price AS entry, en.price_complete AS entry_ok,
            {holds_sel}
        FROM fireset fs
        LEFT JOIN prices en ON en.exchange=fs.exchange AND en.symbol=fs.symbol AND en.bucket_start=fs.fire_ts
        {holds_join}
    """).fetchall()
    cols = [d[0] for d in con.description]
    per_hold = {h: {"gross": [], "net": []} for h in HOLDS}
    for r in rows:
        d = dict(zip(cols, r))
        if not (d["entry_ok"] and d["entry"] and d["entry"] > 0):
            continue
        for h in HOLDS:
            c, ok = d[f"c{h}"], d[f"ok{h}"]
            if ok and c and float(c) > 0:
                g = float(c) / float(d["entry"]) - 1.0
                per_hold[h]["gross"].append(g)
                per_hold[h]["net"].append(g - COST)
    result[prim] = {"deduped_fires": len(deduped),
                    "by_hold": {f"{h}m": {"gross": dist(per_hold[h]["gross"]),
                                          "net": dist(per_hold[h]["net"])} for h in HOLDS}}

out = {
    "label": "EXPLORATORY accumulation-LONG multi-hold return distribution (reads outcomes)",
    "source_parquet": "cold-bars-window/bars-window.parquet",
    "source_parquet_sha256": sha256_file(PARQUET),
    "git_rev": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "note": "data ends ~2026-09-11 so long holds only resolve for early fires (n shrinks with hold)",
    "theta": THETA, "holds_min": list(HOLDS), "round_trip_cost": COST, "direction": "long",
    "per_primary": result,
}
out["fingerprint_sha256"] = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
print(json.dumps(out, indent=2, sort_keys=True, default=str))
