# EXPLORATORY pump-short probe (HYP-024) on the local cold-bar window. Tests the
# opposite side of the accumulation long: after a short-term run-up plus an activity
# blow-off, SHORT the exhaustion and measure the forward reversal. READS RETURNS, so
# it is not outcome-blind and not a frozen verdict; it is a signal-of-life scan over a
# small threshold grid to see whether any short configuration shows positive after-cost
# EV worth formalizing.
#
# Pump minute t: ret15 = close(t)/close(t-15) - 1 >= up_thresh AND
#   spike = activity(t) / mean(activity over [t-60, t-1]) >= spike_mult.
# Entry: short at close(t). Exit: close(t+H). Short net = (entry - exit)/entry - cost.
# One short per instrument per 24h cooldown. Entry and exit bars must be price_complete.
#
# Run: uv run --package schurfer-analytics python \
#   docs/research/evidence/generators/hyp024_pump_short_probe.py
import json, hashlib, subprocess, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
import duckdb
from schurfer_analytics.net_buy_accumulation_v2_calibration import BARS_MARKET_TYPE, BARS_CAPTURE_VERSION

REPO = Path(__file__).resolve().parents[4]
PARQUET = str(REPO / "cold-bars-window" / "bars-window.parquet")
CAL_START = datetime(2026, 8, 18, tzinfo=timezone.utc)
CAL_END = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
ROUND_TRIP_COST = 0.0022
COOLDOWN = timedelta(hours=24)
HOLDS = (30, 60, 120, 240)
UP_THRESHS = (0.05, 0.08, 0.12)
SPIKE_MULTS = (3.0, 5.0, 8.0)
RUNUP_MIN = 15


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True).stdout.strip()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


con = duckdb.connect()
con.execute("SET memory_limit='6GB'")
con.execute("SET threads=4")
con.execute(f"""CREATE TEMP TABLE prices AS
    SELECT exchange, symbol, bucket_start, close_price, price_complete,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity
    FROM read_parquet($parquet)
    WHERE market_type = '{BARS_MARKET_TYPE}' AND capture_version = '{BARS_CAPTURE_VERSION}'
      AND bucket_start >= $cal_start - INTERVAL 120 MINUTE
      AND bucket_start < $cal_end + INTERVAL {max(HOLDS) + 5} MINUTE""",
    {"parquet": PARQUET, "cal_start": CAL_START, "cal_end": CAL_END})

# Candidate pump minutes at the LOOSEST thresholds; refine per-combo in Python.
holds_select = ",\n    ".join(
    f"ex{h}.close_price AS exit_{h}, ex{h}.price_complete AS exit_ok_{h}" for h in HOLDS)
holds_join = "\n".join(
    f"LEFT JOIN prices ex{h} ON ex{h}.exchange = b.exchange AND ex{h}.symbol = b.symbol "
    f"AND ex{h}.bucket_start = b.bucket_start + INTERVAL {h} MINUTE" for h in HOLDS)

CAND = f"""
WITH base AS (
    SELECT p.exchange, p.symbol, p.bucket_start, p.close_price, p.price_complete, p.activity,
        avg(p.activity) OVER (PARTITION BY p.exchange, p.symbol ORDER BY p.bucket_start
            RANGE BETWEEN INTERVAL 60 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING) AS base60
    FROM prices p
),
feat AS (
    SELECT b.*, r15.close_price AS close_15ago
    FROM base b
    LEFT JOIN prices r15 ON r15.exchange = b.exchange AND r15.symbol = b.symbol
        AND r15.bucket_start = b.bucket_start - INTERVAL {RUNUP_MIN} MINUTE
)
SELECT b.exchange, b.symbol, b.bucket_start,
    b.close_price AS entry_close, b.price_complete AS entry_ok,
    b.close_15ago, b.activity, b.base60,
    {holds_select}
FROM feat b
{holds_join}
WHERE b.bucket_start >= $cal_start AND b.bucket_start < $cal_end
  AND b.price_complete AND b.close_15ago > 0 AND b.close_price > 0 AND b.base60 > 0
  AND (b.close_price / b.close_15ago - 1) >= {min(UP_THRESHS)}
  AND (b.activity / b.base60) >= {min(SPIKE_MULTS)}
ORDER BY b.exchange, b.symbol, b.bucket_start
"""
rows = con.execute(CAND, {"cal_start": CAL_START, "cal_end": CAL_END}).fetchall()
cols = [d[0] for d in con.description]
cands = [dict(zip(cols, r)) for r in rows]


def dedup(items):
    kept, last = [], {}
    for it in sorted(items, key=lambda x: (x["exchange"], x["symbol"], x["bucket_start"])):
        key = (it["exchange"], it["symbol"])
        if key in last and it["bucket_start"] - last[key] < COOLDOWN:
            continue
        kept.append(it)
        last[key] = it["bucket_start"]
    return kept


grid = []
for up in UP_THRESHS:
    for sm in SPIKE_MULTS:
        qual = [c for c in cands
                if (c["entry_close"] / c["close_15ago"] - 1) >= up and (c["activity"] / c["base60"]) >= sm]
        qual = dedup(qual)
        for h in HOLDS:
            nets = []
            for c in qual:
                ex, ok = c[f"exit_{h}"], c[f"exit_ok_{h}"]
                if ok and ex and c["entry_close"] > 0 and float(ex) > 0:
                    short_gross = (c["entry_close"] - float(ex)) / c["entry_close"]
                    nets.append(short_gross - ROUND_TRIP_COST)
            if nets:
                wins = sum(1 for x in nets if x > 0)
                grid.append({"up_thresh": up, "spike_mult": sm, "hold": h, "n": len(nets),
                             "mean_net_short": statistics.fmean(nets),
                             "median_net_short": statistics.median(nets),
                             "win_rate": wins / len(nets)})
            else:
                grid.append({"up_thresh": up, "spike_mult": sm, "hold": h, "n": 0})

out = {
    "label": "EXPLORATORY pump-short probe HYP-024 (reads outcomes; grid scan, not a verdict)",
    "source_parquet": "cold-bars-window/bars-window.parquet",
    "source_parquet_sha256": sha256_file(PARQUET),
    "git_rev": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "cal_window_utc": [CAL_START.isoformat(), CAL_END.isoformat()],
    "runup_lookback_min": RUNUP_MIN, "cooldown_hours": 24, "round_trip_cost": ROUND_TRIP_COST,
    "direction": "short", "candidate_minutes_loose": len(cands),
    "grid": grid,
}
out["fingerprint_sha256"] = hashlib.sha256(
    json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
print(json.dumps(out, indent=2, sort_keys=True, default=str))
