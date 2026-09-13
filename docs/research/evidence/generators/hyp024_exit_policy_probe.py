# EXPLORATORY exit-policy comparison for the HYP-024 pump-short, on one pre-chosen
# entry threshold (up>=0.08, spike>=3). Compares a fixed hold against take-profit /
# stop-loss brackets and trailing stops, to see whether dynamic exits beat the fixed
# 240m hold. READS RETURNS; not a verdict.
#
# HARD DATA CAVEAT: the subset has only per-minute close (no high/low), so every exit
# is evaluated at MINUTE CLOSES. Intrabar stop/TP fills and gap-through are invisible,
# which makes backtested stops OPTIMISTIC. A faithful stop backtest needs OHLC or L2.
#
# Short PnL at minute k: (entry - close_k)/entry. Policies (all minus 22 bps at exit):
#   fixed_H       : exit at t+H.
#   tp_sl(tp,sl)  : first close where pnl>=tp or pnl<=-sl, else t+MAXH.
#   trail(tr)     : track best pnl; exit when pnl <= best-tr (armed after pnl>0), else t+MAXH.
#
# Run: uv run --package schurfer-analytics python \
#   docs/research/evidence/generators/hyp024_exit_policy_probe.py
import json, hashlib, subprocess, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
import duckdb
from schurfer_analytics.net_buy_accumulation_v2_calibration import BARS_MARKET_TYPE, BARS_CAPTURE_VERSION

REPO = Path(__file__).resolve().parents[4]
PARQUET = str(REPO / "cold-bars-window" / "bars-window.parquet")
CAL_START = datetime(2026, 8, 18, tzinfo=timezone.utc)
CAL_END = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
COST = 0.0022
COOLDOWN = timedelta(hours=24)
UP, SPIKE, RUNUP, MAXH = 0.08, 3.0, 15, 240


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
      AND bucket_start < $cal_end + INTERVAL {MAXH + 5} MINUTE""",
    {"parquet": PARQUET, "cal_start": CAL_START, "cal_end": CAL_END})

ENTRIES = f"""
WITH base AS (
    SELECT p.exchange, p.symbol, p.bucket_start, p.close_price, p.price_complete, p.activity,
        avg(p.activity) OVER (PARTITION BY p.exchange, p.symbol ORDER BY p.bucket_start
            RANGE BETWEEN INTERVAL 60 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING) AS base60
    FROM prices p
)
SELECT b.exchange, b.symbol, b.bucket_start, b.close_price AS entry_close
FROM base b
LEFT JOIN prices r15 ON r15.exchange = b.exchange AND r15.symbol = b.symbol
    AND r15.bucket_start = b.bucket_start - INTERVAL {RUNUP} MINUTE
WHERE b.bucket_start >= $cal_start AND b.bucket_start < $cal_end
  AND b.price_complete AND b.close_price > 0 AND r15.close_price > 0 AND b.base60 > 0
  AND (b.close_price / r15.close_price - 1) >= {UP}
  AND (b.activity / b.base60) >= {SPIKE}
ORDER BY b.exchange, b.symbol, b.bucket_start
"""
rows = con.execute(ENTRIES, {"cal_start": CAL_START, "cal_end": CAL_END}).fetchall()

# dedup 24h per instrument
kept, last = [], {}
for ex, sym, ts, ec in rows:
    if (ex, sym) in last and ts - last[(ex, sym)] < COOLDOWN:
        continue
    kept.append((ex, sym, ts, float(ec)))
    last[(ex, sym)] = ts

# forward close path per entry
con.execute("CREATE OR REPLACE TEMP TABLE fireset(exchange VARCHAR, symbol VARCHAR, fire_ts TIMESTAMPTZ, entry DOUBLE)")
con.executemany("INSERT INTO fireset VALUES (?,?,?,?)", kept)
path_rows = con.execute(f"""
    SELECT fs.exchange, fs.symbol, fs.fire_ts, fs.entry,
        date_diff('minute', fs.fire_ts, p.bucket_start) AS k, p.close_price, p.price_complete
    FROM fireset fs JOIN prices p ON p.exchange = fs.exchange AND p.symbol = fs.symbol
        AND p.bucket_start > fs.fire_ts AND p.bucket_start <= fs.fire_ts + INTERVAL {MAXH} MINUTE
    ORDER BY fs.exchange, fs.symbol, fs.fire_ts, k
""").fetchall()

paths = {}
for ex, sym, ts, entry, k, close, ok in path_rows:
    paths.setdefault((ex, sym, ts, entry), []).append((int(k), float(close) if close else None, bool(ok)))


def short_pnl(entry, close):
    return (entry - close) / entry


def sim_fixed(entry, path, h):
    for k, close, ok in path:
        if k == h and ok and close:
            return short_pnl(entry, close) - COST
    # fall back to last complete close if the exact h bar is missing
    complete = [(k, c) for k, c, ok in path if ok and c]
    return (short_pnl(entry, complete[-1][1]) - COST) if complete else None


def sim_tp_sl(entry, path, tp, sl):
    for k, close, ok in path:
        if not (ok and close):
            continue
        pnl = short_pnl(entry, close)
        if pnl >= tp or pnl <= -sl:
            return pnl - COST
    complete = [(k, c) for k, c, ok in path if ok and c]
    return (short_pnl(entry, complete[-1][1]) - COST) if complete else None


def sim_trail(entry, path, tr):
    best = 0.0
    armed = False
    for k, close, ok in path:
        if not (ok and close):
            continue
        pnl = short_pnl(entry, close)
        if pnl > best:
            best = pnl
        if pnl > 0:
            armed = True
        if armed and pnl <= best - tr:
            return pnl - COST
    complete = [(k, c) for k, c, ok in path if ok and c]
    return (short_pnl(entry, complete[-1][1]) - COST) if complete else None


def summ(nets):
    nets = [x for x in nets if x is not None]
    if not nets:
        return {"n": 0}
    return {"n": len(nets), "mean_net": statistics.fmean(nets),
            "median_net": statistics.median(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets)}


policies = {}
items = [(key[3], paths[key]) for key in paths]  # (entry_price, forward close path)
for h in (60, 120, 240):
    policies[f"fixed_{h}"] = summ([sim_fixed(e, p, h) for e, p in items])
for tp, sl in [(0.03, 0.03), (0.05, 0.03), (0.05, 0.02)]:
    policies[f"tp{int(tp*100)}_sl{int(sl*100)}"] = summ([sim_tp_sl(e, p, tp, sl) for e, p in items])
for tr in (0.02, 0.03):
    policies[f"trail{int(tr*100)}"] = summ([sim_trail(e, p, tr) for e, p in items])

out = {
    "label": "EXPLORATORY exit-policy comparison (close-based; stops OPTIMISTIC without OHLC)",
    "source_parquet": "cold-bars-window/bars-window.parquet",
    "source_parquet_sha256": sha256_file(PARQUET),
    "git_rev": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "entry_rule": {"up_thresh": UP, "spike_mult": SPIKE, "runup_min": RUNUP},
    "n_entries": len(kept), "round_trip_cost": COST, "direction": "short",
    "data_caveat": "per-minute close only; no high/low, intrabar stop/TP fills invisible",
    "policies": policies,
}
out["fingerprint_sha256"] = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
print(json.dumps(out, indent=2, sort_keys=True, default=str))
