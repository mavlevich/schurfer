"""Bybit 1-minute burst: seconds-level decay v1 (descriptive).

Protocol: docs/research/bybit-burst-decay-v1.md. Nothing here is a trading rule and
nothing claims executable economics (no quotes, costs or size). Every price below is a
trade-price proxy.

Phases (each checks the frozen contract):

- `firings` freezes the population: the paired 1-minute burst set of the edge-loss
  readout. It reads only reduced bars that match their manifests and the bars pinned
  by the read-2 `inputs.json`, records the code revision, and refuses any count but
  EXPECTED_FIRINGS.
- `fetch` downloads, once and locally, the Bybit public trade files of the
  instrument-days the firings need, with sha256 and a 20 GiB cap (counting files
  already on disk).
- `read` computes the measures from the frozen firings and the verified files, reading
  each file once, and writes the result once.
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
    verify_bars,
)
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Sequence

    import duckdb

DECAY_VERSION = "bybit_burst_decay_v1"
ARCHIVE = "https://public.bybit.com/trading/{symbol}/{symbol}{day}.csv.gz"
BURST_RETURN = 0.05
PAIRED_OFFSETS_MIN = (1, 2, 3, 6, 61)
EXPECTED_FIRINGS = 732
EXIT_AFTER_S = 3600.0  # the exit is the first trade at or after B + 60 min (the t+61 open)
EXIT_WAIT_S = 60.0
ENTRY_WAIT_S = 2.0
DELAYS_S = (0.0, 0.25, 0.5, 1.0, 2.0, 2.7, 5.0, 10.0, 20.0, 30.0, 46.0, 60.0)
KEY_DELAYS_S = (2.7, 46.0)
STALE_S = 5.0
MAX_BYTES = 20 * 1024**3
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_261_005
FIRINGS_NAME = "decay-firings.json"
TAPES_NAME = "decay-tapes.json"
RESULT_NAME = "decay-result.json"
Trade = tuple[float, float, float]  # time, price, USDT notional


def contract_sha256() -> str:
    """Pins the protocol's fixed parameters; every phase checks it."""
    contract = {
        "version": DECAY_VERSION,
        "burst_return": BURST_RETURN,
        "paired_offsets_min": PAIRED_OFFSETS_MIN,
        "expected_firings": EXPECTED_FIRINGS,
        "exit": ["first trade at or after B + s", EXIT_AFTER_S, "wait", EXIT_WAIT_S],
        "entry": ["first trade at or after B + d", "wait", ENTRY_WAIT_S],
        "secondary": "last trade at or before B + d, with its age",
        "delays_s": DELAYS_S,
        "key_delays_s": KEY_DELAYS_S,
        "stale_s": STALE_S,
        "max_bytes": MAX_BYTES,
        "bootstrap": [BOOTSTRAP_ITERATIONS, BOOTSTRAP_SEED],
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def check_contract(payload: dict[str, Any], what: str) -> None:
    if payload.get("contract_sha256") != contract_sha256():
        raise SystemExit(f"{what} was made under another contract; refusing")


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
                "t61_open": opens_[-1],
            }
        )
    return out


def verified_bars_against_read2(bars_dir: Path, pinned_inputs: Path) -> str:
    """The reduced bars must match their manifests and the bars read 2 pinned; returns
    the pinned inputs' sha256."""
    pinned, pinned_sha = load_verified(pinned_inputs)
    if verify_bars(bars_dir) != pinned["bars"]:
        raise SystemExit("the reduced bars differ from the ones read 2 pinned; refusing")
    return pinned_sha


def tape_days(firings: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    """(symbol, UTC day) pairs covering each burst minute and its exit wait."""
    days: set[tuple[str, str]] = set()
    for f in firings:
        first = datetime.fromtimestamp(f["bar_start"], UTC).date()
        last = datetime.fromtimestamp(f["bar_start"] + 60 + EXIT_AFTER_S + EXIT_WAIT_S, UTC).date()
        day = first
        while day <= last:
            days.add((f["symbol"], day.isoformat()))
            day += timedelta(days=1)
    return sorted(days)


# ---------------------------------------------------------------- fetch


def fetch_tapes(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    import httpx

    payload, firings_sha = load_verified(stage_dir / FIRINGS_NAME)
    check_contract(payload, "the firing list")
    needed = tape_days(payload["firings"])
    late = [f"{s} {d}" for s, d in needed if date.fromisoformat(d) >= BLIND_END.date()]
    if late:
        raise SystemExit(f"days on or after the blind boundary: {', '.join(late)}")
    tapes_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    total = 0
    status: Counter[str] = Counter()

    def count(size: int) -> None:
        nonlocal total
        total += size
        if total > MAX_BYTES:
            raise SystemExit("the 20 GiB cap was reached (files on disk count too)")

    with httpx.Client(timeout=300, follow_redirects=True) as client:
        for symbol, day in needed:
            name = f"{symbol}{day}.csv.gz"
            path = tapes_dir / name
            if path.exists():
                count(path.stat().st_size)
            else:
                with client.stream("GET", ARCHIVE.format(symbol=symbol, day=day)) as response:
                    if response.status_code == 404:
                        files[name] = {"status": "not_in_archive"}
                        status["not_in_archive"] += 1
                        continue
                    response.raise_for_status()
                    partial = path.with_suffix(".partial")
                    try:
                        with partial.open("wb") as out:
                            for block in response.iter_bytes():
                                count(len(block))
                                out.write(block)
                    except SystemExit:
                        partial.unlink(missing_ok=True)
                        raise
                    partial.rename(path)
            files[name] = {
                "status": "ok",
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            status["ok"] += 1
    return {
        "decay_version": DECAY_VERSION,
        "contract_sha256": contract_sha256(),
        "firings_sha256": firings_sha,
        "status": dict(status),
        "bytes": total,
        "files": files,
    }


# ---------------------------------------------------------------- trades


def read_trades(path: Path) -> list[Trade]:
    """A day's trades in time order; trades at one timestamp keep the file's row order
    (the archive has no sequence id), never a price order."""
    import duckdb

    rows = (
        duckdb.connect()
        .execute(
            "SELECT timestamp::DOUBLE, price::DOUBLE, foreignNotional::DOUBLE"
            " FROM (SELECT *, row_number() OVER () AS row FROM read_csv(?, header = true,"
            " all_varchar = true)) ORDER BY timestamp::DOUBLE, row",
            [str(path)],
        )
        .fetchall()
    )
    return [(float(t), float(p), float(n)) for t, p, n in rows if float(p) > 0]


class Trades:
    """Point-in-time trade prices of one instrument."""

    def __init__(self, trades: Sequence[Trade]) -> None:
        self.times = [t for t, _, _ in trades]
        self.prices = [p for _, p, _ in trades]
        self.notional = [n for _, _, n in trades]

    def last_at(self, at: float) -> tuple[float, float] | None:
        """(price, age) of the last trade at or before `at`."""
        i = bisect.bisect_right(self.times, at) - 1
        return None if i < 0 else (self.prices[i], at - self.times[i])

    def first_from(self, at: float, wait: float) -> tuple[float, float] | None:
        """(price, time) of the first trade at or after `at`, if within `wait` seconds."""
        i = bisect.bisect_left(self.times, at)
        if i >= len(self.times) or self.times[i] - at > wait:
            return None
        return self.prices[i], self.times[i]

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


def _matches(price: float, bar_open: float) -> bool:
    return abs(price - bar_open) <= 1e-9 * bar_open


def measure(trades: Trades, firing: dict[str, Any]) -> dict[str, Any]:
    """One firing's measures. B is the burst bar's end; X the exit (the t+61 open)."""
    b = float(firing["bar_start"]) + 60
    exit_ = trades.first_from(b + EXIT_AFTER_S, EXIT_WAIT_S)
    if exit_ is None:
        return {"status": "exit_unavailable"}
    entry = trades.first_from(b, ENTRY_WAIT_S)
    if entry is None:
        return {"status": "entry_unavailable"}
    x = exit_[0]
    out: dict[str, Any] = {
        "status": "ok",
        "checks": {
            "entry_wait_s": entry[1] - b,
            "entry_matches_t1_open": _matches(entry[0], firing["t1_open"]),
            "exit_wait_s": exit_[1] - b - EXIT_AFTER_S,
            "exit_matches_t61_open": _matches(x, firing["t61_open"]),
        },
        "g_first_bps": (x / entry[0] - 1) * 1e4,
        "first_after": {},
        "last_before": {},
    }
    for d in DELAYS_S:
        key = f"{d:g}"
        after = trades.first_from(b + d, ENTRY_WAIT_S)
        out["first_after"][key] = (
            None
            if after is None
            else {"g_bps": (x / after[0] - 1) * 1e4, "wait_s": after[1] - b - d}
        )
        before = trades.last_at(b + d)
        out["last_before"][key] = (
            None if before is None else {"g_bps": (x / before[0] - 1) * 1e4, "age_s": before[1]}
        )
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
    """g at the first trade after B minus g at the first trade after B + delay, over the
    firings where both exist."""
    pairs = [
        (r, r["m"]["g_first_bps"] - r["m"]["first_after"][delay]["g_bps"])
        for r in rows
        if r["m"]["first_after"].get(delay) is not None
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


def _curve(ok: Sequence[dict[str, Any]], kind: str, age_field: str) -> dict[str, Any]:
    curve: dict[str, Any] = {}
    for d in DELAYS_S:
        key = f"{d:g}"
        cells = [r["m"][kind][key] for r in ok if r["m"][kind][key] is not None]
        g = [c["g_bps"] for c in cells]
        curve[key] = {
            "missing": len(ok) - len(cells),
            "mean_bps": fmean(g) if g else None,
            **quantiles(g),
            f"{age_field}_over_{STALE_S:g}s_share": (
                sum(c[age_field] > STALE_S for c in cells) / len(cells) if cells else None
            ),
        }
    return curve


def summarize(rows: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    ok = [r for r in rows if r["m"]["status"] == "ok"]
    checks = [r["m"]["checks"] for r in ok]
    intrabar = [r for r in ok if "intrabar" in r["m"]]
    by_symbol: dict[str, float] = defaultdict(float)
    by_week: dict[int, list[float]] = defaultdict(list)
    key = f"{KEY_DELAYS_S[0]:g}"
    for r in ok:
        cell = r["m"]["first_after"][key]
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
        "checks": {
            "entry_not_t1_open": sum(not c["entry_matches_t1_open"] for c in checks),
            "exit_not_t61_open": sum(not c["exit_matches_t61_open"] for c in checks),
            "entry_wait_s": quantiles([c["entry_wait_s"] for c in checks]),
            "exit_wait_s": quantiles([c["exit_wait_s"] for c in checks]),
        },
        "g_first_bps": {
            "mean_bps": fmean(r["m"]["g_first_bps"] for r in ok) if ok else None,
            **quantiles([r["m"]["g_first_bps"] for r in ok]),
        },
        "decay_first_trade_after": _curve(ok, "first_after", "wait_s"),
        "last_trade_before_secondary": _curve(ok, "last_before", "age_s"),
        "paired_loss_vs_first_trade": {
            f"{d:g}": paired_loss(ok, f"{d:g}", seed) for d in KEY_DELAYS_S
        },
        "intrabar_conditional_on_a_burst_bar": {
            "share_detected_before_b": len(intrabar) / len(ok) if ok else None,
            "seconds_before_b": quantiles(
                [r["m"]["intrabar"]["seconds_before_b"] for r in intrabar]
            ),
            "g_bps": quantiles([r["m"]["intrabar"]["g_bps"] for r in intrabar]),
            "paired_vs_first_after_b_mean_bps": (
                fmean(r["m"]["intrabar"]["g_bps"] - r["m"]["g_first_bps"] for r in intrabar)
                if intrabar
                else None
            ),
        },
        "concentration_of_paired_loss_at_2_7s": {
            "top5_instrument_share": (top5 / total) if total else None,
            "iso_week_mean_bps": {w: [len(v), fmean(v)] for w, v in sorted(by_week.items())},
        },
    }


def read(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: read once")
    payload, firings_sha = load_verified(stage_dir / FIRINGS_NAME)
    check_contract(payload, "the firing list")
    tapes, tapes_sha = load_verified(stage_dir / TAPES_NAME)
    check_contract(tapes, "the tape record")
    if tapes["firings_sha256"] != firings_sha:
        raise ValueError("the tapes were fetched for another firing list")
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for firing in payload["firings"]:
        by_symbol[firing["symbol"]].append(firing)
    rows: list[dict[str, Any]] = []
    for _symbol, firings in sorted(by_symbol.items()):
        names = [f"{s}{d}.csv.gz" for s, d in tape_days(firings)]
        missing = {n for n in names if tapes["files"].get(n, {}).get("status") != "ok"}
        loaded: list[Trade] = []
        for name in names:
            if name in missing:
                continue
            path = tapes_dir / name
            if sha256_file(path) != tapes["files"][name]["sha256"]:
                raise ValueError(f"{name} does not match its recorded sha256")
            loaded += read_trades(path)  # each file once per instrument
        trades = Trades(sorted(loaded, key=lambda t: t[0]))
        for firing in firings:
            own = {f"{s}{d}.csv.gz" for s, d in tape_days([firing])}
            m = {"status": "no_tape"} if own & missing else measure(trades, firing)
            day = datetime.fromtimestamp(firing["bar_start"], UTC).date().isoformat()
            rows.append({**firing, "day": day, "m": m})
    result = {
        "decay_version": DECAY_VERSION,
        "contract_sha256": contract_sha256(),
        "firings_sha256": firings_sha,
        "tapes_sha256": tapes_sha,
        "firings_code_revision": payload["code_revision"],
        **summarize(rows, BOOTSTRAP_SEED),
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("firings", "fetch", "read"), required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--bars-dir", type=Path)
    parser.add_argument("--pinned-inputs", type=Path, help="read-2 inputs.json")
    parser.add_argument("--tapes-dir", type=Path)
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args(argv)
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "firings":
        import duckdb

        from .mexc_early_trigger_hyp029 import verified_revision

        if args.bars_dir is None or args.pinned_inputs is None:
            raise SystemExit("--bars-dir and --pinned-inputs are required")
        revision = verified_revision(args.code_revision)
        pinned_sha = verified_bars_against_read2(args.bars_dir, args.pinned_inputs)
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        load_bars(con, args.bars_dir, [])
        firings = freeze_firings(con)
        if len(firings) != EXPECTED_FIRINGS:
            raise SystemExit(f"{len(firings)} firings, not the readout's {EXPECTED_FIRINGS}")
        digest = write_once(
            args.stage_dir / FIRINGS_NAME,
            {
                "decay_version": DECAY_VERSION,
                "contract_sha256": contract_sha256(),
                "code_revision": revision,
                "read2_inputs_sha256": pinned_sha,
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


if __name__ == "__main__":
    main()
