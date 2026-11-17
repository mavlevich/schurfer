"""I25 stage 1 verdicts: M1 to M6 and the stage 1 stop codes, from receipts only.

Contract: docs/research/new-perp-stage1-request-list-v1.md and the protocol
(docs/research/new-perp-short-lab-protocol-v1.md, sections 2 to 4 and 8). This module
reads the receipts and ``phase0.json`` of a finished or stopped run and never makes a
request. It holds no price: the reduced facts it reads are timestamps, existence,
sizes, counts and a few catalog fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import new_perp_stage1 as s1
from .bybit_carry_feasibility import publish_once
from .catalyst_feasibility import asset

REPORT_VERSION = "new_perp_stage1_verdict_v1"
T = s1.THRESHOLDS

# Exclusion order of a launch (one reason each). Their counts sum to the launches, so the
# stage 1 `n_primary` is the protocol's formula with the two data-availability reasons
# (`t0_unavailable`, `entry_unavailable`) made explicit.
REASONS = (
    "t0_unavailable",
    "t0_ambiguous",
    "identity_excluded",
    "window_censored",
    "lookback_censored",
    "funding_uncovered",
    "entry_unavailable",
    "primary",
)


@dataclass
class RunData:
    """The final results of a run, by class and symbol."""

    phase0: dict[str, Any]
    listings: dict[str, dict[str, Any]] = field(default_factory=dict)
    heads: dict[str, dict[str, Any]] = field(default_factory=dict)
    firsts: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_bar: dict[str, dict[str, Any]] = field(default_factory=dict)
    mark_hourly: dict[str, dict[str, Any]] = field(default_factory=dict)
    trade_hourly: dict[str, dict[str, Any]] = field(default_factory=dict)
    funding: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    book: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    enabled: set[str] = field(default_factory=lambda: set(s1.enabled_classes()))
    preflight: dict[str, Any] | None = None
    requests: dict[str, Any] = field(default_factory=dict)


def load_run(root: Path) -> RunData:
    """Rebuild the results of a run from its receipts. No request, no body."""
    receipts = [json.loads(p.read_text()) for p in sorted((root / "receipts").glob("*.json"))]
    final = s1.final_from_receipts(receipts)
    by_cls: dict[str, dict[str, dict[str, Any]]] = {}
    for r in final.values():
        if r["symbol"]:
            by_cls.setdefault(r["cls"], {})[r["symbol"]] = r
    funding: dict[str, list[dict[str, Any]]] = {}
    pages = (r for r in final.values() if r["cls"] == "D6")
    for r in sorted(pages, key=lambda r: (r["symbol"], int(r["meta"]["page"]))):
        funding.setdefault(r["symbol"], []).append(r)
    book: dict[str, dict[str, dict[str, Any]]] = {}
    for r in final.values():
        if r["cls"] == "F":
            book.setdefault(r["symbol"], {}).setdefault(r["meta"]["label"], {})[
                r["meta"]["file"]
            ] = r
    preflight_path = root / "preflight.json"
    contract = json.loads((root / "contract.json").read_text())
    attempts = [json.loads(p.read_text()) for p in sorted((root / "attempts").glob("*.json"))]
    count, used, by_n, by_b = s1.ledger_totals(attempts, receipts)
    return RunData(
        phase0=json.loads((root / "phase0.json").read_text()),
        listings=by_cls.get("D1", {}),
        heads=by_cls.get("D2h", {}),
        firsts=by_cls.get("D2", {}),
        entry_bar=by_cls.get("D3", {}),
        mark_hourly=by_cls.get("D4", {}),
        trade_hourly=by_cls.get("D5", {}),
        funding=funding,
        book=book,
        enabled=set(s1.enabled_classes(contract)),
        preflight=json.loads(preflight_path.read_text()) if preflight_path.exists() else None,
        requests={"attempts": count, "bytes": used, "by_class": by_n, "bytes_by_class": by_b},
    )


def _share(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def _at_most(share: float | None, limit: float) -> bool:
    return share is not None and share <= limit


def _at_least(share: float | None, limit: float) -> bool:
    return share is not None and share >= limit


def _floor_hour(ms: int) -> int:
    return ms - ms % s1.HOUR_MS


def static_order(row: dict[str, Any]) -> str:
    """M6 at coverage level, from catalog fields only: `feasible`, `infeasible`, `unknown`.

    The price-dependent part (quantity step and minimum quantity against USD 50 at the
    entry price) needs a price and is deferred to stage 3 and 4.
    """
    try:
        notional = Decimal(str(row["min_notional"]))
        leverage = Decimal(str(row["max_leverage"]))
    except (InvalidOperation, TypeError):
        return "unknown"
    if row["qty_step"] is None or row["min_order_qty"] is None:
        return "unknown"
    if notional > T["m6_order_notional_usd"] or leverage < T["m6_min_leverage"]:
        return "infeasible"
    return "feasible"


def bar_state(
    result: dict[str, Any] | None, entry_ms: int, exit_ms: int, delivery_ms: int | None
) -> str:
    """Hourly series over the path of V1. `covered`, `confirmed_event`, or the gap reason.

    A series that ends before X is a data gap unless the catalog confirms an end dated at
    or before the first missing minute (the end-of-archive rule, protocol 5.3 items 3, 4).
    """
    if result is None:
        return "not_run"
    if result["outcome"] != "ok":
        return "unavailable"
    facts = result["facts"]
    if facts["n"] == 0:
        return "empty"
    if facts["oldest_ms"] > _floor_hour(entry_ms):
        return "starts_late"
    first_missing = facts["newest_ms"] + s1.HOUR_MS
    if facts["newest_ms"] >= _floor_hour(exit_ms - 1):
        return "covered"
    if delivery_ms is not None and delivery_ms <= first_missing:
        return "confirmed_event"
    return "ends_early"


def path_days(entry_ms: int, exit_ms: int) -> list[str]:
    first, last = s1.day_of(entry_ms), s1.day_of(exit_ms - 1)
    return [s1.add_days(first, i) for i in range(s1.day_diff(last, first) + 1)]


def evaluate(data: RunData) -> dict[str, Any]:
    """The stage 1 report: M1 to M6, launch statuses, n_primary and the stop code."""
    launches = s1.in_window_launches(data.phase0)
    contradictions = s1.catalog_contradictions(data.phase0)
    candidates = {
        c["symbol"]: c for c in s1.entry_candidates(data.phase0, data.listings, data.firsts)
    }
    rows: list[dict[str, Any]] = []
    for row in launches:
        rows.append(_launch(data, row, contradictions, candidates))
    n_launches = len(launches)
    not_assessed: list[str] = []

    # M1
    m1_counts = _count(rows, "m1")
    ambiguous = _share(m1_counts.get("premarket_or_ambiguous", 0), n_launches)
    readable = _share(m1_counts.get("aligned", 0) + m1_counts.get("delayed_open", 0), n_launches)
    m1: dict[str, Any] = {
        "classes": m1_counts,
        "ambiguous_share": ambiguous,
        "readable_share": readable,
        "pass": _at_most(ambiguous, T["m1_ambiguous_max"])
        and _at_least(readable, T["m1_readable_min"]),
    }
    unrebuildable = sum(1 for r in rows if r["m1"] in ("no_archive", "t0_unreadable"))
    lifecycle_share = _share(unrebuildable, n_launches)
    lifecycle_ok = _at_most(lifecycle_share, T["lifecycle_unrebuildable_max"])

    # M2
    m2 = _m2(data, rows, candidates)

    # M3
    m3 = _m3(rows)

    # statuses and n_primary (exclusive reasons)
    _assign_reasons(data, rows, candidates, not_assessed)
    reasons = _count(rows, "reason")
    n_primary = reasons.get("primary", 0)
    weeks = len({r["week"] for r in rows if r["reason"] == "primary"})

    # M4, M5, M6 are over the episodes still eligible at each step
    eligible = [r for r in rows if r["symbol"] in candidates]
    m4 = _m4(eligible, not_assessed)
    after_funding = [r for r in eligible if r["funding"] != "uncovered"]
    m5 = _m5(data, after_funding, candidates, not_assessed)
    m6 = _m6(after_funding)

    stops = _stop(m1, lifecycle_ok, m2, m3, m4, m5, m6, n_primary, weeks)
    outcome = stops[0] if stops else ("stage1_incomplete" if not_assessed else "stage1_pass")
    return {
        "version": REPORT_VERSION,
        "outcome": outcome,
        "stop_codes": stops,
        "not_assessed": sorted(set(not_assessed)),
        "launches": n_launches,
        "n_primary": n_primary,
        "iso_weeks": weeks,
        "reasons": {r: reasons.get(r, 0) for r in REASONS},
        "m1": {**m1, "lifecycle_unrebuildable_share": lifecycle_share},
        "m2": m2,
        "m3": m3,
        "m4": m4,
        "m5": m5,
        "m6": m6,
        "book": _book(data, candidates),
        "spot": _spot(data, rows),
        "requests": data.requests,
        "classes_in_go": sorted(data.enabled),
        "book_source_confirmed": False,
    }


def _launch(
    data: RunData,
    row: dict[str, Any],
    contradictions: dict[str, str],
    candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    symbol = row["symbol"]
    listing = data.listings.get(symbol)
    t0, m1 = s1.t0_of(row, listing, data.firsts.get(symbol))
    listing_ok = listing is not None and listing.get("outcome") == "ok"
    facts = listing["facts"] if listing is not None and listing_ok else None
    near = None
    if facts is not None:
        days = set(facts["days_in_window"])
        if facts["first_day"] is not None:
            days.add(facts["first_day"])
        launch_day = s1.day_of(row["launch_ms"])
        near = any(abs(s1.day_diff(d, launch_day)) <= T["m2_near_launch_days"] for d in days)
    status = contradictions.get(symbol)
    if status is None and facts is not None and s1.ticker_reuse(facts, row["launch_ms"]):
        status = "ticker_reuse_suspected"
    unverifiable = (not listing_ok) or row["base_coin"] is None
    if row["base_coin"] is not None and asset(row["base_coin"]) != row["base"]:
        unverifiable = True
    cuts = s1.v1_cuts(t0) if t0 is not None else None
    return {
        "symbol": symbol,
        "row": row,
        "week": s1.iso_week(row["launch_ms"]),
        "listing": "ok" if listing_ok else ("absent" if listing else "not_run"),
        "near_file": near,
        "m1": m1,
        "t0_ms": t0,
        "identity": status,
        "unverifiable": unverifiable,
        "cuts": cuts,
        "eligible": symbol in candidates,
        "funding": None,
        "reason": None,
    }


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


def _m2(
    data: RunData, rows: list[dict[str, Any]], candidates: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    n = len(rows)
    no_near = sum(1 for r in rows if not r["near_file"])
    window = list(data.phase0["archive"]["candidates"])
    in_window = unavailable = 0
    for symbol in window:
        result = data.listings.get(symbol)
        if result is None or result.get("outcome") != "ok":
            unavailable += 1
            continue
        first = result["facts"]["first_day"]
        if first is not None and s1.WINDOW_START_DAY <= first < s1.CUT_DAY:
            in_window += 1
    # Unlisted candidates count in the worst case: they could be archive-only symbols.
    archive_only = in_window + unavailable
    union = n + archive_only
    path_bad = 0
    for symbol, c in candidates.items():
        listing = data.listings[symbol]["facts"]
        have = set(listing["days_in_window"])
        days = path_days(c["entry_ms"], c["exit_ms"])
        missing = sum(1 for d in days if d not in have)
        if missing > T["m2_path_days_missing_max"] * len(days):
            path_bad += 1
    shares = {
        "no_file_near_launch": _share(no_near, n),
        "archive_only": _share(archive_only, union),
        "path_days_missing": _share(path_bad, len(candidates)),
    }
    limits = {
        "no_file_near_launch": T["m2_no_file_near_launch_max"],
        "archive_only": T["m2_archive_only_max"],
        "path_days_missing": T["m2_path_days_missing_max"],
    }
    ok = all(
        _at_most(shares[k], limits[k]) for k in shares if k != "path_days_missing" or candidates
    )
    return {
        "shares": shares,
        "counts": {
            "catalog_launches": n,
            "no_file_near_launch": no_near,
            "archive_only_in_window": in_window,
            "archive_only_unlisted": unavailable,
            "path_days_missing_launches": path_bad,
            "candidates_listed": len(window),
        },
        "pass": ok,
    }


def _m3(rows: list[dict[str, Any]]) -> dict[str, Any]:
    survive = [r for r in rows if r["m1"] in ("aligned", "delayed_open") and r["near_file"]]
    statuses = {"relist": 0, "multiplier_ambiguous": 0, "ticker_reuse_suspected": 0}
    for r in survive:
        if r["identity"] in statuses:
            statuses[r["identity"]] += 1
    contradicting = sum(statuses.values())
    unverifiable = sum(1 for r in survive if r["unverifiable"] and r["identity"] is None)
    remain = len(survive) - contradicting
    n = len(survive)
    shares = {
        "identity_unverifiable": _share(unverifiable, n),
        "contradicting": _share(contradicting, n),
    }
    ok = (
        _at_most(shares["identity_unverifiable"], T["m3_unverifiable_max"])
        and _at_most(shares["contradicting"], T["m3_contradicting_max"])
        and remain >= T["min_episodes"]
    )
    return {
        "survivors": n,
        "statuses": statuses,
        "identity_unverifiable": unverifiable,
        "change_known_at_entry": "not_observable_at_stage_1",
        "episodes_remaining": remain,
        "shares": shares,
        "pass": ok,
    }


def _assign_reasons(
    data: RunData,
    rows: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    not_assessed: list[str],
) -> None:
    for r in rows:
        symbol = r["symbol"]
        if r["m1"] in ("no_archive", "t0_unreadable"):
            r["reason"] = "t0_unavailable"
        elif r["m1"] == "premarket_or_ambiguous":
            r["reason"] = "t0_ambiguous"
        elif r["identity"] is not None:
            r["reason"] = "identity_excluded"
        elif r["cuts"]["window_censored"]:
            r["reason"] = "window_censored"
        elif r["cuts"]["lookback_censored"]:
            r["reason"] = "lookback_censored"
        elif symbol in candidates:
            state = _funding_state(data, candidates[symbol])
            r["funding"] = (
                "pending"
                if not state["decided"]
                else ("covered" if state["covered"] else "uncovered")
            )
            if r["funding"] == "uncovered":
                r["reason"] = "funding_uncovered"
            elif _entry_bar(data, candidates[symbol]) in ("absent", "unavailable"):
                r["reason"] = "entry_unavailable"
            else:
                r["reason"] = "primary"
        else:
            raise ValueError("a launch reached no status")
    if any(r["funding"] == "pending" for r in rows):
        not_assessed.append("m4_funding_not_run")
    if any(_entry_bar(data, candidates[r["symbol"]]) == "not_run" for r in rows if r["eligible"]):
        not_assessed.append("m5_entry_bar_not_run")


def _funding_state(data: RunData, candidate: dict[str, Any]) -> dict[str, Any]:
    start, end = s1.funding_span(candidate["t0_ms"])
    return s1.funding_state(data.funding.get(candidate["symbol"], []), start, end)


def _entry_bar(data: RunData, candidate: dict[str, Any]) -> str:
    result = data.entry_bar.get(candidate["symbol"])
    if result is None:
        return "not_run"
    if result["outcome"] != "ok":
        return "unavailable"
    facts = result["facts"]
    return (
        "present" if facts["n"] == 1 and facts["oldest_ms"] == candidate["entry_ms"] else "absent"
    )


def _m4(eligible: list[dict[str, Any]], not_assessed: list[str]) -> dict[str, Any]:
    uncovered = [r for r in eligible if r["funding"] == "uncovered"]
    n = len(eligible)
    by_month: dict[str, int] = {}
    by_status = {"delisted": 0, "listed": 0}
    for r in uncovered:
        month = s1.day_of(r["row"]["launch_ms"])[:7]
        by_month[month] = by_month.get(month, 0) + 1
        by_status["listed" if r["row"]["status"] == "Trading" else "delisted"] += 1
    share = _share(len(uncovered), n)
    assessed = "m4_funding_not_run" not in not_assessed
    return {
        "eligible": n,
        "funding_uncovered": len(uncovered),
        "share": share,
        "by_launch_month": dict(sorted(by_month.items())),
        "by_status": by_status,
        "assessed": assessed,
        "pass": assessed and _at_most(share, T["m4_funding_uncovered_max"]),
    }


def _m5(
    data: RunData,
    episodes: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    not_assessed: list[str],
) -> dict[str, Any]:
    states: dict[str, dict[str, int]] = {"entry": {}, "mark": {}, "trade": {}}
    ok = 0
    for r in episodes:
        c = candidates[r["symbol"]]
        delivery = r["row"]["delivery_ms"]
        entry = _entry_bar(data, c)
        mark = bar_state(data.mark_hourly.get(c["symbol"]), c["entry_ms"], c["exit_ms"], delivery)
        trade = bar_state(data.trade_hourly.get(c["symbol"]), c["entry_ms"], c["exit_ms"], delivery)
        for key, value in (("entry", entry), ("mark", mark), ("trade", trade)):
            states[key][value] = states[key].get(value, 0) + 1
        covered = ("covered", "confirmed_event")
        if entry == "present" and mark in covered and trade in covered:
            ok += 1
    n = len(episodes)
    share = _share(ok, n)
    assessed = n > 0 and not any("not_run" in per for per in states.values())
    if not assessed and n:
        not_assessed.append("m5_bars_not_run")
    return {
        "episodes": n,
        "with_bars": ok,
        "share": share,
        "states": {k: dict(sorted(v.items())) for k, v in states.items()},
        "level": "entry minute bar (trade) and hourly trade and mark series over the V1 path",
        "minute_level_path": "not_assessed_at_stage_1",
        "assessed": assessed,
        "pass": assessed and _at_least(share, T["m5_bars_min"]),
    }


def _m6(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"feasible": 0, "infeasible": 0, "unknown": 0}
    for r in episodes:
        counts[static_order(r["row"])] += 1
    n = len(episodes)
    share = _share(counts["feasible"], n)
    return {
        "episodes": n,
        "counts": counts,
        "share": share,
        "price_dependent_part": "deferred_to_stage_3_and_4",
        "pass": _at_least(share, T["m6_feasible_min"]),
    }


def _stop(
    m1: dict[str, Any],
    lifecycle_ok: bool,
    m2: dict[str, Any],
    m3: dict[str, Any],
    m4: dict[str, Any],
    m5: dict[str, Any],
    m6: dict[str, Any],
    n_primary: int,
    weeks: int,
) -> list[str]:
    """Stop codes in the order of protocol section 8, rows 1 to 5. Only assessed checks count."""
    out: list[str] = []
    if not m1["pass"] or not lifecycle_ok:
        out.append("not_reconstructible")
    m4_fail = m4["assessed"] and not m4["pass"]
    m5_fail = m5["assessed"] and not m5["pass"]
    if not m2["pass"] or m4_fail or m5_fail:
        out.append("incomplete_coverage")
    if not m3["pass"]:
        out.append("identity_unresolved")
    if n_primary < T["min_episodes"] or weeks < T["min_weeks"]:
        out.append("insufficient_events")
    if not m6["pass"]:
        out.append("capital_infeasible")
    return out


def _book(data: RunData, candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if "F" not in data.enabled:
        return {"in_go": False}
    counts = {
        "entry": {"ob500": 0, "ob200": 0, "none": 0},
        "exit": {"ob500": 0, "ob200": 0, "none": 0},
    }
    for symbol in candidates:
        for label in ("entry", "exit"):
            tried = data.book.get(symbol, {}).get(label, {})
            if tried.get("ob500", {}).get("outcome") == "ok":
                counts[label]["ob500"] += 1
            elif tried.get("ob200", {}).get("outcome") == "ok":
                counts[label]["ob200"] += 1
            else:
                counts[label]["none"] += 1
    return {
        "in_go": True,
        "candidates": len(candidates),
        "counts": counts,
        "gates_nothing": True,
        "counts_for_scenario_l": "zero until the owner confirms the source (protocol 11, item 7)",
    }


def _spot(data: RunData, rows: list[dict[str, Any]]) -> dict[str, Any]:
    spot = data.phase0.get("spot")
    if not spot or not spot["ok"]:
        return {"available": False}
    bases = {r["base"] for r in spot["rows"]}
    present = sum(1 for r in rows if r["row"]["base"] in bases)
    return {
        "available": True,
        "launches_with_spot_symbol": present,
        "spot_rows_with_launch_time": spot["with_launch_time"],
        "gates_nothing": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = evaluate(load_run(args.root))
    publish_once(args.out, report)
    sys.stdout.write(
        json.dumps({"outcome": report["outcome"], "stops": report["stop_codes"]}) + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
