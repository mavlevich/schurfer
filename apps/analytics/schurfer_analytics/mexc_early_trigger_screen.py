"""EXPLORATORY early-trigger screen on archived MEXC 5m bars (burnt window only).

Step 2 of the source-venue early-detection line (ROADMAP, 2026-09-27). The question: if a
pump is caught a few percent into the move on the venue where it starts, instead of at
the +20% scanner threshold, is anything left after costs? This is exploration on the
burnt 2026-08-10..31 window. It nominates at most one rule for a later registered,
untouched forward test, and proves nothing by itself.

Fixed before the first run (after review):

- **Trigger grid, small on purpose.**
  - One closed 5m bar with return (close/open - 1) at least X, with X in TRIGGER_RETURNS.
  - Bar turnover at least VOLUME_MULTIPLE x the median turnover of the prior 24h, and at
    least MIN_BAR_TURNOVER_USD.
  - A prior 24h change below MAX_PRIOR_24H_CHANGE.
  - At most one trigger per symbol per COOLDOWN.
- **Entry.** The open of the next 5m bar. Outcome bars must all close before the window
  end.
- **False trigger.** A trigger that does not reach +15% over its entry within 6h.
- **Frequency.** Per 1,000 eligible instrument-hours. Eligible means a closed 5m bar with 24h
  of contiguous history, under the prior-24h limit, whose horizon fits in the window.
- **Matched control.** For each trigger, the nearest bar of the same symbol and ISO week
  with the same volume filter but a flat return (0..1%), whose prior-24h median turnover
  is within a factor of 2, at least a cooldown away. Unmatched triggers are counted.
- **One sensitivity.** Entry one 5m bar later. It is reported only and never used to
  choose a cell.
- **Two readings.**
  - The MEXC-bar move is a source-venue signal, not a return we can take.
  - The Bybit leg is the executable question: the same trigger entered at the Bybit 5m
    open right after the MEXC bar closes, on the base's Bybit USDT perpetual live at the
    time, with the HYP-012b price-level identity band (2x).
- **Scanner lead.** Approximate: matched by the MEXC USDT swap symbol, the first scanner
  observation within 24h after the trigger.

The screen runs only on a verified archive: every manifest entry `complete` or `empty`,
every sha256 matching, and the pinned window covering the screen window. It reports
symbol and daily coverage before any result.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .mexc_kline_archive import read_rows

if TYPE_CHECKING:
    from collections.abc import Sequence

WINDOW_START = datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 31, tzinfo=UTC)  # exclusive; outcome bars close before it
HISTORY_START = datetime(2026, 8, 9, tzinfo=UTC)  # 24h of history before the first trigger
BAR = 300
DAY_BARS = 288
TRIGGER_RETURNS = (0.03, 0.05, 0.08)
CONTROL_RETURN = (0.0, 0.01)
VOLUME_MULTIPLE = 5.0
MIN_BAR_TURNOVER_USD = 20_000.0
MAX_PRIOR_24H_CHANGE = 0.10
COOLDOWN = DAY_BARS
HORIZONS = {"30m": 6, "1h": 12, "4h": 48, "24h": 288}
LONGEST = max(HORIZONS.values())
CONTINUATION = 0.15
CONTINUATION_BARS = 72  # 6h
SCANNER_LEVEL = 0.20
COST_SCENARIOS_PCT = (0.2, 0.4)
BOOTSTRAP_ITERATIONS = 2000
IDENTITY_BAND = 2.0


@dataclass(frozen=True)
class Episode:
    symbol: str
    i: int
    t: int
    entry: float
    prior_median_turnover: float
    returns: dict[str, float]
    delayed_returns: dict[str, float]
    continued: bool
    scanner_reach: bool
    mae_4h: float
    bybit: dict[str, float] = field(default_factory=dict)
    # The Bybit leg entered one 5m bar later (diagnostic only, never used to choose).
    bybit_delayed: dict[str, float] = field(default_factory=dict)


def verify_archive(archive_dir: Path) -> dict[str, Any]:
    """Refuse an archive that is incomplete, altered or pinned to another window."""
    manifest = json.loads((archive_dir / "manifest.json").read_text())
    if manifest.get("interval") != "Min5":
        raise ValueError("the screen needs the Min5 archive")
    lo, hi = (datetime.fromisoformat(x) for x in manifest["window"])
    if lo > HISTORY_START or hi < WINDOW_END:
        raise ValueError(f"archive window {manifest['window']} does not cover the screen")
    statuses: dict[str, int] = defaultdict(int)
    bad = []
    for symbol, entry in manifest["symbols"].items():
        statuses[entry.get("status", "missing_status")] += 1
        if entry.get("status") not in ("complete", "empty"):
            bad.append(symbol)
            continue
        _, digest = read_rows(archive_dir / f"{symbol}.jsonl.gz")
        if digest != entry["sha256"]:
            bad.append(symbol)
    if bad:
        raise ValueError(
            f"archive not usable: {len(bad)} symbols failed or altered, e.g. {bad[:5]}"
        )
    return {"window": manifest["window"], "statuses": dict(statuses)}


def episodes_for(
    symbol: str, bars: list[dict[str, float]], low: float, high: float
) -> tuple[list[Episode], int]:
    """Triggers with low <= bar return < high under the shared filters and cooldown, and
    the number of eligible bars (the frequency denominator)."""
    start, end = int(WINDOW_START.timestamp()), int(WINDOW_END.timestamp())
    ts = [int(b["t"]) for b in bars]
    out: list[Episode] = []
    eligible = 0
    last = -(10**9)
    for i in range(DAY_BARS, len(bars)):
        b = bars[i]
        if not start <= ts[i] < end:
            continue
        # Contiguous 24h history, and every outcome bar (entry .. entry + LONGEST, plus
        # the one-bar-delayed entry) present, contiguous and closed before the window end.
        if ts[i] - ts[i - DAY_BARS] != DAY_BARS * BAR:
            continue
        last_needed = i + 1 + LONGEST  # the delayed entry's last outcome bar
        if last_needed >= len(bars) or ts[last_needed] - ts[i] != (1 + LONGEST) * BAR:
            continue
        if ts[last_needed] + BAR > end:
            continue
        base = bars[i - DAY_BARS]["o"]
        if base <= 0 or b["o"] <= 0 or b["o"] / base - 1 >= MAX_PRIOR_24H_CHANGE:
            continue
        eligible += 1
        if i - last < COOLDOWN:
            continue
        ret = b["c"] / b["o"] - 1
        if not low <= ret < high:
            continue
        med = median(x["a"] for x in bars[i - DAY_BARS : i])
        if b["a"] < MIN_BAR_TURNOVER_USD or med <= 0 or b["a"] < VOLUME_MULTIPLE * med:
            continue
        j = i + 1
        entry, delayed = bars[j]["o"], bars[j + 1]["o"]
        if entry <= 0 or delayed <= 0:
            continue
        out.append(
            Episode(
                symbol=symbol,
                i=i,
                t=ts[i],
                entry=entry,
                prior_median_turnover=med,
                returns={k: (bars[j + n - 1]["c"] / entry - 1) * 100 for k, n in HORIZONS.items()},
                delayed_returns={
                    k: (bars[j + n]["c"] / delayed - 1) * 100 for k, n in HORIZONS.items()
                },
                continued=max(x["h"] for x in bars[j : j + CONTINUATION_BARS]) / entry - 1
                >= CONTINUATION,
                scanner_reach=max(x["h"] for x in bars[j : j + DAY_BARS]) / base - 1
                >= SCANNER_LEVEL,
                mae_4h=(min(x["l"] for x in bars[j : j + 48]) / entry - 1) * 100,
            )
        )
        last = i
    return out, eligible


def matched_controls(triggers: list[Episode], pool: dict[str, list[Episode]]) -> list[Episode]:
    """One control per trigger: same symbol and ISO week, prior-24h median turnover within
    2x, at least a cooldown away, nearest in time; never reused."""
    used: set[tuple[str, int]] = set()
    out = []
    for trig in sorted(triggers, key=lambda e: (e.symbol, e.t)):
        week = datetime.fromtimestamp(trig.t, UTC).isocalendar()[:2]
        options = [
            c
            for c in pool.get(trig.symbol, [])
            if (c.symbol, c.t) not in used
            and datetime.fromtimestamp(c.t, UTC).isocalendar()[:2] == week
            and abs(c.i - trig.i) >= COOLDOWN
            and 0.5 <= c.prior_median_turnover / trig.prior_median_turnover <= 2.0
        ]
        if options:
            pick = min(options, key=lambda c: (abs(c.t - trig.t), c.t))
            used.add((pick.symbol, pick.t))
            out.append(pick)
    return out


def cluster_ci(values: list[tuple[str, float]], seed_key: str) -> list[float] | None:
    if len({s for s, _ in values}) < 2:
        return None
    estimate = cluster_bootstrap_mean(
        tuple(ClusterObservation(s, v) for s, v in values),
        iterations=BOOTSTRAP_ITERATIONS,
        seed=derived_seed(20_260_927, seed_key),
    ).estimate
    return [round(estimate.lower_bound, 3), round(estimate.upper_bound, 3)]


def summarize(label: str, eps: list[Episode], eligible_bars: int) -> dict[str, Any]:
    if not eps:
        return {"cell": label, "n": 0}
    symbols = {e.symbol for e in eps}
    row: dict[str, Any] = {
        "cell": label,
        "n": len(eps),
        "symbols": len(symbols),
        "per_1000_eligible_instrument_hours": round(len(eps) / (eligible_bars / 12) * 1000, 2),
        "top_symbol_share": round(
            max(sum(1 for e in eps if e.symbol == s) for s in symbols) / len(eps), 3
        ),
        "false_trigger_rate": round(1 - fmean(e.continued for e in eps), 3),
        "scanner_reach_24h": round(fmean(e.scanner_reach for e in eps), 3),
        "mae_4h_median": round(median(e.mae_4h for e in eps), 2),
    }
    for k in HORIZONS:
        vals = [e.returns[k] for e in eps]
        row[f"mexc_mean_{k}"] = round(fmean(vals), 3)
        row[f"mexc_median_{k}"] = round(median(vals), 3)
        row[f"mexc_delayed_mean_{k}"] = round(fmean(e.delayed_returns[k] for e in eps), 3)
    with_bybit = [e for e in eps if e.bybit]
    row["bybit_legs"] = len(with_bybit)
    for k in HORIZONS:
        vals = [e.bybit[k] for e in with_bybit if k in e.bybit]
        row[f"bybit_mean_{k}"] = round(fmean(vals), 3) if vals else None
        row[f"bybit_median_{k}"] = round(median(vals), 3) if vals else None
    for cost in COST_SCENARIOS_PCT:
        for k in ("1h", "4h"):
            legs = [(e.symbol, e.bybit[k] - cost) for e in with_bybit if k in e.bybit]
            row[f"bybit_net_{k}_cost{cost}"] = round(fmean(v for _, v in legs), 3) if legs else None
            row[f"bybit_net_{k}_cost{cost}_ci"] = cluster_ci(legs, f"{label}:{k}:{cost}")
    return row


async def fetch_bybit_inputs(episodes: list[Episode]) -> dict[str, Any]:
    """Fetch the Bybit catalogue and, per trigger with a single live route, the raw 5m bars
    of its hold. Retries on HTTP and API errors; an exhausted retry aborts the whole run,
    so a snapshot never freezes a transient gap."""
    import httpx

    from .source_lead_multi_source_report import (
        BYBIT_KLINE_URL,
        _get_json,
        fetch_bybit_instruments,
        parse_instruments,
    )

    catalogue = await fetch_bybit_instruments()
    instruments = parse_instruments(catalogue)
    rows: dict[str, list[list[str]]] = {}
    semaphore = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=30) as client:

        async def one(e: Episode) -> None:
            native = route(e, instruments)
            if native is None:
                return
            entry_ms = (e.t + BAR) * 1000
            params = {
                "category": "linear",
                "symbol": native,
                "interval": "5",
                "start": str(entry_ms),
                "end": str(entry_ms + LONGEST * BAR * 1000),
                "limit": "400",
            }
            async with semaphore:
                payload = await _get_json(client, BYBIT_KLINE_URL, params)
            listed = (payload.get("result") or {}).get("list") or []
            rows[f"{native}:{entry_ms}"] = sorted(
                ([str(v) for v in r[:6]] for r in listed), key=lambda r: int(r[0])
            )

        await asyncio.gather(*(one(e) for e in episodes))
    return {"bybit_catalogue": catalogue, "bybit_rows": dict(sorted(rows.items()))}


def route(e: Episode, instruments: Sequence[Any]) -> str | None:
    base = e.symbol.removesuffix("_USDT").upper()
    entry_ms = (e.t + BAR) * 1000
    end_ms = entry_ms + (LONGEST * BAR) * 1000
    live = [x for x in instruments if x.base == base and x.live_over(entry_ms, end_ms)]
    return live[0].native_id if len(live) == 1 else None


def attach_bybit_legs(episodes: list[Episode], inputs: dict[str, Any]) -> dict[str, int]:
    """Bybit legs from stored inputs only: entered at the Bybit 5m open right after the
    MEXC bar closes, on the single live route, inside the 2x price band."""
    from .source_lead_multi_source_report import parse_instruments

    instruments = parse_instruments(inputs["bybit_catalogue"])
    status: dict[str, int] = defaultdict(int)
    for e in episodes:
        native = route(e, instruments)
        if native is None:
            status["no_single_bybit_perp"] += 1
            continue
        entry_ms = (e.t + BAR) * 1000
        stored = inputs["bybit_rows"].get(f"{native}:{entry_ms}")
        if stored is None:
            raise ValueError(f"snapshot lacks the Bybit bars of {native} at {entry_ms}")
        rows = {int(r[0]): r for r in stored}
        first = rows.get(entry_ms)
        if first is None or float(first[1]) <= 0:
            status["bybit_missing_entry"] += 1
            continue
        open_ = float(first[1])
        if not 1 / IDENTITY_BAND <= e.entry / open_ <= IDENTITY_BAND:
            status["bybit_price_level_mismatch"] += 1
            continue
        for k, n in HORIZONS.items():
            bar = rows.get(entry_ms + (n - 1) * BAR * 1000)
            if bar is not None:
                e.bybit[k] = (float(bar[4]) / open_ - 1) * 100
        delayed = rows.get(entry_ms + BAR * 1000)
        if delayed is not None and float(delayed[1]) > 0:
            for k, n in HORIZONS.items():
                bar = rows.get(entry_ms + n * BAR * 1000)
                if bar is not None:
                    e.bybit_delayed[k] = (float(bar[4]) / float(delayed[1]) - 1) * 100
        status["bybit_leg"] += 1
    return dict(status)


def diagnostics(eps: list[Episode], key: str = "1h", cost: float = 0.2) -> dict[str, Any]:
    """Concentration and robustness of a cell's Bybit leg (diagnostic only): the largest
    events and symbols, the mean without each symbol, and the one-bar-delayed entry."""
    legs = [(e, e.bybit[key] - cost) for e in eps if key in e.bybit]
    if len(legs) < 2:
        return {"legs": len(legs)}
    total = sum(v for _, v in legs)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for e, v in legs:
        by_symbol[e.symbol].append(v)
    events = sorted(legs, key=lambda ev: -ev[1])
    symbols = sorted(by_symbol.items(), key=lambda kv: -sum(kv[1]))
    without_each = {
        s: fmean(v for e, v in legs if e.symbol != s) for s in by_symbol if len(by_symbol) > 1
    }
    top5 = {id(e) for e, _ in events[:5]}
    delayed = [e.bybit_delayed[key] - cost for e in eps if key in e.bybit_delayed]
    return {
        "legs": len(legs),
        "mean_net": round(fmean(v for _, v in legs), 3),
        "top5_events": [
            [e.symbol, datetime.fromtimestamp(e.t, UTC).isoformat(), round(v, 2)]
            for e, v in events[:5]
        ],
        "top5_events_share_of_total": round(sum(v for _, v in events[:5]) / total, 3)
        if total
        else None,
        "mean_without_top5_events": (
            round(fmean(v for e, v in legs if id(e) not in top5), 3) if len(legs) > 5 else None
        ),
        "top5_symbols": [[s, round(sum(v), 2), len(v)] for s, v in symbols[:5]],
        "mean_without_top_symbol": round(without_each[symbols[0][0]], 3),
        "mean_without_each_symbol_min_max": [
            round(min(without_each.values()), 3),
            round(max(without_each.values()), 3),
        ],
        "median_net": round(median(v for _, v in legs), 3),
        "delayed_one_bar_mean_net": round(fmean(delayed), 3) if delayed else None,
    }


def scanner_lead(episodes: list[Episode], events_csv: Path) -> dict[str, Any]:
    """APPROXIMATE: first scanner observation of the same MEXC USDT swap within 24h."""
    first: dict[str, list[int]] = defaultdict(list)
    with events_csv.open() as handle:
        for row in csv.reader(handle):
            if row[0] == "id" or row[5].lower() != "mexc" or not row[8].endswith("/USDT:USDT"):
                continue
            at = int(datetime.fromisoformat(row[15].replace("+00", "+00:00")).timestamp())
            first[row[8].split("/")[0].upper()].append(at)
    for times in first.values():
        times.sort()
    leads = []
    for e in episodes:
        times = first.get(e.symbol.removesuffix("_USDT").upper(), [])
        k = bisect_left(times, e.t)
        if k < len(times) and times[k] - e.t <= 86400:
            leads.append((times[k] - e.t) / 60)
    return {
        "approximate": True,
        "flagged_later": len(leads),
        "share": round(len(leads) / len(episodes), 3) if episodes else None,
        "median_lead_min": round(median(leads), 1) if leads else None,
    }


def bybit_inputs(
    episodes: list[Episode], snapshot_dir: Path | None, archive_dir: Path
) -> tuple[dict[str, Any], str | None]:
    """The Bybit catalogue and bars, from a write-once snapshot when one exists; otherwise
    fetched live and, with a snapshot directory, frozen before use."""
    from .source_lead_multi_source_report import complete_digest, load_verified, write_once

    path = snapshot_dir / "bybit_inputs.json" if snapshot_dir is not None else None
    if path is not None:
        complete_digest(path)
        if path.exists():
            inputs, digest = load_verified(path)
            manifest_sha = hashlib.sha256((archive_dir / "manifest.json").read_bytes()).hexdigest()
            if inputs.get("archive_manifest_sha256") != manifest_sha:
                raise ValueError("the snapshot was taken on another archive manifest")
            return inputs, digest
    inputs = asyncio.run(fetch_bybit_inputs(episodes))
    if path is None:
        return inputs, None
    inputs["archive_manifest_sha256"] = hashlib.sha256(
        (archive_dir / "manifest.json").read_bytes()
    ).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = write_once(path, inputs)
    return inputs, digest


def run(
    archive_dir: Path, events_csv: Path | None, bybit: bool, snapshot_dir: Path | None = None
) -> dict[str, Any]:
    coverage = verify_archive(archive_dir)
    manifest = json.loads((archive_dir / "manifest.json").read_text())
    triggers: dict[str, list[Episode]] = defaultdict(list)
    pool: dict[str, list[Episode]] = defaultdict(list)
    eligible_bars = 0
    daily_symbols: dict[str, int] = defaultdict(int)
    for symbol in sorted(manifest["symbols"]):
        if manifest["symbols"][symbol]["status"] != "complete":
            continue
        bars, _ = read_rows(archive_dir / f"{symbol}.jsonl.gz")
        for day in {datetime.fromtimestamp(int(b["t"]), UTC).date().isoformat() for b in bars}:
            daily_symbols[day] += 1
        for x in TRIGGER_RETURNS:
            eps, eligible = episodes_for(symbol, bars, x, 10.0)
            triggers[f"ret>={x:.0%}"].extend(eps)
        eligible_bars += eligible
        controls, _ = episodes_for(symbol, bars, *CONTROL_RETURN)
        pool[symbol] = controls
    bybit_status: dict[str, dict[str, int]] = {}
    snapshot_sha: str | None = None
    if bybit:
        inputs, snapshot_sha = bybit_inputs(
            [e for eps in triggers.values() for e in eps], snapshot_dir, archive_dir
        )
        for label, eps in triggers.items():
            bybit_status[label] = attach_bybit_legs(eps, inputs)
    cells = []
    for label, eps in triggers.items():
        cells.append(summarize(label, eps, eligible_bars))
        controls = matched_controls(eps, pool)
        control_row = summarize(f"{label} matched control", controls, eligible_bars)
        control_row["unmatched_triggers"] = len(eps) - len(controls)
        cells.append(control_row)
    result: dict[str, Any] = {
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "note": "exploratory, burnt window, current MEXC listings only (delisted since are absent)",
        "archive": coverage,
        "eligible_instrument_hours": round(eligible_bars / 12, 1),
        "symbols_per_day": dict(sorted(daily_symbols.items())),
        "cells": cells,
        "bybit_leg_status": bybit_status,
        "bybit_snapshot_sha256": snapshot_sha,
        "diagnostics_bybit_1h_cost0.2": {
            label: diagnostics(eps) for label, eps in triggers.items()
        },
    }
    if events_csv is not None:
        result["scanner_lead"] = {
            label: scanner_lead(eps, events_csv) for label, eps in triggers.items()
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--archive-dir", type=Path, required=True, help=".../Min5")
    parser.add_argument("--events-csv", type=Path, default=None)
    parser.add_argument("--no-bybit", dest="bybit", action="store_false")
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=None,
        help="freeze the Bybit catalogue and bars here once; later runs reuse them",
    )
    args = parser.parse_args()
    result = run(args.archive_dir, args.events_csv, args.bybit, args.snapshot_dir)
    sys.stdout.write(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main()
