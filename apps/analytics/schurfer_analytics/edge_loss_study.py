"""Where the edge is lost: detection-delay decomposition v1 (Bybit, Binance, MEXC bars).

Registered before any read: docs/research/edge-loss-decomposition-v1.md, with amendment 1.
Nothing on or after 2026-09-29T00:00Z is read: every bar, exit and peak ends before it.

- **Part A** (descriptive, no verdict): for each scanner pump source (24h change of at
  least +20%) the move's timeline from 1-minute bars. Move start (the 24h low before the
  bar-based +20% crossing), the 5-minute crossings of +3/+5/+10%, the 24h crossing, the
  scanner's own `first_seen_at`. At each moment, the share of the move (low to the 24h
  peak) already gone.
- **Part B** (one primary contrast): every firing of a fixed trigger on every instrument,
  whether or not a pump followed. Primary: Bybit, 5-minute return of at least +5% on the
  closed bar, long at the next minute open, exit 60 minutes later, one firing per
  instrument per 60 minutes, middle cost scenario. The minimum detectable effect and the
  counts are computed and written before the mean. Every other cell is descriptive.

Phases: `scanner-export` (on the production host, read-only) freezes the scanner's
sources of the window. `read` (on a machine with the reduced bars and the MEXC archive)
verifies every input against its manifest, records the inputs once, and writes the
result once. The Gate tick-tape part is a separate module.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean, pstdev
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .edge_loss_bars import WINDOW_FIRST, WINDOW_LAST, reduced_name
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import duckdb

STUDY_VERSION = "edge_loss_decomposition_v1_amendment1"
BLIND_END = datetime(2026, 9, 29, tzinfo=UTC)  # nothing at or after it is read
SCANNER_FROM = datetime(2026, 7, 23, tzinfo=UTC)  # the scanner's records start here
MEXC_FIRST = date(2026, 8, 28)
MINUTE = 60
DAY_MINUTES = 1440
PUMP_THRESHOLD = 0.20
FIVE_MINUTE_THRESHOLDS = (0.03, 0.05, 0.10)
ONE_MINUTE_THRESHOLDS = (0.02, 0.03, 0.05)
ONE_MINUTE_TURNOVER_MULTIPLE = 5.0
ONE_MINUTE_MEDIAN_MIN_BARS = 30  # of the 60 prior minutes, for the turnover median
HORIZONS = (15, 60, 240)
COOLDOWN_MINUTES = 60
FEE_BPS = 5.5  # Bybit published base-tier linear taker rate; not measured
SLIPPAGE_BPS = (5.0, 15.0, 40.0)
COST_SCENARIOS_BPS = tuple(FEE_BPS + s for s in SLIPPAGE_BPS)  # per side
PRIMARY_COST_BPS = COST_SCENARIOS_BPS[1]
SENSITIVITY_COST_BPS = 10.0 + 15.0  # descriptive only
BINANCE_PRICE_COVERAGE = 0.99
BAR_READY_S = 2.7  # Bybit, measured
WATCH_ENTRY_S = 46.0  # measured paper entry after the bar closes
SCANNER_CYCLE_S = 101.0  # measured median real cycle
BOOTSTRAP_ITERATIONS = 10_000
Z_ALPHA = 1.959964  # two-sided 5%
Z_POWER = 0.841621  # 80% power
PRIMARY = {
    "venue": "bybit",
    "family": "five_minute",
    "threshold": 0.05,
    "side": "long",
    "horizon": 60,
    "cost_bps_per_side": PRIMARY_COST_BPS,
}
CONTRACT: dict[str, Any] = {
    "study_version": STUDY_VERSION,
    "bybit_window": [WINDOW_FIRST.isoformat(), WINDOW_LAST.isoformat()],
    "mexc_first_day": MEXC_FIRST.isoformat(),
    "blind_end": BLIND_END.isoformat(),
    "pump_threshold": PUMP_THRESHOLD,
    "five_minute_thresholds": FIVE_MINUTE_THRESHOLDS,
    "one_minute": {
        "thresholds": ONE_MINUTE_THRESHOLDS,
        "turnover_multiple": ONE_MINUTE_TURNOVER_MULTIPLE,
        "median_window_minutes": 60,
        "median_min_bars": ONE_MINUTE_MEDIAN_MIN_BARS,
    },
    "horizons_minutes": HORIZONS,
    "cooldown_minutes": COOLDOWN_MINUTES,
    "entry": "open of the minute after the trigger bar; exit at the open h minutes later",
    "fee_bps_published": FEE_BPS,
    "cost_scenarios_bps_per_side": COST_SCENARIOS_BPS,
    "sensitivity_cost_bps_per_side": SENSITIVITY_COST_BPS,
    "binance_price_coverage": BINANCE_PRICE_COVERAGE,
    "delays_s": {
        "bar_ready": BAR_READY_S,
        "watch_entry": WATCH_ENTRY_S,
        "scanner_cycle": SCANNER_CYCLE_S,
    },
    "primary": PRIMARY,
    "bootstrap": {"iterations": BOOTSTRAP_ITERATIONS, "clusters": ["instrument", "utc_day"]},
    "mde": {"z_alpha": Z_ALPHA, "z_power": Z_POWER, "se": "max of the two bootstrap sds"},
}
SCANNER_SQL = """
SELECT exchange, symbol, event_id, first_seen_at
FROM app.pump_event_sources
WHERE exchange IN ('bybit', 'binance', 'mexc', 'gate')
  AND first_seen_at >= :since AND first_seen_at < :until
ORDER BY exchange, first_seen_at, symbol, event_id
"""
INPUTS_NAME = "inputs.json"
RESULT_NAME = "result.json"
SCANNER_NAME = "scanner.json"
Row = tuple[Any, ...]


def contract_sha256() -> str:
    return hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()


def bootstrap_seed() -> int:
    return int(contract_sha256()[:12], 16)


# ---------------------------------------------------------------- inputs


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def window_days(first: date, last: date) -> list[str]:
    return [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]


def verify_bars(bars_dir: Path) -> list[dict[str, Any]]:
    """Every window day must be reduced, and match its manifest."""
    out = []
    for day in window_days(WINDOW_FIRST, WINDOW_LAST):
        path = bars_dir / reduced_name(day)
        manifest_path = path.with_suffix(".manifest.json")
        if not path.exists() or not manifest_path.exists():
            raise ValueError(f"{day}: the reduced bars or their manifest are missing")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("day") != day or sha256_file(path) != manifest.get("reduced_sha256"):
            raise ValueError(f"{day}: {path.name} does not match its manifest")
        out.append(
            {
                "day": day,
                "reduced_sha256": manifest["reduced_sha256"],
                "source_sha256": manifest["source_sha256"],
                "rows_by_exchange": manifest["rows_by_exchange"],
            }
        )
    return out


def verify_mexc(archive_dir: Path) -> tuple[list[Path], str]:
    """The MEXC 1m files the manifest marks complete, each checked against its sha256."""
    manifest_path = archive_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("failures"):
        raise ValueError("the MEXC archive records failures")
    files = []
    for symbol, entry in sorted(manifest["symbols"].items()):
        if entry.get("status") != "complete":
            continue
        path = archive_dir / f"{symbol}.jsonl.gz"
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"{path.name} does not match the MEXC manifest")
        files.append(path)
    return files, sha256_file(manifest_path)


def load_bars(con: duckdb.DuckDBPyConnection, bars_dir: Path, mexc_files: list[Path]) -> None:
    """One table `bars`: usable minute bars (all four prices positive) of every venue."""
    paths = [str(bars_dir / reduced_name(d)) for d in window_days(WINDOW_FIRST, WINDOW_LAST)]
    con.execute(
        """
        CREATE TABLE bars AS
        SELECT exchange, symbol, bucket_start AS t,
               open_price AS o, high_price AS h, low_price AS l, close_price AS c,
               coalesce(buy_total_notional_usd, 0) + coalesce(sell_total_notional_usd, 0) AS n
        FROM read_parquet(?)
        WHERE open_price > 0 AND high_price > 0 AND low_price > 0 AND close_price > 0
          AND bucket_start < ?
        """,
        [paths, BLIND_END],
    )
    if mexc_files:
        con.execute(
            """
            INSERT INTO bars
            SELECT 'mexc', regexp_extract(filename, '([^/]+)\\.jsonl\\.gz$', 1),
                   to_timestamp(t), o, h, l, c, coalesce(a, 0)
            FROM read_json(?, format = 'newline_delimited', compression = 'gzip',
                           filename = true,
                           columns = {t: 'BIGINT', o: 'DOUBLE', h: 'DOUBLE', l: 'DOUBLE',
                                      c: 'DOUBLE', v: 'DOUBLE', a: 'DOUBLE'})
            WHERE o > 0 AND h > 0 AND l > 0 AND c > 0
              AND to_timestamp(t) >= ? AND to_timestamp(t) < ?
            """,
            [
                [str(p) for p in mexc_files],
                datetime.combine(MEXC_FIRST, datetime.min.time(), UTC),
                BLIND_END,
            ],
        )


def binance_first_day(con: duckdb.DuckDBPyConnection, bars_dir: Path) -> str | None:
    """The first UTC day on which at least 99% of Binance bars carry price_complete.

    A coverage rule on the reduced files, decided before any price is read.
    """
    paths = [str(bars_dir / reduced_name(d)) for d in window_days(WINDOW_FIRST, WINDOW_LAST)]
    rows = con.execute(
        """
        SELECT CAST(bucket_start AT TIME ZONE 'UTC' AS DATE) AS day,
               avg(CASE WHEN price_complete THEN 1.0 ELSE 0.0 END) AS share
        FROM read_parquet(?) WHERE exchange = 'binance' GROUP BY 1 ORDER BY 1
        """,
        [paths],
    ).fetchall()
    for day, share in rows:
        if share >= BINANCE_PRICE_COVERAGE:
            return str(day.isoformat())
    return None


# ---------------------------------------------------------------- statistics


def quantiles(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "q25": None, "median": None, "q75": None}
    ordered = sorted(values)

    def at(p: float) -> float:
        pos = (len(ordered) - 1) * p
        lo, hi = math.floor(pos), math.ceil(pos)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)

    return {"n": len(ordered), "q25": at(0.25), "median": at(0.5), "q75": at(0.75)}


def primary_inference(observations: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    """MDE first, then the mean and the two one-way cluster intervals and the verdict."""
    values = [o["net_bps"] for o in observations]
    if len(values) < 2:
        return {"firings": len(values), "verdict": "not_established", "reason": "too_few"}
    boots: dict[str, Any] = {}
    for scheme in ("instrument", "utc_day"):
        obs = tuple(ClusterObservation(o[scheme], o["net_bps"]) for o in observations)
        comp = cluster_bootstrap_mean(
            obs, iterations=BOOTSTRAP_ITERATIONS, seed=derived_seed(seed, f"primary:{scheme}")
        )
        boots[scheme] = comp
    se = max(pstdev(boots[s].samples) for s in boots)
    report: dict[str, Any] = {
        "firings": len(values),
        "clusters": {s: boots[s].estimate.clusters for s in boots},
        "se_bps": se,
        "mde_bps_80pct_power": (Z_ALPHA + Z_POWER) * se,
    }
    # Only after the MDE is fixed in the report:
    report["mean_net_bps"] = fmean(values)
    report["ci95_bps"] = {
        s: [boots[s].estimate.lower_bound, boots[s].estimate.upper_bound] for s in boots
    }
    lowers = [boots[s].estimate.lower_bound for s in boots]
    uppers = [boots[s].estimate.upper_bound for s in boots]
    if all(x > 0 for x in lowers):
        report["verdict"] = "positive_established"
    elif all(x < 0 for x in uppers):
        report["verdict"] = "negative_established"
    else:
        report["verdict"] = "not_established"
    return report


# ---------------------------------------------------------------- part B


_FORWARD = """
SELECT f.exchange, f.symbol, epoch(f.t)::BIGINT AS ts, f.r,
       e.o AS entry, {exits}
FROM {cands} f
LEFT JOIN bars e ON e.exchange = f.exchange AND e.symbol = f.symbol
                AND e.t = f.t + INTERVAL 1 MINUTE
{joins}
ORDER BY f.exchange, f.symbol, f.t
"""


def _forward_sql(cands: str) -> str:
    exits = ", ".join(f"x{h}.o AS x{h}" for h in HORIZONS)
    joins = "\n".join(
        f"LEFT JOIN bars x{h} ON x{h}.exchange = f.exchange AND x{h}.symbol = f.symbol"
        f" AND x{h}.t = f.t + INTERVAL {h + 1} MINUTE"
        for h in HORIZONS
    )
    return _FORWARD.format(cands=cands, exits=exits, joins=joins)


def trigger_candidates(con: duckdb.DuckDBPyConnection) -> dict[str, list[Row]]:
    """Every bar passing the loosest threshold of each family, with forward opens."""
    con.execute(
        """
        CREATE TABLE cand5 AS
        SELECT a.exchange, a.symbol, a.t, a.c / p.c - 1 AS r
        FROM bars a JOIN bars p
          ON p.exchange = a.exchange AND p.symbol = a.symbol
         AND p.t = a.t - INTERVAL 5 MINUTE
        WHERE a.c / p.c - 1 >= ?
        """,
        [min(FIVE_MINUTE_THRESHOLDS)],
    )
    con.execute(
        """
        CREATE TABLE cand1 AS
        WITH w AS (
          SELECT exchange, symbol, t, c, n,
                 lag(c) OVER s AS pc, lag(t) OVER s AS pt,
                 median(n) OVER r AS med, count(*) OVER r AS cnt
          FROM bars
          WINDOW s AS (PARTITION BY exchange, symbol ORDER BY t),
                 r AS (PARTITION BY exchange, symbol ORDER BY t
                       RANGE BETWEEN INTERVAL 60 MINUTE PRECEDING
                                 AND INTERVAL 1 MINUTE PRECEDING)
        )
        SELECT exchange, symbol, t, c / pc - 1 AS r FROM w
        WHERE pt = t - INTERVAL 1 MINUTE AND c / pc - 1 >= ?
          AND cnt >= ? AND med > 0 AND n >= ? * med
        """,
        [min(ONE_MINUTE_THRESHOLDS), ONE_MINUTE_MEDIAN_MIN_BARS, ONE_MINUTE_TURNOVER_MULTIPLE],
    )
    return {
        "five_minute": con.execute(_forward_sql("cand5")).fetchall(),
        "one_minute": con.execute(_forward_sql("cand1")).fetchall(),
    }


def firings(rows: Iterable[Row], venue: str, threshold: float, first_ts: int) -> list[Row]:
    """Rows of one venue at or above the threshold, at most one per instrument per hour."""
    kept: list[Row] = []
    last: dict[str, int] = {}
    for row in rows:
        exchange, symbol, ts, r = row[0], row[1], row[2], row[3]
        if exchange != venue or r < threshold or ts < first_ts:
            continue
        if symbol in last and ts < last[symbol] + COOLDOWN_MINUTES * MINUTE:
            continue
        last[symbol] = ts
        kept.append(row)
    return kept


def outcomes(rows: Sequence[Row], horizon: int, side: str) -> dict[str, Any]:
    """Resolve each firing at a horizon: censored by the window end, unresolved (an
    entry or exit bar missing), or a gross return in bps."""
    end = int(BLIND_END.timestamp())
    index = 5 + HORIZONS.index(horizon)
    resolved, unresolved, censored = [], 0, 0
    for row in rows:
        ts, entry, exit_ = row[2], row[4], row[index]
        if ts + (horizon + 2) * MINUTE > end:
            censored += 1
            continue
        if entry is None or exit_ is None:
            unresolved += 1
            continue
        gross = exit_ / entry - 1 if side == "long" else 1 - exit_ / entry
        day = datetime.fromtimestamp(ts, UTC).date().isoformat()
        resolved.append({"instrument": row[1], "utc_day": day, "gross_bps": gross * 1e4})
    return {"resolved": resolved, "unresolved": unresolved, "censored": censored}


def describe(resolved: Sequence[dict[str, Any]], unresolved: int, censored: int) -> dict[str, Any]:
    gross = [o["gross_bps"] for o in resolved]
    cell: dict[str, Any] = {
        "firings": len(resolved) + unresolved + censored,
        "resolved": len(resolved),
        "unresolved": unresolved,
        "censored_by_window_end": censored,
        "instruments": len({o["instrument"] for o in resolved}),
        "gross_bps": {"mean": fmean(gross) if gross else None, **quantiles(gross)},
        "net_mean_bps": {
            f"{c:g}": (fmean(gross) - 2 * c if gross else None)
            for c in (*COST_SCENARIOS_BPS, SENSITIVITY_COST_BPS)
        },
    }
    return cell


def part_b(
    candidates: dict[str, list[Row]], venue_first: dict[str, date | None], seed: int
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    primary: dict[str, Any] | None = None
    families = {"five_minute": FIVE_MINUTE_THRESHOLDS, "one_minute": ONE_MINUTE_THRESHOLDS}
    for venue, first in venue_first.items():
        if first is None:
            cells.append({"venue": venue, "status": "no_eligible_days"})
            continue
        first_ts = int(datetime.combine(first, datetime.min.time(), UTC).timestamp())
        for family, thresholds in families.items():
            for threshold in thresholds:
                fired = firings(candidates[family], venue, threshold, first_ts)
                for side in ("long", "short"):
                    for horizon in HORIZONS:
                        got = outcomes(fired, horizon, side)
                        key = {
                            "venue": venue,
                            "family": family,
                            "threshold": threshold,
                            "side": side,
                            "horizon": horizon,
                        }
                        is_primary = all(
                            PRIMARY[k] == key[k]
                            for k in ("venue", "family", "threshold", "side", "horizon")
                        )
                        if is_primary:
                            obs = [
                                {**o, "net_bps": o["gross_bps"] - 2 * PRIMARY_COST_BPS}
                                for o in got["resolved"]
                            ]
                            primary = {
                                **key,
                                "unresolved": got["unresolved"],
                                "censored_by_window_end": got["censored"],
                                **primary_inference(obs, seed),
                            }
                        cells.append(
                            {**key, **describe(got["resolved"], got["unresolved"], got["censored"])}
                        )
    return {"primary": primary, "descriptive_cells": cells}


# ---------------------------------------------------------------- part A


class Series:
    """One instrument's minute bars, indexed by epoch minute."""

    def __init__(self, rows: Sequence[Row]) -> None:
        self.at: dict[int, tuple[float, float, float, float]] = {
            int(ts) // MINUTE: (o, h, lo, c) for ts, o, h, lo, c in rows
        }
        self.minutes = sorted(self.at)

    def close(self, m: int) -> float | None:
        bar = self.at.get(m)
        return bar[3] if bar else None

    def open(self, m: int) -> float | None:
        bar = self.at.get(m)
        return bar[0] if bar else None

    def change(self, m: int, back: int) -> float | None:
        now, then = self.close(m), self.close(m - back)
        return now / then - 1 if now and then else None

    def span(self, first: int, last: int) -> list[int]:
        lo = bisect.bisect_left(self.minutes, first)
        hi = bisect.bisect_right(self.minutes, last)
        return self.minutes[lo:hi]


def scanner_entry_weights() -> dict[int, float]:
    """The scanner's entry bar after the crossing bar m: available BAR_READY_S after the
    close, detected after a uniform phase of a SCANNER_CYCLE_S cycle, entered at the
    first minute open after detection: offset k means the open of bar m + k."""
    weights: dict[int, float] = {}
    k = 2
    while True:
        lo = (k - 2) * MINUTE
        hi = (k - 1) * MINUTE
        a = max(lo, BAR_READY_S)
        b = min(hi, BAR_READY_S + SCANNER_CYCLE_S)
        if b > a:
            weights[k] = (b - a) / SCANNER_CYCLE_S
        if lo >= BAR_READY_S + SCANNER_CYCLE_S:
            return weights
        k += 1


def timeline(series: Series, first_seen: datetime, data_end_minute: int) -> dict[str, Any]:
    """One scanner source's move: statuses or per-moment shares of the move gone."""
    seen_m = int(first_seen.timestamp()) // MINUTE
    if not series.minutes or series.minutes[0] > seen_m - 2 * DAY_MINUTES:
        return {"status": "insufficient_history"}
    cross = None
    for m in range(seen_m - 1, seen_m - 1 - DAY_MINUTES, -1):
        now, prior = series.change(m, DAY_MINUTES), series.change(m - 1, DAY_MINUTES)
        if now is not None and prior is not None and now >= PUMP_THRESHOLD > prior:
            cross = m
            break
    if cross is None:
        return {"status": "no_bar_crossing"}
    if cross + DAY_MINUTES >= data_end_minute:
        return {"status": "censored_peak"}
    prior_bars = series.span(cross - DAY_MINUTES, cross)
    next_bars = series.span(cross, cross + DAY_MINUTES)
    if not prior_bars or not next_bars:
        return {"status": "insufficient_bars"}
    low_m = min(prior_bars, key=lambda m: series.at[m][2])
    low = series.at[low_m][2]
    peak = max(series.at[m][1] for m in next_bars)
    if peak <= low:
        return {"status": "no_move"}

    def share(price: float | None) -> float | None:
        return None if price is None else (price - low) / (peak - low)

    crossings: dict[str, int | None] = {}
    for k in FIVE_MINUTE_THRESHOLDS:
        crossings[f"5m_{k:g}"] = next(
            (
                m
                for m in series.span(low_m, cross)
                if (r := series.change(m, 5)) is not None and r >= k
            ),
            None,
        )
    crossings["24h_0.2"] = cross
    moments: dict[str, dict[str, float | None]] = {}
    weights = scanner_entry_weights()
    for name, at in crossings.items():
        if at is None:
            moments[name] = {"status_not_crossed": 1.0}
            continue
        row: dict[str, float | None] = {
            "minutes_after_move_start": float(at + 1 - low_m),
            "crossing_close": share(series.close(at)),
            "stream_ideal_next_open": share(series.open(at + 1)),
            "watch_path_entry": share(series.open(at + 1 + math.ceil(WATCH_ENTRY_S / MINUTE))),
        }
        if name == "24h_0.2":
            opens = [(weights[k], share(series.open(at + k))) for k in weights]
            row["scanner_model_entry"] = (
                sum(w * v for w, v in opens if v is not None)
                if all(v is not None for _, v in opens)
                else None
            )
            seen_open_m = math.ceil(first_seen.timestamp() / MINUTE)
            row["scanner_actual_entry"] = share(series.open(seen_open_m))
            row["scanner_lag_s"] = first_seen.timestamp() - (at + 1) * MINUTE
        moments[name] = row
    return {"status": "ok", "moments": moments}


def part_a(
    con: duckdb.DuckDBPyConnection,
    scanner: Sequence[dict[str, Any]],
    venue_first: dict[str, date | None],
) -> dict[str, Any]:
    end_minute = int(BLIND_END.timestamp()) // MINUTE
    out: dict[str, Any] = {}
    for venue, first in venue_first.items():
        if first is None:
            out[venue] = {"status": "no_eligible_days"}
            continue
        start = datetime.combine(first, datetime.min.time(), UTC)
        events = [
            e for e in scanner if e["exchange"] == venue and start <= e["first_seen_at"] < BLIND_END
        ]
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            by_symbol[event["symbol"]].append(event)
        statuses: Counter[str] = Counter()
        values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        not_crossed: Counter[str] = Counter()
        for symbol, items in sorted(by_symbol.items()):
            rows = con.execute(
                "SELECT epoch(t)::BIGINT, o, h, l, c FROM bars"
                " WHERE exchange = ? AND symbol = ? ORDER BY t",
                [venue, symbol],
            ).fetchall()
            series = Series(rows)
            for event in items:
                got = timeline(series, event["first_seen_at"], end_minute)
                statuses[got["status"]] += 1
                if got["status"] != "ok":
                    continue
                for threshold, row in got["moments"].items():
                    if "status_not_crossed" in row:
                        not_crossed[threshold] += 1
                        continue
                    for moment, value in row.items():
                        if value is not None:
                            values[threshold][moment].append(value)
        out[venue] = {
            "first_day": first.isoformat(),
            "scanner_sources": len(events),
            "statuses": dict(statuses),
            "not_crossed_before_24h_crossing": dict(not_crossed),
            "moments": {
                threshold: {moment: quantiles(v) for moment, v in sorted(moments.items())}
                for threshold, moments in sorted(values.items())
            },
        }
    return out


# ---------------------------------------------------------------- phases


async def export_scanner(db_url: str) -> list[dict[str, Any]]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(SCANNER_SQL), {"since": SCANNER_FROM, "until": BLIND_END}
            )
            return [
                {
                    "exchange": r.exchange,
                    "symbol": r.symbol,
                    "event_id": r.event_id,
                    "first_seen_at": r.first_seen_at.astimezone(UTC).isoformat(),
                }
                for r in result
            ]
    finally:
        await engine.dispose()


def run_read(
    stage_dir: Path,
    bars_dir: Path,
    mexc_dir: Path,
    revision: str,
    now: datetime,
) -> dict[str, Any]:
    import duckdb

    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: the study is read once")
    scanner_payload, scanner_sha = load_verified(stage_dir / SCANNER_NAME)
    bars = verify_bars(bars_dir)
    mexc_files, mexc_manifest_sha = verify_mexc(mexc_dir)
    inputs = {
        "study_version": STUDY_VERSION,
        "contract_sha256": contract_sha256(),
        "reader_code_revision": revision,
        "taken_at": now.isoformat(),
        "scanner_sha256": scanner_sha,
        "bars": bars,
        "mexc_manifest_sha256": mexc_manifest_sha,
        "mexc_files": len(mexc_files),
    }
    inputs_sha = write_once(stage_dir / INPUTS_NAME, inputs)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    binance_first = binance_first_day(con, bars_dir)
    load_bars(con, bars_dir, mexc_files)
    venue_first: dict[str, date | None] = {
        "bybit": WINDOW_FIRST,
        "binance": date.fromisoformat(binance_first) if binance_first else None,
        "mexc": MEXC_FIRST,
    }
    scanner = [
        {**e, "first_seen_at": datetime.fromisoformat(e["first_seen_at"])}
        for e in scanner_payload["sources"]
    ]
    seed = bootstrap_seed()
    candidates = trigger_candidates(con)
    result = {
        "study_version": STUDY_VERSION,
        "inputs_sha256": inputs_sha,
        "contract_sha256": contract_sha256(),
        "reader_code_revision": revision,
        "venue_first_day": {k: v.isoformat() if v else None for k, v in venue_first.items()},
        "part_b": part_b(candidates, venue_first, seed),
        "part_a": part_a(con, scanner, venue_first),
        "scanner_entry_weights": scanner_entry_weights(),
        "caveats": [
            "fees are Bybit's published base-tier rate, not measured fills",
            "MEXC holds only symbols listed on 2026-09-27; its costs are not measured",
            "minute bars quantize every delay below a minute to the next open",
        ],
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


def verified_revision(claimed: str | None) -> str:
    from .mexc_early_trigger_hyp029 import verified_revision as check

    return check(claimed)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("scanner-export", "read"), required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--bars-dir", type=Path)
    parser.add_argument("--mexc-dir", type=Path, help=".../mexc_klines/Min1")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args(argv)
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    if args.phase == "scanner-export":
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            raise SystemExit("DATABASE_URL is required")
        sources = asyncio.run(export_scanner(db_url))
        digest = write_once(
            args.stage_dir / SCANNER_NAME,
            {"study_version": STUDY_VERSION, "exported_at": now.isoformat(), "sources": sources},
        )
        counts = Counter(s["exchange"] for s in sources)
        sys.stdout.write(json.dumps({"sha256": digest, "sources": dict(counts)}) + "\n")
        return
    if args.bars_dir is None or args.mexc_dir is None:
        raise SystemExit("--bars-dir and --mexc-dir are required for the read")
    revision = verified_revision(args.code_revision)
    result = run_read(args.stage_dir, args.bars_dir, args.mexc_dir, revision, now)
    primary = result["part_b"]["primary"] or {}
    sys.stdout.write(
        json.dumps({k: primary.get(k) for k in ("firings", "mde_bps_80pct_power", "verdict")})
        + "\n"
    )


if __name__ == "__main__":
    main()
