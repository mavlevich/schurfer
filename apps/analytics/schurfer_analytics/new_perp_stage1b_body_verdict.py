"""I25 Stage 1b body-phase verdicts: M1 to M6 and the stop codes, from receipts only.

Contract: docs/research/new-perp-stage1b-body-v1.md. This module reads the sealed metadata
facts and the receipts of a body run and never makes a request. It holds no price, rate or
volume. Its report is aggregate counts only: no symbol, no slot, no per-launch date, no
per-launch result. `stage1b_pass_partial` is a coverage statement, never an economic result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from . import new_perp_stage1 as s
from . import new_perp_stage1_verdict as discovery
from . import new_perp_stage1b_body as body
from .new_perp_window import HOLDOUT

if TYPE_CHECKING:
    from pathlib import Path

REPORT_VERSION: Final = "new_perp_stage1b_body_verdict_v1"
T: Final = s.THRESHOLDS
STATEMENT: Final = (
    "coverage level only; not an economic result; no price, rate, volume or return was "
    "stored or published; no holdout fact may tune a Stage 3 parameter"
)

REASONS: Final = body.REASONS


@dataclass
class BodyData:
    report: dict[str, Any]
    rows: dict[str, dict[str, Any]]
    firsts: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_bar: dict[str, dict[str, Any]] = field(default_factory=dict)
    mark_hourly: dict[str, dict[str, Any]] = field(default_factory=dict)
    trade_hourly: dict[str, dict[str, Any]] = field(default_factory=dict)
    funding: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stop: dict[str, Any] | None = None
    api_errors: dict[str, int] = field(default_factory=dict)
    requests: dict[str, Any] = field(default_factory=dict)


def from_receipts(
    receipts: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    sealed: body.Sealed,
    stop: dict[str, Any] | None,
) -> BodyData:
    final = s.final_from_receipts(receipts, attempts)
    by_cls: dict[str, dict[str, dict[str, Any]]] = {}
    funding: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(final.values(), key=lambda r: (r["cls"], str(r["meta"].get("page", "")))):
        slot = r["meta"].get("slot")
        if slot is None:
            continue
        if r["cls"] == "D6":
            funding.setdefault(slot, []).append(r)
        else:
            by_cls.setdefault(r["cls"], {})[slot] = r
    for pages in funding.values():
        pages.sort(key=lambda r: int(r["meta"]["page"]))
    specs = body.class_specs()
    count, used, by_n, by_b = s.ledger_totals(attempts, receipts, None, specs)
    codes: dict[str, int] = {}
    for r in receipts:
        if "ret_code" in r:
            codes[str(r["ret_code"])] = codes.get(str(r["ret_code"]), 0) + 1
    return BodyData(
        report=sealed.report,
        rows=sealed.rows,
        firsts=by_cls.get("D2", {}),
        entry_bar=by_cls.get("D3", {}),
        mark_hourly=by_cls.get("D4", {}),
        trade_hourly=by_cls.get("D5", {}),
        funding=funding,
        stop=stop,
        api_errors=dict(sorted(codes.items())),
        requests={"attempts": count, "bytes": used, "by_class": by_n, "bytes_by_class": by_b},
    )


def from_fetcher(fetcher: s.Fetcher, sealed: body.Sealed) -> BodyData:
    stop_path = fetcher.root / "stop.json"
    stop = json.loads(stop_path.read_text()) if stop_path.exists() else None
    return from_receipts(fetcher.receipts, fetcher.attempts, sealed, stop)


def load_run(root: Path, sealed: body.Sealed) -> BodyData:
    """Rebuild the results of a body run from its receipts. No request, no body."""
    receipts = [json.loads(p.read_text()) for p in sorted((root / "receipts").glob("*.json"))]
    attempts = [json.loads(p.read_text()) for p in sorted((root / "attempts").glob("*.json"))]
    stop_path = root / "stop.json"
    stop = json.loads(stop_path.read_text()) if stop_path.exists() else None
    return from_receipts(receipts, attempts, sealed, stop)


def _share(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def _at_most(share: float | None, limit: float) -> bool:
    return share is not None and share <= limit


def _at_least(share: float | None, limit: float) -> bool:
    return share is not None and share >= limit


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r[key])] = out.get(str(r[key]), 0) + 1
    return dict(sorted(out.items()))


def _quarter(ms: int) -> str:
    stamp = datetime.fromtimestamp(ms / 1000, UTC)
    return f"{stamp.year}-Q{(stamp.month - 1) // 3 + 1}"


def static_order(row: dict[str, Any]) -> str:
    """M6 at coverage level, from catalog fields only: `feasible`, `infeasible`, `unknown`."""
    try:
        notional = Decimal(str(row["min_notional"]))
        leverage = Decimal(str(row["max_leverage"]))
    except (InvalidOperation, TypeError, KeyError):
        return "unknown"
    if row.get("qty_step") is None or row.get("min_order_qty") is None:
        return "unknown"
    if notional > T["m6_order_notional_usd"] or leverage < T["m6_min_leverage"]:
        return "infeasible"
    return "feasible"


def _launch(data: BodyData, slot: str, queued: set[str]) -> dict[str, Any]:
    row = data.rows[slot]
    t0, m1 = body.t0_state(row, data.firsts.get(slot), slot in queued)
    status = row.get("catalog_identity", "not_contradicted")
    identity = None if status == "not_contradicted" else status
    if identity is None and row.get("ticker_reuse_suspected") is True:
        identity = "ticker_reuse_suspected"
    unverifiable = row.get("filename_identity_unverified", True) is True or (
        row.get("listing_state") != "ok"
    )
    return {
        "slot": slot,
        "row": row,
        "m1": m1,
        "t0_ns": t0,
        "near_file": row.get("first_file_within_1d_of_launch") is True,
        "identity": identity,
        "unverifiable": unverifiable,
        "cuts": body.cuts_ns(t0) if t0 is not None else None,
        "week": s.iso_week(row["launch_ms"]) if "launch_ms" in row else "unknown",
        "eligible": False,
        "funding": None,
        "funding_reason": None,
        "reason": None,
    }


def _entry_state(data: BodyData, c: body.Candidate) -> str:
    result = data.entry_bar.get(c.slot)
    if result is None:
        return "not_run"
    if result["outcome"] != "ok":
        return "unavailable"
    facts = result["facts"]
    return "present" if facts["n"] == 1 and facts["oldest_ms"] == c.entry_ms else "absent"


def _funding_state(data: BodyData, c: body.Candidate) -> dict[str, Any]:
    start, end = HOLDOUT.funding_span(c.t0_ms)
    delivery = data.rows[c.slot].get("delivery_ms")
    return s.funding_state(data.funding.get(c.slot, []), start, end, delivery)


def _path_days_bad(row: dict[str, Any], c: body.Candidate) -> bool:
    days = discovery.path_days(c.entry_ms, c.exit_ms)
    have = set(row.get("days_in_window") or [])
    missing = sum(1 for d in days if d not in have)
    return bool(missing > T["m2_path_days_missing_max"] * len(days))


def evaluate(data: BodyData, phase: str) -> dict[str, Any]:
    """The report of Go A (`phase == "a"`) or of the whole body phase (`"final"`).

    After Go A only checks that no later request can change are decided: M1, M2, M3 and an
    upper bound of the 100-episode and 26-week floor. They are definitive, and a stop code
    among them ends the stage: Go B is refused. M4, M5, M6 and `n_primary` need Go B.
    """
    queued = {slot for slot, row in data.rows.items() if body.has_size(row)}
    rows = [_launch(data, slot, queued) for slot in sorted(data.rows)]
    n = len(rows)
    candidates = {c.slot: c for c in body.entry_candidates(data.rows, data.firsts, queued)}
    for r in rows:
        r["eligible"] = r["slot"] in candidates
    not_assessed: list[str] = []

    m1 = _m1(rows, n, not_assessed)
    m2 = _m2(data, rows, candidates, n, not_assessed)
    m3 = _m3(rows, not_assessed)
    weeks_cand = len({r["week"] for r in rows if r["eligible"]})
    if phase == "a":
        n_primary: int | None = None
        weeks: int | None = None
        reasons: dict[str, int] = {}
        m4 = m5 = None
        eligible = [r for r in rows if r["eligible"]]
        m6 = _m6(eligible, provisional=True)
        floor_bound = {"candidates": len(candidates), "iso_weeks": weeks_cand}
        not_assessed += ["m4_funding_go_b", "m5_bars_go_b", "n_primary_go_b"]
    else:
        _assign_reasons(data, rows, candidates, not_assessed)
        counts = _count(rows, "reason")
        reasons = {name: counts.get(name, 0) for name in REASONS}
        n_primary = reasons["primary"]
        weeks = len({r["week"] for r in rows if r["reason"] == "primary"})
        eligible = [r for r in rows if r["eligible"]]
        m4 = _m4(eligible, not_assessed)
        after_funding = [r for r in eligible if r["funding"] != "uncovered"]
        m5 = _m5(data, after_funding, candidates, not_assessed)
        m6 = _m6(after_funding)
        floor_bound = None

    stops = _stops(phase, m1, m2, m3, m4, m5, m6, n_primary, weeks, floor_bound)
    blocking = [x for x in not_assessed if x.startswith(("m1_", "m2_", "m3_"))]
    if stops:
        outcome = stops[0]
    elif data.stop is not None:
        outcome = "stage1b_stopped"  # a stopped run never reaches a pass
    elif phase == "a":
        outcome = "stage1b_incomplete" if blocking else "stage1b_a_complete"
    elif not_assessed:
        outcome = "stage1b_incomplete"
    else:
        outcome = "stage1b_pass_partial"
    report: dict[str, Any] = {
        "version": REPORT_VERSION,
        "phase": phase,
        "statement": STATEMENT,
        "outcome": outcome,
        "stop_codes": stops,
        "not_assessed": sorted(set(not_assessed)),
        "launches": n,
        "n_primary": n_primary,
        "iso_weeks": weeks,
        "reasons": reasons,
        "m1": m1,
        "m2": m2,
        "m3": m3,
        "m4": m4,
        "m5": m5,
        "m6": m6,
        "partial_checks": partial_checks(m4, m5),
        "stop": data.stop,
        "api_errors": data.api_errors,
        "requests": data.requests,
    }
    if phase == "a":
        report["floor_upper_bound"] = floor_bound
        report["go_b_allowed"] = outcome == "stage1b_a_complete"
        report["entry_candidates"] = len(candidates)
    return report


def partial_checks(m4: dict[str, Any] | None, m5: dict[str, Any] | None) -> list[str]:
    out = [
        "m1: the first public trade against launchTime only; a bar at E is not the first "
        "nonzero-volume bar and does not date the regular launch",
        "m2(ii): an observed file series outside the catalog, not a census of launches and "
        "not verified instruments",
        "m3: the current catalog and file lists only; the absence of a contradiction does not "
        "prove historical identity known at E, and change_known_at_entry is not observable",
        "m6: static catalog fields only; no price, quantity rounding, risk limit or depth",
    ]
    if m4 is not None:
        weak = m4["covered_by_interval_snapshot"]["snapshot_shorter_or_unknown"]
        out.append(
            "m4: funding is a coarse check (no gap longer than 8 h), not proof of every "
            f"settlement; {weak} covered launches have a snapshot interval shorter than 8 h "
            "or unknown"
        )
    if m5 is not None:
        out.append("m5: minute-level continuity is deferred to stage 4 (protocol Amendment 1)")
        unproven = sum(per.get("unproven", 0) for per in m5["states"].values())
        if unproven:
            out.append(f"m5: {unproven} hourly series are unproven (a truncated page)")
    return out


def _m1(rows: list[dict[str, Any]], n: int, not_assessed: list[str]) -> dict[str, Any]:
    counts = _count(rows, "m1")
    pending = counts.get("not_run", 0)
    ambiguous = counts.get("premarket_or_ambiguous", 0)
    readable = counts.get("aligned", 0) + counts.get("delayed_open", 0)
    unreadable = sum(counts.get(k, 0) for k in body.UNREADABLE)
    shares = {
        "ambiguous": _share(ambiguous, n),
        "readable": _share(readable, n),
        "lifecycle_unrebuildable": _share(unreadable, n),
    }
    # A failure is known even while launches are pending: pending ones can only help the
    # readable share, never lower the ambiguous or unreadable one.
    fails = (
        n == 0
        or ambiguous / n > T["m1_ambiguous_max"]
        or (readable + pending) / n < T["m1_readable_min"]
        or unreadable / n > T["lifecycle_unrebuildable_max"]
    )
    if pending:
        not_assessed.append("m1_not_run")
    return {
        "classes": counts,
        "shares": shares,
        "pending": pending,
        "fail": fails,
        "pass": not fails and not pending,
    }


def _m2(
    data: BodyData,
    rows: list[dict[str, Any]],
    candidates: dict[str, body.Candidate],
    n: int,
    not_assessed: list[str],
) -> dict[str, Any]:
    report = data.report
    near_missing = sum(
        r["row"].get("first_file_within_1d_of_launch") is False
        and r["row"].get("filename_identity_unverified", True) is False
        for r in rows
    )
    unproven = sum(r["row"].get("filename_identity_unverified", True) is True for r in rows)
    ii = report["m2ii"]
    share_i = _share(near_missing, n)
    share_ii = ii["share"]
    bad = sum(_path_days_bad(data.rows[slot], c) for slot, c in candidates.items())
    share_iii = _share(bad, len(candidates))
    if unproven:
        not_assessed.append("m2_i_unproven_catalog_folders")
    if share_ii is None:
        not_assessed.append("m2_ii_share_unavailable")
    if not candidates:
        not_assessed.append("m2_iii_no_candidates")
    fails = (
        (share_i or 0) > T["m2_no_file_near_launch_max"]
        or (share_ii or 0) > T["m2_archive_only_max"]
        or (share_iii or 0) > T["m2_path_days_missing_max"]
    )
    ok = (
        not unproven
        and _at_most(share_i, T["m2_no_file_near_launch_max"])
        and _at_most(share_ii, T["m2_archive_only_max"])
        and _at_most(share_iii, T["m2_path_days_missing_max"])
    )
    return {
        "shares": {
            "no_file_near_launch": share_i,
            "archive_only": share_ii,
            "path_days": share_iii,
        },
        "counts": {
            "catalog_launches": n,
            "no_file_near_launch": near_missing,
            "i_unproven": unproven,
            "archive_only_in_window": ii["archive_only_in_window"],
            "archive_only_pre_window": ii["archive_only_pre_window"],
            "unverified_directories": ii["unverified_directories"],
            "collision_directories": ii["collision_directories"],
            "path_days_missing_launches": bad,
            "path_days_candidates": len(candidates),
        },
        "metadata_report_agrees": report["m2_i_no_file_near_launch"] == near_missing,
        "archive_only_proven": False,
        "archive_only_scope": "observed pre-cut file-prefix series, first file inside the window",
        "partial": True,
        "fail": fails,
        "pass": ok and not fails,
    }


def _m3(rows: list[dict[str, Any]], not_assessed: list[str]) -> dict[str, Any]:
    survivors = [r for r in rows if r["m1"] in ("aligned", "delayed_open") and r["near_file"]]
    statuses = {"relist": 0, "multiplier_ambiguous": 0, "ticker_reuse_suspected": 0}
    for r in survivors:
        if r["identity"] in statuses:
            statuses[r["identity"]] += 1
    contradicting = sum(statuses.values())
    unverifiable = sum(1 for r in survivors if r["unverifiable"] and r["identity"] is None)
    remain = len(survivors) - contradicting
    shares = {
        "identity_unverifiable": _share(unverifiable, len(survivors)),
        "contradicting": _share(contradicting, len(survivors)),
    }
    pending = any(r["m1"] == "not_run" for r in rows)
    if pending:
        not_assessed.append("m3_m1_not_run")
    ok = (
        _at_most(shares["identity_unverifiable"], T["m3_unverifiable_max"])
        and _at_most(shares["contradicting"], T["m3_contradicting_max"])
        and remain >= T["min_episodes"]
    )
    return {
        "survivors": len(survivors),
        "statuses": statuses,
        "identity_unverifiable": unverifiable,
        "change_known_at_entry": "not_observable",
        "episodes_remaining": remain,
        "shares": shares,
        "assessed": not pending,
        "pass": ok and not pending,
    }


def _assign_reasons(
    data: BodyData,
    rows: list[dict[str, Any]],
    candidates: dict[str, body.Candidate],
    not_assessed: list[str],
) -> None:
    for r in rows:
        slot = r["slot"]
        if r["m1"] in body.UNREADABLE or r["m1"] == "not_run":
            r["reason"] = "t0_unavailable"
        elif r["m1"] == "premarket_or_ambiguous":
            r["reason"] = "t0_ambiguous"
        elif r["identity"] is not None:
            r["reason"] = "identity_excluded"
        elif r["cuts"]["window_censored"]:
            r["reason"] = "window_censored"
        elif r["cuts"]["lookback_censored"]:
            r["reason"] = "lookback_censored"
        elif slot in candidates:
            state = _funding_state(data, candidates[slot])
            r["funding_reason"] = state["reason"]
            r["funding"] = (
                "pending"
                if not state["decided"]
                else ("covered" if state["covered"] else "uncovered")
            )
            if r["funding"] == "uncovered":
                r["reason"] = "funding_uncovered"
            elif _entry_state(data, candidates[slot]) in ("absent", "unavailable"):
                r["reason"] = "entry_unavailable"
            else:
                r["reason"] = "primary"
        else:
            raise ValueError("a launch reached no status")
    if any(r["funding"] == "pending" for r in rows):
        not_assessed.append("m4_funding_not_run")
    if any(_entry_state(data, candidates[r["slot"]]) == "not_run" for r in rows if r["eligible"]):
        not_assessed.append("m5_entry_bar_not_run")


def _m4_status(assessed: bool, share: float | None) -> str:
    if (share or 0) > T["m4_funding_uncovered_max"]:
        return "fail"
    return "partial_pass" if assessed and share is not None else "not_assessed"


def _m4(eligible: list[dict[str, Any]], not_assessed: list[str]) -> dict[str, Any]:
    uncovered = [r for r in eligible if r["funding"] == "uncovered"]
    covered = [r for r in eligible if r["funding"] == "covered"]
    n = len(eligible)
    by_quarter: dict[str, int] = {}
    by_delivery = {"delivered_in_window": 0, "not_delivered_in_window": 0}
    for r in uncovered:
        quarter = _quarter(r["row"]["launch_ms"])
        by_quarter[quarter] = by_quarter.get(quarter, 0) + 1
        by_delivery[
            "delivered_in_window" if r["row"].get("delivery_ms") else "not_delivered_in_window"
        ] += 1
    share = _share(len(uncovered), n)
    assessed = "m4_funding_not_run" not in not_assessed and n > 0
    consistent = sum(1 for r in covered if r["row"].get("funding_interval_min") == 480)
    return {
        "eligible": n,
        "funding_uncovered": len(uncovered),
        "share": share,
        "partial": True,
        "status": _m4_status(assessed, share),
        "fail": (share or 0) > T["m4_funding_uncovered_max"],
        "rule": "pagination finished and no gap longer than 8 h (a coarse check, not proof of "
        "every settlement)",
        "covered_by_interval_snapshot": {
            "snapshot_is_8h": consistent,
            "snapshot_shorter_or_unknown": len(covered) - consistent,
        },
        "interval_note": "the catalog fundingInterval is a current snapshot, not the historical "
        "schedule",
        "by_reason": _count(uncovered, "funding_reason"),
        "by_launch_quarter": dict(sorted(by_quarter.items())),
        "by_delivery": by_delivery,
        "assessed": assessed,
        "pass": assessed and _at_most(share, T["m4_funding_uncovered_max"]),
    }


BAD_BAR_STATES: Final = ("starts_late", "interior_gap", "ends_early", "unavailable", "empty")


def _m5(
    data: BodyData,
    episodes: list[dict[str, Any]],
    candidates: dict[str, body.Candidate],
    not_assessed: list[str],
) -> dict[str, Any]:
    """M5 over the episodes after funding.

    `fail` is a proven lower-bound failure: an episode with a decided gap is bad whatever is
    still pending, an episode that is pending, unproven or still waiting for its funding
    decision may yet be fine (or leave the denominator), so it counts as a success. If even
    that optimistic share is below the limit the failure is known.
    """
    states: dict[str, dict[str, int]] = {"entry": {}, "mark": {}, "trade": {}}
    ok = unknown = 0
    for r in episodes:
        c = candidates[r["slot"]]
        delivery = r["row"].get("delivery_ms")
        entry = _entry_state(data, c)
        mark = discovery.bar_state(data.mark_hourly.get(c.slot), c.entry_ms, c.exit_ms, delivery)
        trade = discovery.bar_state(data.trade_hourly.get(c.slot), c.entry_ms, c.exit_ms, delivery)
        parts = (("entry", entry), ("mark", mark), ("trade", trade))
        for key, value in parts:
            states[key][value] = states[key].get(value, 0) + 1
        values = [value for _, value in parts]
        covered = ("covered", "confirmed_event")
        if entry == "present" and mark in covered and trade in covered:
            ok += 1
        elif r["funding"] == "pending" or (
            not any(x in ("absent", "unavailable", *BAD_BAR_STATES) for x in values)
            and any(x in ("not_run", "unproven") for x in values)
        ):
            unknown += 1
    n = len(episodes)
    share = _share(ok, n)
    optimistic = _share(ok + unknown, n)
    assessed = n > 0 and not any("not_run" in per for per in states.values())
    if not assessed and n:
        not_assessed.append("m5_bars_not_run")
    if any("unproven" in per for per in states.values()):
        assessed = False  # a page that cannot prove continuity is never green
        not_assessed.append("m5_series_unproven")
    return {
        "episodes": n,
        "with_bars": ok,
        "share": share,
        "states": {k: dict(sorted(v.items())) for k, v in states.items()},
        "level": "entry minute bar (trade) and hourly trade and mark series over the V1 path",
        "minute_level_path": "not_assessed_at_stage_1b",
        "assessed": assessed,
        "fail": n > 0 and (optimistic or 0) < T["m5_bars_min"],
        "pass": assessed and _at_least(share, T["m5_bars_min"]),
    }


def _m6(episodes: list[dict[str, Any]], provisional: bool = False) -> dict[str, Any]:
    counts = {"feasible": 0, "infeasible": 0, "unknown": 0}
    for r in episodes:
        counts[static_order(r["row"])] += 1
    n = len(episodes)
    share = _share(counts["feasible"], n)
    ok = _at_least(share, T["m6_feasible_min"])
    return {
        "episodes": n,
        "counts": counts,
        "share": share,
        "partial": True,
        "provisional": provisional,
        "status": "partial_pass" if ok else "fail",
        "not_checked": [
            "quantity step and minimum quantity against USD 50 at the entry price",
            "risk limit and position cap",
            "depth at entry",
        ],
        "pass": ok,
    }


def _stops(
    phase: str,
    m1: dict[str, Any],
    m2: dict[str, Any],
    m3: dict[str, Any],
    m4: dict[str, Any] | None,
    m5: dict[str, Any] | None,
    m6: dict[str, Any],
    n_primary: int | None,
    weeks: int | None,
    bound: dict[str, int] | None,
) -> list[str]:
    """Stop codes in the order of protocol section 8, rows 1 to 5. Only known failures count."""
    out: list[str] = []
    if m1["fail"]:
        out.append("not_reconstructible")
    m4_fail = m4 is not None and m4["fail"]  # a known failure needs no complete assessment
    m5_fail = m5 is not None and m5["fail"]
    if m2["fail"] or m4_fail or m5_fail:
        out.append("incomplete_coverage")
    if m3["assessed"] and not m3["pass"]:
        out.append("identity_unresolved")
    if phase == "a":
        assert bound is not None
        short = bound["candidates"] < T["min_episodes"] or bound["iso_weeks"] < T["min_weeks"]
    else:
        assert n_primary is not None and weeks is not None
        short = n_primary < T["min_episodes"] or weeks < T["min_weeks"]
    if short and not m1["pending"]:
        out.append("insufficient_events")
    if phase != "a" and not m6["pass"]:
        out.append("capital_infeasible")
    return out
