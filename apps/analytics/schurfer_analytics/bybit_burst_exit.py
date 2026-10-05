"""Bybit 1-minute burst: exit management v1 (exploratory).

Protocol: docs/research/bybit-burst-exit-management-v1.md. Data before 2026-09-29 only.
Nothing here is a trading rule; quotes are not fills, and both halves lie inside the
window in which the trigger was found.

One phase, `read`, from inputs that are all already published and pinned by sha256: the
frozen decay firings, the decay study's trade tapes (through its result), and the cost
study's order books and funding. Per instrument the books are read twice: once for the
entry and the scheduled moments, then, once the trade tape has given the triggers, for
the triggered fills. The result is written once.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from . import bybit_burst_cost as cost
from . import bybit_burst_decay as decay
from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .edge_loss_study import quantiles, sha256_file
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_VERSION = "bybit_burst_exit_management_v1"
BOOKS_SHA256 = "9a6da537d1217a4916f358ef6e01d8d610a9b06b86baaf9eef81e79e36be7174"
FUNDING_SHA256 = "c0cb3efce63a96e7b2e8d35a87692236e5c0c1e3242680bc406d5473f9cec6ca"
ENTRY_DELAY_S = 5.0
ENTRY_WAIT_S = decay.ENTRY_WAIT_S  # the first trade at or after B + 5 s, within 2 s
HOLD_S = 3600.0  # H: B + 60 min, the cost study's exit
CHECK_S = 900.0  # E: B + 15 min
STOP = 0.03
TRAIL = 0.03
TAKE = 0.05
EXIT_DELAY_S = 5.0  # a decided exit (S, T, P, and E when it exits) fills this much later
EXITS = ("H", "S", "T", "E", "P")
SPLIT = datetime(2026, 9, 7, tzinfo=UTC)  # choice half before, test half from
MIN_COMMON_SHARE = 0.6
MIN_FIRINGS = 150
MIN_INSTRUMENTS = 30
MIN_DAYS = 10
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_261_007
RESULT_NAME = "exit-result.json"


def contract_sha256() -> str:
    contract = {
        "version": EXIT_VERSION,
        "firings_sha256": cost.DECAY_FIRINGS_SHA256,
        "decay_result_sha256": cost.DECAY_RESULT_SHA256,
        "books_sha256": BOOKS_SHA256,
        "funding_sha256": FUNDING_SHA256,
        "entry": ["first trade at or after B + s, within", ENTRY_DELAY_S, ENTRY_WAIT_S],
        "entry_price": "executable average price of USD 50 walked through the asks",
        "exits": {
            "H": HOLD_S,
            "S": STOP,
            "T": TRAIL,
            "E": CHECK_S,
            "P": TAKE,
        },
        "exit_delay_s": EXIT_DELAY_S,
        "notional_usd": cost.NOTIONAL_USD,
        "fee_bps": cost.FEE_BPS,
        "split": SPLIT.isoformat(),
        "minimums": [MIN_COMMON_SHARE, MIN_FIRINGS, MIN_INSTRUMENTS, MIN_DAYS],
        "bootstrap": [BOOTSTRAP_ITERATIONS, BOOTSTRAP_SEED],
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def to_ms(seconds: float) -> int:
    return round(seconds * 1000)


def scheduled_moments(firing: dict[str, Any]) -> list[int]:
    """The book moments known before any trade is read (ms)."""
    b = float(firing["bar_start"]) + 60
    return [
        to_ms(b + ENTRY_DELAY_S),
        to_ms(b + CHECK_S),
        to_ms(b + CHECK_S + EXIT_DELAY_S),
        to_ms(b + HOLD_S),
    ]


def entry_of(firing: dict[str, Any], trades: decay.Trades, book: dict[str, Any]) -> dict[str, Any]:
    """The executable entry at B + 5 s: USD 50 walked through the asks."""
    b = float(firing["bar_start"]) + 60
    if trades.first_from(b + ENTRY_DELAY_S, ENTRY_WAIT_S) is None:
        return {"status": "entry_unavailable"}
    if book["status"] != "ok":
        return {"status": book["status"]}
    bought = cost.vwap(book["asks"], notional=cost.NOTIONAL_USD)
    if bought is None:
        return {"status": "depth_short"}
    price, qty = bought
    return {
        "status": "ok",
        "at": b + ENTRY_DELAY_S,
        "price": price,
        "qty": qty,
        "notional": price * qty,
    }


def decisions(firing: dict[str, Any], trades: decay.Trades, entry_price: float) -> dict[str, Any]:
    """Per exit: the decision time (s) and why, from trades strictly after the entry and
    before B + 60 min. S, T and P trigger on a trade; E decides at B + 15 min on the last
    trade at or before it; otherwise each falls back to H."""
    b = float(firing["bar_start"]) + 60
    start, end = b + ENTRY_DELAY_S, b + HOLD_S
    out: dict[str, Any] = {"H": None, "S": None, "T": None, "E": None, "P": None}
    high = float("-inf")
    i = bisect.bisect_right(trades.times, start)
    while i < len(trades.times) and trades.times[i] < end:
        t, p = trades.times[i], trades.prices[i]
        high = max(high, p)
        if out["S"] is None and p <= entry_price * (1 - STOP):
            out["S"] = t
        if out["T"] is None and p <= high * (1 - TRAIL):
            out["T"] = t
        if out["P"] is None and p >= entry_price * (1 + TAKE):
            out["P"] = t
        if out["S"] is not None and out["T"] is not None and out["P"] is not None:
            break
        i += 1
    last = trades.last_at(b + CHECK_S)
    if last is not None and last[0] < entry_price:
        out["E"] = b + CHECK_S
    return out


def fill_moments(firing: dict[str, Any], decided: dict[str, Any], delay_s: float) -> dict[str, Any]:
    """Per exit: (fill moment ms, reason). A decided exit fills `delay_s` after its
    decision; an exit that never decides falls back to H at B + 60 min."""
    hold = to_ms(float(firing["bar_start"]) + 60 + HOLD_S)
    out: dict[str, Any] = {}
    for exit_id in EXITS:
        at = decided[exit_id]
        if at is None:
            out[exit_id] = (hold, "time")
        else:
            out[exit_id] = (to_ms(at + delay_s), "decided")
    return out


def settle(
    entry: dict[str, Any],
    book: dict[str, Any],
    fill_ms: int,
    funding: dict[str, Any] | None,
) -> dict[str, Any]:
    """Sell the entry quantity into the bids at the fill moment: net in bps of the entry
    notional after fees on both notionals and funding paid as qty x mark x rate. A broken,
    stale or thin book is `exit_unfilled`; never a fill at the trade price."""
    if book["status"] != "ok":
        return {"status": "exit_unfilled", "why": book["status"]}
    sold = cost.vwap(book["bids"], base=entry["qty"])
    if sold is None:
        return {"status": "exit_unfilled", "why": "exit_depth_short"}
    if funding is None or funding.get("status") != "ok":
        return {"status": "funding_missing"}
    paid = cost.funding_paid(funding["settlements"], entry["qty"], to_ms(entry["at"]), fill_ms)
    if paid is None:
        return {"status": "funding_missing"}
    entry_notional = entry["notional"]
    exit_notional = entry["qty"] * sold[0]
    fees = cost.FEE_BPS / 1e4 * (entry_notional + exit_notional)
    return {
        "status": "ok",
        "hold_s": fill_ms / 1000 - entry["at"],
        "gross_bps": (exit_notional / entry_notional - 1) * 1e4,
        "net_bps": (exit_notional - entry_notional - fees - paid) / entry_notional * 1e4,
    }


# ---------------------------------------------------------------- statistics


def _ci(pairs: Sequence[tuple[dict[str, Any], float]], seed: int, label: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for scheme, field in (("instrument", "symbol"), ("utc_day", "day")):
        boot = cluster_bootstrap_mean(
            tuple(ClusterObservation(str(r[field]), v) for r, v in pairs),
            iterations=BOOTSTRAP_ITERATIONS,
            seed=derived_seed(seed, f"{label}:{scheme}"),
        )
        out[f"ci95_by_{scheme}"] = [boot.estimate.lower_bound, boot.estimate.upper_bound]
    return out


def _describe(values: Sequence[float]) -> dict[str, Any]:
    return {"mean_bps": fmean(values) if values else None, **quantiles(values)}


def half_report(rows: Sequence[dict[str, Any]], variant: str) -> dict[str, Any]:
    """Counts, each exit's own exclusions by reason, and the common set: the firings
    whose entry and all five exits resolve."""
    entered = [r for r in rows if r["entry"]["status"] == "ok"]
    common = [r for r in entered if all(r[variant][x]["status"] == "ok" for x in EXITS)]
    report: dict[str, Any] = {
        "firings": len(rows),
        "entry_status": dict(Counter(r["entry"]["status"] for r in rows)),
        "entered": len(entered),
        "common": len(common),
        "common_share": len(common) / len(entered) if entered else 0.0,
        "instruments": len({r["symbol"] for r in common}),
        "days": len({r["day"] for r in common}),
        "exits": {},
    }
    for x in EXITS:
        cells = [r[variant][x] for r in entered]
        report["exits"][x] = {
            "own_status": dict(
                Counter(c["status"] + (f":{c['why']}" if "why" in c else "") for c in cells)
            ),
            "decided_share_on_common": (
                sum(r[variant][x]["reason"] == "decided" for r in common) / len(common)
                if common
                else None
            ),
            "net_on_common": _describe([r[variant][x]["net_bps"] for r in common]),
            "hold_s_on_common": quantiles([r[variant][x]["hold_s"] for r in common]),
        }
    return {"report": report, "common": common}


def summarize(rows: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    choice_rows = [r for r in rows if r["bar_start"] < SPLIT.timestamp()]
    test_rows = [r for r in rows if r["bar_start"] >= SPLIT.timestamp()]
    out: dict[str, Any] = {"firings": len(rows)}
    for variant in ("delayed", "zero_delay"):
        out[variant] = {
            "choice_half": half_report(choice_rows, variant)["report"],
            "test_half": half_report(test_rows, variant)["report"],
        }
    choice = half_report(choice_rows, "delayed")
    if choice["report"]["common_share"] < MIN_COMMON_SHARE or len(choice["common"]) < MIN_FIRINGS:
        out["decision"] = "insufficient_data_choice_half"
        return out
    means = {x: fmean(r["delayed"][x]["net_bps"] for r in choice["common"]) for x in EXITS}
    chosen = max(EXITS, key=lambda x: means[x])  # EXITS order breaks an exact tie: H first
    out["chosen"] = chosen
    test = half_report(test_rows, "delayed")
    common = test["common"]
    if (
        test["report"]["common_share"] < MIN_COMMON_SHARE
        or len(common) < MIN_FIRINGS
        or test["report"]["instruments"] < MIN_INSTRUMENTS
        or test["report"]["days"] < MIN_DAYS
    ):
        out["decision"] = "insufficient_data_test_half"
        return out
    net = [(r, r["delayed"][chosen]["net_bps"]) for r in common]
    diff = [(r, r["delayed"][chosen]["net_bps"] - r["delayed"]["H"]["net_bps"]) for r in common]
    by_symbol: dict[str, float] = defaultdict(float)
    for r, v in net:
        by_symbol[r["symbol"]] += v
    total = sum(by_symbol.values())
    verdict = {
        "n": len(common),
        "chosen_mean_net_bps": fmean(v for _, v in net),
        "chosen_median_net_bps": median(v for _, v in net),
        **{f"chosen_{k}": v for k, v in _ci(net, seed, "test:chosen").items()},
        "h_mean_net_bps": fmean(r["delayed"]["H"]["net_bps"] for r in common),
        "paired_diff_vs_h_mean_bps": fmean(v for _, v in diff),
        **{f"paired_diff_{k}": v for k, v in _ci(diff, seed, "test:diff").items()},
        "top5_instrument_share_of_net": (
            sum(sorted(by_symbol.values(), reverse=True)[:5]) / total if total else None
        ),
    }
    out["test_verdict"] = verdict
    lower = [verdict["chosen_ci95_by_instrument"][0], verdict["chosen_ci95_by_utc_day"][0]]
    out["decision"] = (
        "candidate_for_forward_cohort" if all(v > 0 for v in lower) else "no_exit_put_forward"
    )
    return out


# ---------------------------------------------------------------- read


def books_for(
    symbol: str,
    wanted: Sequence[int],
    books_record: dict[str, Any],
    books_dir: Path,
) -> dict[int, dict[str, Any]]:
    """The book at each moment, from the instrument's verified day files."""
    by_day: dict[str, list[int]] = defaultdict(list)
    for ms in wanted:
        by_day[cost.day_of(ms)].append(ms)
    out: dict[int, dict[str, Any]] = {}
    for day, moments in sorted(by_day.items()):
        name = f"{day}_{symbol}_ob200.data.zip"
        entry = books_record["files"].get(name, {})
        if entry.get("status") != "ok":
            continue
        path = books_dir / name
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"{name} does not match its recorded sha256")
        out.update(cost.capture_file(path, moments))
    return out


def measure_symbol(
    firings: Sequence[dict[str, Any]],
    trades: decay.Trades,
    books_record: dict[str, Any],
    books_dir: Path,
    funding: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    symbol = firings[0]["symbol"]
    first = books_for(
        symbol, [ms for f in firings for ms in scheduled_moments(f)], books_record, books_dir
    )
    no_book = {"status": "no_book"}
    staged = []
    later: set[int] = set()
    for f in firings:
        entry = entry_of(f, trades, first.get(scheduled_moments(f)[0], no_book))
        fills: dict[str, Any] = {}
        if entry["status"] == "ok":
            decided = decisions(f, trades, entry["price"])
            fills = {
                "delayed": fill_moments(f, decided, EXIT_DELAY_S),
                "zero_delay": fill_moments(f, decided, 0.0),
            }
            later |= {ms for v in fills.values() for ms, _ in v.values()} - set(first)
        staged.append((f, entry, fills))
    second = books_for(symbol, sorted(later), books_record, books_dir) if later else {}
    books = {**first, **second}
    rows = []
    for f, entry, fills in staged:
        row: dict[str, Any] = {
            "symbol": symbol,
            "bar_start": f["bar_start"],
            "day": datetime.fromtimestamp(f["bar_start"], UTC).date().isoformat(),
            "entry": {"status": entry["status"]},
        }
        for variant, moments in fills.items():
            row[variant] = {}
            for x, (ms, reason) in moments.items():
                cell = settle(entry, books.get(ms, no_book), ms, funding)
                row[variant][x] = {**cell, "reason": reason}
        rows.append(row)
    return rows


def read(
    stage_dir: Path,
    firings_path: Path,
    decay_result: Path,
    tapes_record: Path,
    tapes_dir: Path,
    books_record_path: Path,
    funding_path: Path,
    books_dir: Path,
    revision: str,
) -> dict[str, Any]:
    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: read once")
    firings = cost.load_firings(firings_path)
    decay_payload, decay_sha = load_verified(decay_result)
    if decay_sha != cost.DECAY_RESULT_SHA256:
        raise SystemExit("the decay result is not the published one; refusing")
    tapes, tapes_sha = load_verified(tapes_record)
    if tapes_sha != decay_payload["tapes_sha256"]:
        raise SystemExit("the tape record is not the one the decay result read; refusing")
    books_record, books_sha = load_verified(books_record_path)
    funding_record, funding_sha = load_verified(funding_path)
    if books_sha != BOOKS_SHA256 or funding_sha != FUNDING_SHA256:
        raise SystemExit("the books or the funding are not the cost study's; refusing")
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in firings:
        by_symbol[f["symbol"]].append(f)
    rows: list[dict[str, Any]] = []
    for symbol, own in sorted(by_symbol.items()):
        names = [f"{s}{d}.csv.gz" for s, d in decay.tape_days(own)]
        missing = {n for n in names if tapes["files"].get(n, {}).get("status") != "ok"}
        loaded: list[decay.Trade] = []
        for name in names:
            if name in missing:
                continue
            path = tapes_dir / name
            if sha256_file(path) != tapes["files"][name]["sha256"]:
                raise ValueError(f"{name} does not match its recorded sha256")
            loaded += decay.read_trades(path, [decay.firing_window(f) for f in own])
        trades = decay.Trades(sorted(loaded, key=lambda t: t[0]))
        with_tape = [
            f for f in own if not {f"{s}{d}.csv.gz" for s, d in decay.tape_days([f])} & missing
        ]
        for f in own:
            if f not in with_tape:
                day = datetime.fromtimestamp(f["bar_start"], UTC).date().isoformat()
                rows.append(
                    {
                        "symbol": symbol,
                        "bar_start": f["bar_start"],
                        "day": day,
                        "entry": {"status": "no_tape"},
                    }
                )
        if with_tape:
            funding = funding_record["instruments"].get(symbol)
            rows += measure_symbol(with_tape, trades, books_record, books_dir, funding)
    result = {
        "exit_version": EXIT_VERSION,
        "contract_sha256": contract_sha256(),
        "reader_code_revision": revision,
        "inputs_sha256": {
            "firings": cost.DECAY_FIRINGS_SHA256,
            "decay_result": decay_sha,
            "tapes": tapes_sha,
            "books": books_sha,
            "funding": funding_sha,
        },
        **summarize(rows, BOOTSTRAP_SEED),
        "peak_rss_bytes": decay.peak_rss_bytes(),
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--firings", type=Path, required=True, help="decay-firings.json")
    parser.add_argument("--decay-result", type=Path, required=True, help="decay-result.json")
    parser.add_argument("--tapes-record", type=Path, required=True, help="decay-tapes.json")
    parser.add_argument("--tapes-dir", type=Path, required=True)
    parser.add_argument("--books-record", type=Path, required=True, help="cost-books.json")
    parser.add_argument("--funding", type=Path, required=True, help="cost-funding.json")
    parser.add_argument("--books-dir", type=Path, required=True)
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args(argv)
    from .mexc_early_trigger_hyp029 import verified_revision

    revision = verified_revision(args.code_revision)
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    result = read(
        args.stage_dir,
        args.firings,
        args.decay_result,
        args.tapes_record,
        args.tapes_dir,
        args.books_record,
        args.funding,
        args.books_dir,
        revision,
    )
    sys.stdout.write(
        json.dumps({"decision": result["decision"], "chosen": result.get("chosen")}) + "\n"
    )


if __name__ == "__main__":
    main()
