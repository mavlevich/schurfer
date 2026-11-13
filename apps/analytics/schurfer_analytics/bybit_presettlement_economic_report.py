"""B3a two-exit laboratory report from immutable native receipts, never an exam."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import bybit_presettlement_economics as source
from .bybit_carry_feasibility import publish_once
from .bybit_post_funding_screen import DAY_MS, MINUTE_MS, verified
from .bybit_presettlement_feasibility import FIRST_MS, INDEX_SHA256, LAST_MS
from .clustered_inference import percentile

SEED = 3103
DRAWS = 20_000
HALF = int(datetime(2025, 4, 1, tzinfo=UTC).timestamp() * 1000)


def day(at: int) -> str:
    return datetime.fromtimestamp(at / 1000, UTC).date().isoformat()


def week(at: int) -> str:
    iso = datetime.fromtimestamp(at / 1000, UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02}"


def weeks() -> list[str]:
    return list(dict.fromkeys(week(at) for at in range(FIRST_MS, LAST_MS + 1, DAY_MS)))


def funding(path: Path) -> dict[str, dict[str, Any]]:
    """Coverage is proven by successful native paging ranges, not a zero default."""
    verified(path, INDEX_SHA256)
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"rates": {}, "coverage": []})
    failed: set[str] = set()
    native: set[str] = set()
    stage = None
    with gzip.open(path, "rb") as stream:
        for line in stream:
            row = json.loads(line)
            kind, symbol = row["kind"], row["symbol"]
            if kind == "stage":
                stage = row["response"]["stage"]
            elif kind == "catalog":
                native.update(
                    r["symbol"]
                    for r in row["response"]["list"]
                    if r.get("contractType") == "LinearPerpetual" and r.get("settleCoin") == "USDT"
                )
            elif kind == "fetch_error":
                failed.add(symbol)
            elif kind == "funding":
                params, response = row["params"], row["response"]
                if (
                    params["symbol"] != symbol
                    or params["category"] != "linear"
                    or response["retCode"] != 0
                ):
                    raise ValueError("funding source identity/status differs")
                lo, hi, limit = (
                    int(params["startTime"]),
                    int(params["endTime"]),
                    int(params["limit"]),
                )
                values = response["result"]["list"]
                if (
                    not isinstance(values, list)
                    or len(values) > limit
                    or not 0 < limit <= 200
                    or lo > hi
                ):
                    raise ValueError("invalid funding page")
                times = []
                target = out[symbol]
                for value in values:
                    at, rate = int(value["fundingRateTimestamp"]), float(value["fundingRate"])
                    if value["symbol"] != symbol or not lo <= at <= hi or not math.isfinite(rate):
                        raise ValueError("invalid funding settlement")
                    if at in target["rates"] and target["rates"][at] != rate:
                        raise ValueError("conflicting funding settlement")
                    target["rates"][at] = rate
                    times.append(at)
                # A full backward page proves only its returned tail of the range.
                target["coverage"].append((min(times) if len(values) == limit else lo, hi))
    if stage != "discovery":
        raise ValueError("not the discovery funding index")
    for symbol in list(out):
        if symbol in failed or symbol not in native:
            del out[symbol]
            continue
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(out[symbol]["coverage"]):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        out[symbol]["coverage"] = merged
    return dict(out)


def paths(snapshot: dict[str, Any]) -> dict[str, dict[int, tuple[float, ...]] | None]:
    out = {}
    for row in snapshot["records"]:
        request = source.Request(**row["request"])
        if request.key != row["key"] or request.key in out:
            raise ValueError("repeated or wrong sealed key")
        body = base64.b64decode(row["body_base64"], validate=True)
        if hashlib.sha256(body).hexdigest() != row["body_sha256"]:
            raise ValueError("sealed native body corrupt")
        out[request.key] = source.bars(request, body) if row["outcome"] == "complete" else None
    return out


def evaluate(
    event: dict[str, Any],
    exit_name: str,
    native: dict[str, dict[int, tuple[float, ...]] | None],
    history: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    symbol, t, d = event["symbol"], event["forecast_settlement_ms"], event["decision_ms"]
    entry, exit_at = d + MINUTE_MS, t + source.EXITS[exit_name]
    out = {
        "symbol": symbol,
        "decision_ms": d,
        "forecast_settlement_ms": t,
        "entry_ms": entry,
        "exit_ms": exit_at,
        "cluster": f"{symbol}:{day(t)}",
        "week": week(t),
        "status": "unresolved",
        "reason": None,
    }

    def get(kind: str, asset: str = symbol) -> dict[int, tuple[float, ...]] | None:
        return native.get(source.Request(asset, d, t + 2 * MINUTE_MS, kind).key)

    price, mark, btc = get("price"), get("mark"), get("price", "BTCUSDT")
    if price is None or mark is None:
        return {**out, "reason": "native_price_or_mark_incomplete"}
    expected = set(range(d, t + 2 * MINUTE_MS, MINUTE_MS))
    if price.keys() != expected or mark.keys() != expected:
        return {**out, "reason": "native_path_gap"}
    if price[entry][4] <= 0 or price[exit_at][4] <= 0:
        return {**out, "reason": "zero_volume_execution_bar"}
    inst = history.get(symbol)
    if inst is None or not any(lo <= entry and hi >= exit_at for lo, hi in inst["coverage"]):
        return {**out, "reason": "funding_coverage_unknown"}
    opened, closed = price[entry][0], price[exit_at][0]
    gross = 10_000 * (closed / opened - 1)
    paid, worst, best = 0.0, 0.0, 0.0
    held = []
    for at, rate in sorted(inst["rates"].items()):
        if min(abs(at - entry), abs(at - exit_at)) <= 5000:
            return {**out, "reason": "funding_boundary_uncertain"}
        if not entry < at < exit_at:
            continue
        if at % MINUTE_MS or at not in mark:
            return {**out, "reason": "funding_mark_unavailable"}
        bar = mark[at]
        value = -10_000 * bar[0] / opened * rate
        bounds = [-10_000 * bar[i] / opened * rate for i in (1, 2)]
        paid += value
        worst += min(bounds)
        best += max(bounds)
        held.append(at)
    btc_return = None
    if btc is not None and btc.keys() == expected and btc[entry][4] > 0 and btc[exit_at][4] > 0:
        btc_return = 10_000 * (btc[exit_at][0] / btc[entry][0] - 1)
    return {
        **out,
        "status": "complete",
        "gross_bps": gross,
        "funding_bps": paid,
        "funding_worst_bps": worst,
        "funding_best_bps": best,
        "pre_cost_bps": gross + paid,
        "pre_cost_best_bps": gross + best,
        "net30_bps": gross + paid - 30,
        "net50_bps": gross + paid - 50,
        "net30_worst_bps": gross + worst - 30,
        "btc_adjusted_net_bps": None if btc_return is None else gross + paid - 30 - btc_return,
        "settlements_held": held,
        "reason": None,
    }


def clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["cluster"]].append(row)
    out = []
    fields = (
        "gross_bps",
        "funding_bps",
        "net30_bps",
        "net50_bps",
        "pre_cost_bps",
        "pre_cost_best_bps",
        "net30_worst_bps",
        "btc_adjusted_net_bps",
    )
    for key, values in sorted(grouped.items()):
        if any(r["status"] != "complete" for r in values):
            continue  # all-or-nothing cluster; missing members never disappear in its mean
        out.append(
            {
                "cluster": key,
                "symbol": values[0]["symbol"],
                "week": values[0]["week"],
                "at": min(r["forecast_settlement_ms"] for r in values),
                **{
                    f: None
                    if any(r[f] is None for r in values)
                    else statistics.fmean(r[f] for r in values)
                    for f in fields
                },
            }
        )
    return out


def bootstrap(
    values: list[dict[str, Any]], field: str, *, draws: int = DRAWS, block: int = 1
) -> dict[str, float | None]:
    if not values:
        return {"lower": None, "upper": None, "se": None, "mde_zero_bps": None}
    calendar = weeks()
    totals = {
        w: (sum(r[field] for r in values if r["week"] == w), sum(r["week"] == w for r in values))
        for w in calendar
    }
    rng = random.Random(SEED)  # noqa: S311 - registered reproducible research PRNG
    samples = []
    for _ in range(draws):
        picked: list[str] = []
        while len(picked) < len(calendar):
            start = rng.randrange(len(calendar))
            picked.extend(calendar[(start + k) % len(calendar)] for k in range(block))
        selected = [totals[w] for w in picked[: len(calendar)]]
        n = sum(v[1] for v in selected)
        if n:
            samples.append(sum(v[0] for v in selected) / n)
    if len(samples) < 0.95 * draws:
        return {"lower": None, "upper": None, "se": None, "mde_zero_bps": None}
    ordered = tuple(sorted(samples))
    se = statistics.stdev(samples)
    return {
        "lower": percentile(ordered, 0.025),
        "upper": percentile(ordered, 0.975),
        "se": se,
        "mde_zero_bps": (1.96 + 0.842) * se,
    }


def mean(rows: list[dict[str, Any]], field: str = "net30_bps") -> float | None:
    return statistics.fmean(r[field] for r in rows) if rows else None


def summarize(rows: list[dict[str, Any]], *, draws: int = DRAWS) -> dict[str, Any]:
    resolved = [r for r in rows if r["status"] == "complete"]
    grouped = clusters(rows)
    coverage = len(resolved) / len(rows) if rows else 0
    symbols = Counter(r["symbol"] for r in grouped)
    top_share = sum(n for _, n in symbols.most_common(5)) / len(grouped) if grouped else 1
    contributed: dict[str, dict[str, float]] = {
        "symbol": defaultdict(float),
        "week": defaultdict(float),
    }
    for r in grouped:
        for kind in contributed:
            contributed[kind][r[kind]] += r["net30_bps"]
    trimmed: dict[str, dict[str, Any]] = {}
    for kind, contributions in contributed.items():
        top = sorted(contributions, key=lambda k: (-contributions[k], k))[:5]
        trimmed[kind] = {
            "removed": top,
            "net30_bps": mean([r for r in grouped if r[kind] not in top]),
        }
    halves = [mean([r for r in grouped if (r["at"] < HALF) == first]) for first in (True, False)]
    bound = bootstrap(grouped, "net30_bps", draws=draws)
    upper = bootstrap(grouped, "pre_cost_best_bps", draws=draws)
    btc_rows = [r for r in grouped if r["btc_adjusted_net_bps"] is not None]
    btc_events = sum(r["btc_adjusted_net_bps"] is not None for r in resolved)
    net = mean(grouped)
    btc_net = mean(btc_rows, "btc_adjusted_net_bps")
    worst_net = mean(grouped, "net30_worst_bps")
    mature = len(grouped) >= 150 and coverage >= 0.95
    conditions = {
        "coverage": coverage >= 0.95,
        "clusters": len(grouped) >= 150,
        "symbols": len(symbols) >= 40,
        "weeks": len({r["week"] for r in grouped}) >= 26,
        "top5_symbol_share": top_share <= 0.30,
        "size": net is not None and net >= 20,
        "lower_bound": bound["lower"] is not None and bound["lower"] > 0,
        "halves": all(v is not None and v > 0 for v in halves),
        "without_top5": all(
            v["net30_bps"] is not None and v["net30_bps"] > 0 for v in trimmed.values()
        ),
        "btc_coverage": bool(rows) and btc_events / len(rows) >= 0.95,
        "btc_adjusted_net": btc_net is not None and btc_net > 0,
        "funding_sensitivity": worst_net is not None and worst_net > 0,
    }
    if mature and net is not None and net < 0:
        verdict = "fail"
    elif not mature or not all(
        conditions[k] for k in ("symbols", "weeks", "top5_symbol_share", "btc_coverage")
    ):
        verdict = "insufficient_data"
    else:
        verdict = "nominated_for_protocol" if all(conditions.values()) else "not_nominated"
    values = [r["net30_bps"] for r in grouped]
    wins, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return {
        "verdict": verdict,
        "conditions": conditions,
        "opportunities": len(rows),
        "resolved_events": len(resolved),
        "complete_event_share": coverage,
        "unresolved_reasons": dict(Counter(r["reason"] for r in rows if r["status"] != "complete")),
        "complete_clusters": len(grouped),
        "symbols": len(symbols),
        "weeks": len({r["week"] for r in grouped}),
        "top5_symbol_share": top_share,
        "cluster_mean_gross_bps": mean(grouped, "gross_bps"),
        "cluster_mean_funding_bps": mean(grouped, "funding_bps"),
        "cluster_mean_net30_bps": net,
        "cluster_mean_net50_bps": mean(grouped, "net50_bps"),
        "cluster_median_net30_bps": statistics.median(values) if values else None,
        "cluster_profit_factor": wins / losses if losses else None,
        "cluster_win_share": sum(v > 0 for v in values) / len(values) if values else None,
        "btc_adjusted_net_bps": mean(btc_rows, "btc_adjusted_net_bps"),
        "half_net30_bps": halves,
        "trimmed": trimmed,
        "net30_week_bootstrap": bound,
        "net30_two_week_bootstrap": bootstrap(grouped, "net30_bps", draws=draws, block=2),
        "budget_upper_pre_cost_best_bps": upper["upper"],
        "budget_assessable": mature,
    }


def portfolio(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def order(row: dict[str, Any]) -> tuple[int, str]:
        key = f"b3a-v1|{row['symbol']}|{row['decision_ms']}".encode()
        return row["entry_ms"], hashlib.sha256(key).hexdigest()

    cash, peak, drawdown = 50.0, 50.0, 0.0
    pending = None
    unknown = False
    closed = streak = longest = 0
    decisions: list[dict[str, Any]] = []
    daily: dict[str, float] = defaultdict(float)

    def close(row: dict[str, Any]) -> None:
        nonlocal cash, peak, drawdown, closed, streak, longest
        pnl = 45 * row["net30_bps"] / 10_000
        cash += pnl
        daily[day(row["exit_ms"])] += pnl
        peak = max(peak, cash)
        drawdown = max(drawdown, peak - cash)
        closed += 1
        streak = streak + 1 if pnl < 0 else 0
        longest = max(longest, streak)

    for row in sorted(rows, key=order):
        if pending is not None and pending["exit_ms"] <= row["entry_ms"]:
            close(pending)
            pending = None
        if unknown:
            reason = "portfolio_value_unknown"
        elif pending is not None:
            reason = "slot_busy"
        elif cash < 50:  # fixed USD45 notional plus USD5 reserve, no borrowing
            reason = "capital_insufficient"
        elif row["status"] != "complete":
            reason, unknown = "selected_path_unresolved", True
        else:
            pending, reason = row, "accepted"
        decisions.append(
            {"symbol": row["symbol"], "decision_ms": row["decision_ms"], "reason": reason}
        )
    if pending is not None:
        close(pending)
    return {
        "initial_cash_usd": 50,
        "notional_usd": 45,
        "reserve_usd": 5,
        "final_cash_usd": None if unknown else cash,
        "known_closed_trades": closed,
        "realized_drawdown_usd_known_prefix": drawdown,
        "longest_losing_streak_known_prefix": longest,
        "daily_realized_pnl_usd_known_prefix": dict(sorted(daily.items())),
        "accepted_holding_ms": sum(
            r["exit_ms"] - r["entry_ms"]
            for r in rows
            if any(
                d["reason"] == "accepted"
                and d["symbol"] == r["symbol"]
                and d["decision_ms"] == r["decision_ms"]
                for d in decisions
            )
        ),
        "reason_counts": dict(Counter(r["reason"] for r in decisions)),
        "decisions": decisions,
        "gates_nomination": False,
        "limitations": (
            "realized-only drawdown, unknown prefix retained; not margin/liquidation evidence"
        ),
    }


def report(
    events: list[dict[str, Any]],
    snapshot: dict[str, Any],
    history: dict[str, dict[str, Any]],
    *,
    draws: int = DRAWS,
) -> dict[str, Any]:
    native = paths(snapshot)
    policies = {}
    for exit_name in source.EXITS:
        rows = [evaluate(e, exit_name, native, history) for e in events]
        policies[exit_name] = {
            **summarize(rows, draws=draws),
            "events": rows,
            "portfolio": portfolio(rows),
        }
    if not all(
        p["budget_assessable"] and p["budget_upper_pre_cost_best_bps"] is not None
        for p in policies.values()
    ):
        budget = "budget_not_assessable"
    elif all(p["budget_upper_pre_cost_best_bps"] < 15 for p in policies.values()):
        budget = "track_b_paused_budget"
    else:
        budget = "maker_source_review_needed"
    return {
        "version": source.VERSION,
        "stage": "discovery",
        "budget_verdict": budget,
        "budget_is_not_maker_ev_bound": True,
        "policies": policies,
        "inference": {
            "draws": draws,
            "seed": SEED,
            "calendar_weeks": weeks(),
            "two_week_blocks": "circular, fixed calendar length, paired seed",
        },
        "promotes_to_live": False,
        "opens_exam": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sealed-sha256", required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    events_path, scope = Path(source.EVENTS), Path(source.SCOPE)
    events = source.load_events(events_path)
    contract = source.binding(events_path, scope)
    saved = json.loads(verified(args.root / "sealed.json", args.sealed_sha256).read_bytes())
    rebuilt = source.sealed(args.root, contract, source.frozen_requests(events_path, scope))
    if saved != json.loads(json.dumps(rebuilt)):
        raise ValueError("sealed artifact differs from durable receipts")
    result = report(events, saved, funding(args.index))
    result["provenance"] = {
        "contract": contract,
        "sealed_sha256": args.sealed_sha256,
        "index_sha256": INDEX_SHA256,
        "dirty": False,
    }
    sha = publish_once(args.output, result)
    sys.stdout.write(
        json.dumps(
            {
                "sha256": sha,
                "budget_verdict": result["budget_verdict"],
                "policies": {k: v["verdict"] for k, v in result["policies"].items()},
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
