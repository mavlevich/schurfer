"""Bybit 1-minute burst: seconds-level decay v1 (descriptive).

Protocol: docs/research/bybit-burst-decay-v1.md. Nothing here is a trading rule and
nothing claims executable economics (no quotes, costs or size).

Phases:

- `firings` freezes the population from the edge-loss reduced bars (read-2 rules): the
  paired 1-minute burst set, with each firing's previous close and prior turnover median
  for the intrabar measure.
- `fetch` downloads, once and locally, the Bybit public trade files of the
  instrument-days the firings need, with sha256 and a 20 GiB cap.
- `read` computes the protocol's measures from the frozen firings and the verified
  files, and writes the result once.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .edge_loss_study import (
    BLIND_END,
    COOLDOWN_MINUTES,
    ONE_MINUTE_MEDIAN_MIN_BARS,
    ONE_MINUTE_TURNOVER_MULTIPLE,
    load_bars,
    quantiles,
    sha256_file,
)
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Sequence

    import duckdb

DECAY_VERSION = "bybit_burst_decay_v1"
ARCHIVE = "https://public.bybit.com/trading/{symbol}/{symbol}{day}.csv.gz"
BURST_RETURN = 0.05
PAIRED_OFFSETS_MIN = (1, 2, 3, 6, 61)
EXIT_S = 3600.0  # the exit is the last trade at or before B + 60 min
DELAYS_S = (0.0, 0.25, 0.5, 1.0, 2.0, 2.7, 5.0, 10.0, 20.0, 30.0, 46.0, 60.0)
KEY_DELAYS_S = (2.7, 46.0)
STALE_S = 5.0
OPEN_MATCH_S = 1.0
MAX_BYTES = 20 * 1024**3
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_261_005
FIRINGS_NAME = "decay-firings.json"
TAPES_NAME = "decay-tapes.json"
RESULT_NAME = "decay-result.json"
Trade = tuple[float, float, float]  # time, price, USDT notional


# ---------------------------------------------------------------- firings


def freeze_firings(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """The paired burst set of the edge-loss readout, Bybit only."""
    con.execute(
        """
        CREATE TABLE burst AS
        WITH w AS (
          SELECT symbol, t, c, n, flow_ok,
                 lag(c) OVER s AS pc, lag(t) OVER s AS pt,
                 median(n) FILTER (WHERE flow_ok) OVER r AS med,
                 count(*) FILTER (WHERE flow_ok) OVER r AS cnt
          FROM good WHERE exchange = 'bybit'
          WINDOW s AS (PARTITION BY symbol ORDER BY t),
                 r AS (PARTITION BY symbol ORDER BY t
                       RANGE BETWEEN INTERVAL 60 MINUTE PRECEDING
                                 AND INTERVAL 1 MINUTE PRECEDING)
        )
        SELECT symbol, t, pc, med, c / pc - 1 AS r FROM w
        WHERE pt = t - INTERVAL 1 MINUTE AND c / pc - 1 >= ? AND flow_ok
          AND cnt >= ? AND med > 0 AND n >= ? * med
        """,
        [BURST_RETURN, ONE_MINUTE_MEDIAN_MIN_BARS, ONE_MINUTE_TURNOVER_MULTIPLE],
    )
    joins = " ".join(
        f"LEFT JOIN good e{k} ON e{k}.exchange = 'bybit' AND e{k}.symbol = f.symbol"
        f" AND e{k}.t = f.t + INTERVAL {k} MINUTE"
        for k in PAIRED_OFFSETS_MIN
    )
    opens = ", ".join(f"e{k}.o" for k in PAIRED_OFFSETS_MIN)
    rows = con.execute(
        f"SELECT f.symbol, epoch(f.t)::BIGINT, f.pc, f.med, {opens}"  # noqa: S608 -- fixed names
        f" FROM burst f {joins} ORDER BY f.symbol, f.t"
    ).fetchall()
    out: list[dict[str, Any]] = []
    last: dict[str, int] = {}
    end = int(BLIND_END.timestamp())
    for symbol, ts, prev_close, median_turnover, *opens_ in rows:
        if symbol in last and ts < last[symbol] + COOLDOWN_MINUTES * 60:
            continue
        last[symbol] = ts
        if any(o is None for o in opens_) or ts + 62 * 60 > end:
            continue
        out.append(
            {
                "symbol": symbol,
                "bar_start": ts,
                "prev_close": prev_close,
                "median_turnover_usd": median_turnover,
                "t1_open": opens_[0],
            }
        )
    return out


def tape_days(firings: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    """(symbol, UTC day) pairs covering each burst minute and its exit."""
    days: set[tuple[str, str]] = set()
    for f in firings:
        first = datetime.fromtimestamp(f["bar_start"] - 60, UTC).date()
        last = datetime.fromtimestamp(f["bar_start"] + 60 + EXIT_S, UTC).date()
        day = first
        while day <= last:
            days.add((f["symbol"], day.isoformat()))
            day += timedelta(days=1)
    return sorted(days)


# ---------------------------------------------------------------- fetch


def fetch_tapes(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    import httpx

    payload, firings_sha = load_verified(stage_dir / FIRINGS_NAME)
    needed = tape_days(payload["firings"])
    late = [
        f"{symbol} {day}" for symbol, day in needed if date.fromisoformat(day) >= BLIND_END.date()
    ]
    if late:
        raise SystemExit(f"days on or after the blind boundary: {', '.join(late)}")
    tapes_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    total = 0
    status: Counter[str] = Counter()
    with httpx.Client(timeout=300, follow_redirects=True) as client:
        for symbol, day in needed:
            name = f"{symbol}{day}.csv.gz"
            path = tapes_dir / name
            if not path.exists():
                with client.stream("GET", ARCHIVE.format(symbol=symbol, day=day)) as response:
                    if response.status_code == 404:
                        files[name] = {"status": "not_in_archive"}
                        status["not_in_archive"] += 1
                        continue
                    response.raise_for_status()
                    partial = path.with_suffix(".partial")
                    with partial.open("wb") as out:
                        for block in response.iter_bytes():
                            total += len(block)
                            if total > MAX_BYTES:
                                partial.unlink()
                                raise SystemExit("the 20 GiB download cap was reached")
                            out.write(block)
                    partial.rename(path)
            else:
                total += path.stat().st_size
            files[name] = {
                "status": "ok",
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            status["ok"] += 1
    return {
        "decay_version": DECAY_VERSION,
        "firings_sha256": firings_sha,
        "status": dict(status),
        "bytes": total,
        "files": files,
    }


# ---------------------------------------------------------------- trades


def read_trades(path: Path, lo: float, hi: float) -> list[Trade]:
    """Trades with lo <= time <= hi in the file's own row order (no sequence id exists;
    ties at one timestamp keep their row order, never a price order)."""
    import duckdb

    rows = (
        duckdb.connect()
        .execute(
            "SELECT timestamp::DOUBLE, price::DOUBLE, foreignNotional::DOUBLE"
            " FROM (SELECT *, row_number() OVER () AS row FROM read_csv(?, header = true,"
            " all_varchar = true)) WHERE timestamp::DOUBLE BETWEEN ? AND ?"
            " ORDER BY timestamp::DOUBLE, row",
            [str(path), lo, hi],
        )
        .fetchall()
    )
    return [(float(t), float(p), float(n)) for t, p, n in rows if float(p) > 0]


class Trades:
    """Point-in-time prices from one window of trades."""

    def __init__(self, trades: Sequence[Trade]) -> None:
        self.times = [t for t, _, _ in trades]
        self.prices = [p for _, p, _ in trades]
        self.notional = [n for _, _, n in trades]

    def last_at(self, at: float) -> tuple[float, float] | None:
        """(price, age) of the last trade at or before `at`."""
        i = bisect.bisect_right(self.times, at) - 1
        return None if i < 0 else (self.prices[i], at - self.times[i])

    def first_from(self, at: float) -> tuple[float, float] | None:
        """(price, time) of the first trade at or after `at`."""
        i = bisect.bisect_left(self.times, at)
        return None if i >= len(self.times) else (self.prices[i], self.times[i])

    def intrabar_detection(
        self, start: float, end: float, prev_close: float, median_turnover: float
    ) -> tuple[float, float] | None:
        """(time, price) of the first trade in [start, end) at which the price is at least
        BURST_RETURN above the previous close and the minute's turnover so far is at
        least the turnover multiple of the prior median."""
        i = bisect.bisect_left(self.times, start)
        turnover = 0.0
        while i < len(self.times) and self.times[i] < end:
            turnover += self.notional[i]
            if (
                self.prices[i] >= prev_close * (1 + BURST_RETURN)
                and turnover >= ONE_MINUTE_TURNOVER_MULTIPLE * median_turnover
            ):
                return self.times[i], self.prices[i]
            i += 1
        return None


def measure(trades: Trades, firing: dict[str, Any]) -> dict[str, Any]:
    """One firing's measures. B is the burst bar's end; X the fixed exit."""
    b = float(firing["bar_start"]) + 60
    exit_ = trades.last_at(b + EXIT_S)
    first = trades.first_from(b)
    if exit_ is None or first is None:
        return {"status": "no_trades"}
    x = exit_[0]
    out: dict[str, Any] = {
        "status": "ok",
        "open_check": {
            "first_trade_after_b_s": first[1] - b,
            "matches_bar_open": abs(first[0] - firing["t1_open"]) <= 1e-9 * firing["t1_open"],
        },
        "g_first_bps": (x / first[0] - 1) * 1e4,
        "delays": {},
    }
    for d in DELAYS_S:
        got = trades.last_at(b + d)
        if got is None:
            out["delays"][f"{d:g}"] = None
            continue
        price, age = got
        out["delays"][f"{d:g}"] = {"g_bps": (x / price - 1) * 1e4, "age_s": age}
    detected = trades.intrabar_detection(
        b - 60, b, firing["prev_close"], firing["median_turnover_usd"]
    )
    if detected is not None:
        out["intrabar"] = {
            "seconds_before_b": b - detected[0],
            "g_bps": (x / detected[1] - 1) * 1e4,
        }
    return out


# ---------------------------------------------------------------- read


def paired_loss(rows: Sequence[dict[str, Any]], delay: str, seed: int) -> dict[str, Any]:
    pairs = [
        (r, r["m"]["g_first_bps"] - r["m"]["delays"][delay]["g_bps"])
        for r in rows
        if r["m"]["delays"].get(delay) is not None
    ]
    out: dict[str, Any] = {"n": len(pairs)}
    if len(pairs) < 2:
        return out
    out["mean_bps"] = fmean(v for _, v in pairs)
    for scheme, field in (("instrument", "symbol"), ("utc_day", "day")):
        boot = cluster_bootstrap_mean(
            tuple(ClusterObservation(str(r[field]), v) for r, v in pairs),
            iterations=BOOTSTRAP_ITERATIONS,
            seed=derived_seed(seed, f"{delay}:{scheme}"),
        )
        out[f"ci95_by_{scheme}"] = [boot.estimate.lower_bound, boot.estimate.upper_bound]
    return out


def summarize(rows: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    ok = [r for r in rows if r["m"]["status"] == "ok"]
    checks = [r["m"]["open_check"] for r in ok]
    curve: dict[str, Any] = {}
    for d in DELAYS_S:
        key = f"{d:g}"
        cells = [r["m"]["delays"][key] for r in ok if r["m"]["delays"][key] is not None]
        g = [c["g_bps"] for c in cells]
        curve[key] = {
            "mean_bps": fmean(g) if g else None,
            **quantiles(g),
            "stale_share": (sum(c["age_s"] > STALE_S for c in cells) / len(cells))
            if cells
            else None,
        }
    intrabar = [r for r in ok if "intrabar" in r["m"]]
    intra_diff = [
        r["m"]["intrabar"]["g_bps"] - r["m"]["delays"]["0"]["g_bps"]
        for r in intrabar
        if r["m"]["delays"]["0"]
    ]
    by_symbol: dict[str, float] = defaultdict(float)
    by_week: dict[int, list[float]] = defaultdict(list)
    key = f"{KEY_DELAYS_S[0]:g}"
    for r in ok:
        cell = r["m"]["delays"][key]
        if cell is None:
            continue
        loss = r["m"]["g_first_bps"] - cell["g_bps"]
        by_symbol[r["symbol"]] += loss
        by_week[datetime.fromtimestamp(r["bar_start"], UTC).isocalendar().week].append(loss)
    total = sum(by_symbol.values())
    top5 = sum(sorted(by_symbol.values(), reverse=True)[:5])
    return {
        "firings": len(rows),
        "statuses": dict(Counter(r["m"]["status"] for r in rows)),
        "open_check": {
            "mismatching_bar_open": sum(not c["matches_bar_open"] for c in checks),
            "first_trade_later_than_1s": sum(
                c["first_trade_after_b_s"] > OPEN_MATCH_S for c in checks
            ),
        },
        "g_first_bps": {"mean_bps": fmean(r["m"]["g_first_bps"] for r in ok) if ok else None},
        "decay_curve": curve,
        "paired_loss_vs_first_trade": {
            f"{d:g}": paired_loss(ok, f"{d:g}", seed) for d in KEY_DELAYS_S
        },
        "intrabar": {
            "share_detected_before_b": len(intrabar) / len(ok) if ok else None,
            "seconds_before_b": quantiles(
                [r["m"]["intrabar"]["seconds_before_b"] for r in intrabar]
            ),
            "g_bps": quantiles([r["m"]["intrabar"]["g_bps"] for r in intrabar]),
            "paired_vs_b_mean_bps": fmean(intra_diff) if intra_diff else None,
        },
        "concentration_at_2_7s": {
            "top5_instrument_share_of_loss": (top5 / total) if total else None,
            "iso_week_mean_bps": {w: [len(v), fmean(v)] for w, v in sorted(by_week.items())},
        },
    }


def read(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: read once")
    payload, firings_sha = load_verified(stage_dir / FIRINGS_NAME)
    tapes, tapes_sha = load_verified(stage_dir / TAPES_NAME)
    if tapes["firings_sha256"] != firings_sha:
        raise ValueError("the tapes were fetched for another firing list")
    rows: list[dict[str, Any]] = []
    for firing in payload["firings"]:
        b = float(firing["bar_start"]) + 60
        trades: list[Trade] = []
        missing = False
        for symbol, day in tape_days([firing]):
            name = f"{symbol}{day}.csv.gz"
            entry = tapes["files"].get(name)
            if entry is None or entry.get("status") != "ok":
                missing = True
                break
            path = tapes_dir / name
            if sha256_file(path) != entry["sha256"]:
                raise ValueError(f"{name} does not match its recorded sha256")
            trades += read_trades(path, b - 61, b + EXIT_S)
        day = datetime.fromtimestamp(firing["bar_start"], UTC).date().isoformat()
        m = {"status": "no_tape"} if missing else measure(Trades(trades), firing)
        rows.append({**firing, "day": day, "m": m})
    result = {
        "decay_version": DECAY_VERSION,
        "contract_sha256": contract_sha256(),
        "firings_sha256": firings_sha,
        "tapes_sha256": tapes_sha,
        **summarize(rows, BOOTSTRAP_SEED),
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("firings", "fetch", "read"), required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--bars-dir", type=Path)
    parser.add_argument("--tapes-dir", type=Path)
    args = parser.parse_args(argv)
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "firings":
        import duckdb

        if args.bars_dir is None:
            raise SystemExit("--bars-dir is required")
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        load_bars(con, args.bars_dir, [])
        firings = freeze_firings(con)
        digest = write_once(
            args.stage_dir / FIRINGS_NAME,
            {
                "decay_version": DECAY_VERSION,
                "contract_sha256": contract_sha256(),
                "firings": firings,
            },
        )
        sys.stdout.write(json.dumps({"sha256": digest, "firings": len(firings)}) + "\n")
        return
    if args.tapes_dir is None:
        raise SystemExit("--tapes-dir is required")
    if args.phase == "fetch":
        if (args.stage_dir / TAPES_NAME).exists():
            raise SystemExit("the tapes are recorded once")
        tapes = fetch_tapes(args.stage_dir, args.tapes_dir)
        digest = write_once(args.stage_dir / TAPES_NAME, tapes)
        sys.stdout.write(json.dumps({"sha256": digest, "status": tapes["status"]}) + "\n")
        return
    result = read(args.stage_dir, args.tapes_dir)
    sys.stdout.write(json.dumps({"statuses": result["statuses"]}) + "\n")


def contract_sha256() -> str:
    """Pins the protocol's fixed parameters."""
    contract = {
        "version": DECAY_VERSION,
        "burst_return": BURST_RETURN,
        "paired_offsets_min": PAIRED_OFFSETS_MIN,
        "exit_s": EXIT_S,
        "delays_s": DELAYS_S,
        "key_delays_s": KEY_DELAYS_S,
        "stale_s": STALE_S,
        "bootstrap": [BOOTSTRAP_ITERATIONS, BOOTSTRAP_SEED],
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    main()
