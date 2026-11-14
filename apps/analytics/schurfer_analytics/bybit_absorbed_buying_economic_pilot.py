"""HYP-036 economic pilot (`hyp036-econ-v1`): one spending decision, never a test.

Contract: docs/research/hyp036-economic-pilot-v1.md. ``plan`` uses frozen event
identities and funding settlement timestamps only. ``prepare``/``fetch``/``seal``
need the code, contract, registry and request list published in main and the
owner's go. ``read`` computes only the contract's report from sealed receipts.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from . import bybit_absorbed_buying_power_audit as audit
from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economic_report as b3report
from . import bybit_presettlement_economics as b3
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import HOUR_MS
from .bybit_post_funding_screen import MINUTE_MS, verified
from .bybit_presettlement_feasibility import INDEX_SHA256

VERSION: Final = "hyp036_econ_v1"
TAG: Final = "hyp036-econ-v1"
EVENTS: Final = "docs/research/evidence/hyp036-power-audit-v1/pairs.json.gz"
EVENTS_SHA256: Final = audit.EVENTS_SHA256  # of the uncompressed frozen pairs.json
CONTRACT: Final = "docs/research/hyp036-economic-pilot-v1.md"
REGISTRY: Final = "docs/research/seen-windows-registry.md"
SCOPE: Final = "docs/research/evidence/hyp036-economic-pilot-v1/requests.json.gz"
BENCHMARK: Final = "BTCUSDT"
HOLD_MS: Final = 4 * HOUR_MS
CAP_BYTES: Final = 128 * 1024**2
# Fixed by the contract from the outcome-blind coverage check.
EXPECTED: Final = {"events": 784, "clusters": 750, "price": 784, "mark": 546, "btc": 393}
COMPLETE_SHARE: Final = 0.95
COSTS_BPS: Final = (30, 50)
TOP_REMOVED: Final = 5
JULY_BLOCK_START: Final = int(datetime(2025, 4, 1, tzinfo=UTC).timestamp() * 1000)
# Observed-size raw MDE of the bounded audit (audit.json.gz, 5bf24449...).
AUDIT_OBSERVED_MDE_BPS: Final = 68.65177415025077
PLANNING_Z: Final = 1.645 + 0.842


@dataclass(frozen=True)
class Request(b3.Request):
    """B3a transport and decoder; a distinct request identity for this study."""

    @property
    def key(self) -> str:
        return transport.digest(
            f"{VERSION}:{self.kind}:{self.symbol}:{self.start}:{self.end}".encode()
        )


def day(at: int) -> str:
    return datetime.fromtimestamp(at / 1000, UTC).date().isoformat()


def week(at: int) -> str:
    iso = datetime.fromtimestamp(at / 1000, UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02}"


def load_events(path: Path) -> list[dict[str, Any]]:
    """The frozen pilot identities, validated by the audit's own reader."""
    raw = gzip.decompress(path.read_bytes())
    if hashlib.sha256(raw).hexdigest() != EVENTS_SHA256:
        raise ValueError("frozen pilot event identities differ")
    members = audit.read_events(json.loads(raw))
    events: list[dict[str, Any]] = [{"symbol": m.symbol, "t": m.d} for m in members]
    if len(events) != EXPECTED["events"]:
        raise ValueError("frozen event count differs")
    if len({f"{e['symbol']}:{day(e['t'])}" for e in events}) != EXPECTED["clusters"]:
        raise ValueError("frozen cluster count differs")
    if any(e["symbol"] == BENCHMARK for e in events):
        raise ValueError("benchmark cannot be an event")
    return events


def hold(t: int) -> tuple[int, int]:
    """Entry at the open of the bar starting t + 1 min; exit at the close of the
    bar ending t + 1 min + 4 h, which starts at t + 4 h."""
    return t + MINUTE_MS, t + MINUTE_MS + HOLD_MS


def settlements(history: dict[str, dict[str, Any]], symbol: str, t: int) -> list[int]:
    entry, exit_at = hold(t)
    inst = history.get(symbol)
    if inst is None:
        return []
    return sorted(at for at in inst["rates"] if entry < at < exit_at)


def requests_for(events: list[dict[str, Any]], history: dict[str, dict[str, Any]]) -> list[Request]:
    """Perp per event, mark only where a settlement falls inside the hold, BTC per t."""
    out: set[Request] = set()
    for e in events:
        entry, exit_at = hold(e["t"])
        out.add(Request(e["symbol"], entry, exit_at, "price"))
        if settlements(history, e["symbol"], e["t"]):
            out.add(Request(e["symbol"], entry, exit_at, "mark"))
        out.add(Request(BENCHMARK, entry, exit_at, "price"))
    return sorted(out, key=lambda r: r.key)


def counts(requests: list[Request]) -> dict[str, int]:
    return {
        "price": sum(r.kind == "price" and r.symbol != BENCHMARK for r in requests),
        "mark": sum(r.kind == "mark" for r in requests),
        "btc": sum(r.kind == "price" and r.symbol == BENCHMARK for r in requests),
        "total": len(requests),
    }


def frozen_requests(events: Path, index: Path, scope: Path) -> list[Request]:
    requests = requests_for(load_events(events), b3report.funding(index))
    observed = counts(requests)
    if any(observed[k] != EXPECTED[k] for k in ("price", "mark", "btc")):
        raise ValueError(f"request counts differ from the contract: {observed}")
    if json.loads(gzip.decompress(scope.read_bytes())) != [asdict(r) for r in requests]:
        raise ValueError("request list differs from the frozen scope")
    for r in requests:
        if r.end - r.start != HOLD_MS or r.start % MINUTE_MS:
            raise ValueError("request outside the registered hold windows")
    return requests


def binding(events: Path, index: Path, scope: Path) -> dict[str, Any]:
    published = b3.publication(
        [
            CONTRACT,
            REGISTRY,
            SCOPE,
            EVENTS,
            "apps/analytics/schurfer_analytics/bybit_absorbed_buying_economic_pilot.py",
            "apps/analytics/schurfer_analytics/bybit_presettlement_economics.py",
            "apps/analytics/schurfer_analytics/bybit_presettlement_bounded_fetch.py",
            "apps/analytics/schurfer_analytics/bybit_presettlement_economic_report.py",
        ]
    )
    scope_sha = transport.digest(scope.read_bytes())
    if scope_sha not in Path(REGISTRY).read_text():
        raise ValueError("no registry row names this request list")
    requests = frozen_requests(events, index, scope)
    limits = transport.Limits(
        attempts=2 * len(requests),
        body_bytes=CAP_BYTES,
        pilot_attempts=2 * len(requests),
        pilot_bytes=CAP_BYTES,
    )
    return {
        "rules": VERSION,
        "revision": transport.clean_revision(),
        "publication": published,
        "scope_sha256": scope_sha,
        "events_sha256": EVENTS_SHA256,
        "index_sha256": INDEX_SHA256,
        "limits": asdict(limits),
        "endpoints": b3.ENDPOINTS,
    }


def paths(snapshot: dict[str, Any]) -> dict[str, dict[int, tuple[float, ...]] | None]:
    """Bars of complete and incomplete responses; only entry/exit/settlement bars matter."""
    out: dict[str, dict[int, tuple[float, ...]] | None] = {}
    for row in snapshot["records"]:
        request = Request(**row["request"])
        if request.key != row["key"] or request.key in out:
            raise ValueError("repeated or wrong sealed key")
        body = base64.b64decode(row["body_base64"], validate=True)
        if hashlib.sha256(body).hexdigest() != row["body_sha256"]:
            raise ValueError("sealed native body corrupt")
        usable = row["outcome"] in ("complete", "incomplete")
        out[request.key] = b3.bars(request, body) if usable else None
    return out


def evaluate(
    event: dict[str, Any],
    native: dict[str, dict[int, tuple[float, ...]] | None],
    history: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    symbol, t = event["symbol"], event["t"]
    entry, exit_at = hold(t)
    exit_bar = exit_at - MINUTE_MS
    out: dict[str, Any] = {
        "symbol": symbol,
        "decision_ms": t,
        "entry_ms": entry,
        "exit_ms": exit_at,
        "cluster": f"{symbol}:{day(t)}",
        "week": week(t),
        "block": "january" if t < JULY_BLOCK_START else "july",
        "status": "incomplete",
        "reason": None,
        "btc_status": "incomplete",
        "btc_reason": None,
    }

    def get(kind: str, asset: str = symbol) -> dict[int, tuple[float, ...]] | None:
        return native.get(Request(asset, entry, exit_at, kind).key)

    btc = get("price", BENCHMARK)
    btc_return = None
    if btc is None:
        out["btc_reason"] = "btc_unavailable"
    elif entry not in btc or exit_bar not in btc:
        out["btc_reason"] = "btc_bar_missing"
    elif btc[entry][4] <= 0 or btc[exit_bar][4] <= 0:
        out["btc_reason"] = "btc_zero_volume"
    else:
        btc_return = 10_000 * (btc[exit_bar][3] - btc[entry][0]) / btc[entry][0]
        out["btc_status"] = "complete"

    price = get("price")
    if price is None:
        return {**out, "reason": "price_unavailable"}
    if entry not in price:
        return {**out, "reason": "entry_bar_missing"}
    if exit_bar not in price:
        return {**out, "reason": "exit_bar_missing"}
    if price[entry][4] <= 0 or price[exit_bar][4] <= 0:
        return {**out, "reason": "zero_volume_execution_bar"}
    inst = history.get(symbol)
    if inst is None or not any(lo <= entry and exit_at <= hi for lo, hi in inst["coverage"]):
        return {**out, "reason": "funding_coverage_unproven"}
    opened, closed = price[entry][0], price[exit_bar][3]
    held = settlements(history, symbol, t)
    mark = get("mark") if held else {}
    funding = low_high_worst = low_high_best = 0.0
    for at in held:
        if mark is None or at % MINUTE_MS or at not in mark:
            return {**out, "reason": "funding_mark_unavailable"}
        rate, bar = inst["rates"][at], mark[at]
        funding += rate * bar[0] / opened  # the short receives a positive rate
        bounds = [rate * bar[i] / opened for i in (1, 2)]
        low_high_worst += min(bounds)
        low_high_best += max(bounds)
    gross = (opened - closed) / opened
    net30 = 10_000 * (gross + funding) - COSTS_BPS[0]
    return {
        **out,
        "status": "complete",
        "entry_price": opened,
        "exit_price": closed,
        "gross_bps": 10_000 * gross,
        "net30_without_funding_bps": 10_000 * gross - COSTS_BPS[0],
        "funding_bps": 10_000 * funding,
        "pre_cost_bps": 10_000 * (gross + funding),
        "net30_bps": net30,
        "net50_bps": 10_000 * (gross + funding) - COSTS_BPS[1],
        "net30_mark_worst_bps": 10_000 * (gross + low_high_worst) - COSTS_BPS[0],
        "net30_mark_best_bps": 10_000 * (gross + low_high_best) - COSTS_BPS[0],
        "btc_price_return_bps": btc_return,
        "btc_adjusted_bps": None if btc_return is None else net30 + btc_return,
        "settlements_held": held,
    }


FIELDS: Final = (
    "gross_bps",
    "net30_without_funding_bps",
    "funding_bps",
    "pre_cost_bps",
    "net30_bps",
    "net50_bps",
    "net30_mark_worst_bps",
    "net30_mark_best_bps",
)


def clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fully resolved clusters only; a missing member is never filled with zero."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["cluster"]].append(row)
    out = []
    for key, values in sorted(grouped.items()):
        if any(r["status"] != "complete" for r in values):
            continue
        btc = [r["btc_adjusted_bps"] for r in values if r["btc_adjusted_bps"] is not None]
        out.append(
            {
                "cluster": key,
                "symbol": values[0]["symbol"],
                "week": values[0]["week"],
                "block": values[0]["block"],
                **{f: statistics.fmean(r[f] for r in values) for f in FIELDS},
                # BTC-incomplete members drop the cluster from the diagnostic only.
                "btc_adjusted_bps": statistics.fmean(btc) if len(btc) == len(values) else None,
            }
        )
    return out


def mean(rows: list[dict[str, Any]], field: str = "net30_bps") -> float | None:
    values = [r[field] for r in rows if r[field] is not None]
    return statistics.fmean(values) if values else None


def top_removed(grouped: list[dict[str, Any]]) -> dict[str, Any]:
    """Symbols ranked by the sum of their complete clusters' contributions to the mean."""
    n = len(grouped)
    contribution: dict[str, float] = defaultdict(float)
    for r in grouped:
        contribution[r["symbol"]] += r["net30_bps"] / n
    removed = sorted(contribution, key=lambda s: (-contribution[s], s.encode()))[:TOP_REMOVED]
    rest = [r for r in grouped if r["symbol"] not in removed]
    return {"removed": removed, "net30_bps": mean(rest), "clusters_left": len(rest)}


def decide(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = clusters(rows)
    total_clusters = len({r["cluster"] for r in rows})
    event_share = sum(r["status"] == "complete" for r in rows) / len(rows) if rows else 0.0
    cluster_share = len(grouped) / total_clusters if total_clusters else 0.0
    net = mean(grouped)
    trimmed = top_removed(grouped) if grouped else {"removed": [], "net30_bps": None}
    if event_share < COMPLETE_SHARE or cluster_share < COMPLETE_SHARE:
        verdict = "not_assessable"
    elif (
        net is not None
        and net > 0
        and trimmed["net30_bps"] is not None
        and trimmed["net30_bps"] > 0
    ):
        verdict = "continue_to_discovery_protocol"
    else:
        verdict = "stop_spending"
    return {
        "verdict": verdict,
        "complete_event_share": event_share,
        "complete_cluster_share": cluster_share,
        "cluster_mean_net30_bps": net,
        "without_top5": trimmed,
    }


def funding_share(rows: list[dict[str, Any]]) -> float | None:
    """Mean funding over mean net at 30 bps; undefined when either is missing or net is 0."""
    net, fund = mean(rows), mean(rows, "funding_bps")
    return None if not net or fund is None else fund / net


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = clusters(rows)
    symbols = Counter(r["symbol"] for r in grouped)
    top_share = (
        sum(n for _, n in symbols.most_common(TOP_REMOVED)) / len(grouped) if grouped else None
    )
    pre = mean(grouped, "pre_cost_bps")
    values = [r["net30_bps"] for r in grouped]
    cluster_se = statistics.stdev(values) / len(values) ** 0.5 if len(values) > 1 else None
    btc_rows = [r for r in grouped if r["btc_adjusted_bps"] is not None]
    by_block = {}
    for block in ("january", "july"):
        part = [r for r in grouped if r["block"] == block]
        by_block[block] = {
            "clusters": len(part),
            **{f"cluster_mean_{f}": mean(part, f) for f in FIELDS},
            "funding_share_of_net30": funding_share(part),
        }
    return {
        "events": len(rows),
        "complete_events": sum(r["status"] == "complete" for r in rows),
        "incomplete_reasons": dict(
            sorted(Counter(r["reason"] for r in rows if r["status"] != "complete").items())
        ),
        "clusters": len({r["cluster"] for r in rows}),
        "complete_clusters": len(grouped),
        "symbols": len(symbols),
        "weeks": len({r["week"] for r in grouped}),
        "top5_symbol_share": top_share,
        **{f"cluster_mean_{f}": mean(grouped, f) for f in FIELDS},
        "funding_share_of_net30": funding_share(grouped),
        "break_even_round_trip_bps": pre,
        "by_block": by_block,
        "btc_diagnostic": {
            "complete_events": sum(r["btc_status"] == "complete" for r in rows),
            "incomplete_reasons": dict(
                sorted(Counter(r["btc_reason"] for r in rows if r["btc_reason"]).items())
            ),
            "clusters": len(btc_rows),
            "cluster_mean_btc_adjusted_bps": mean(btc_rows, "btc_adjusted_bps"),
            "is_tradable_hedge": False,
        },
        "uncertainty": {
            "cluster_standard_error_bps": cluster_se,
            "cluster_standard_error_understates": "same-hour and same-week dependence",
            "audit_planning_standard_error_bps": AUDIT_OBSERVED_MDE_BPS / PLANNING_Z,
        },
    }


def report(
    events: list[dict[str, Any]],
    snapshot: dict[str, Any],
    history: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    native = paths(snapshot)
    rows = [evaluate(e, native, history) for e in events]
    return {
        "version": VERSION,
        "study": TAG,
        "stage": "discovery_spending_screen",
        "decision": decide(rows),
        "summary": summary(rows),
        "portfolio": b3report.portfolio(rows, tag=TAG),
        "events": rows,
        "confirms_strategy": False,
        "opens_exam": False,
        "promotes_to_live": False,
    }


def sealed(root: Path, contract: dict[str, Any], requests: list[Request]) -> dict[str, Any]:
    items: list[b3.Request] = list(requests)
    return b3.sealed(root, contract, items, request_factory=Request, version=VERSION)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "prepare", "fetch", "seal", "read"))
    parser.add_argument("--events", type=Path, default=Path(EVENTS))
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--scope", type=Path, default=Path(SCOPE))
    parser.add_argument("--root", type=Path, default=Path("runtime/research/hyp036-econ-v1"))
    parser.add_argument("--sealed-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "plan":
        requests = requests_for(load_events(args.events), b3report.funding(args.index))
        raw = json.dumps([asdict(r) for r in requests], sort_keys=True, separators=(",", ":"))
        args.scope.parent.mkdir(parents=True, exist_ok=True)
        with args.scope.open("xb") as stream:
            stream.write(gzip.compress(raw.encode(), mtime=0))
        result: dict[str, Any] = {
            **counts(requests),
            "sha256": transport.digest(args.scope.read_bytes()),
        }
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    contract = binding(args.events, args.index, args.scope)
    requests = frozen_requests(args.events, args.index, args.scope)
    if args.command == "prepare":
        transport.prepare(args.root, contract, requests, [r.key for r in requests])
    elif args.command == "fetch":
        with (
            b3.run(args.root, contract, request_factory=Request) as state,
            httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client,
        ):
            result = state.fetch("pilot", client)  # every frozen key, not a sample
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    elif args.command == "seal":
        sha = publish_once(args.root / "sealed.json", sealed(args.root, contract, requests))
        sys.stdout.write(json.dumps({"sha256": sha, "requests": len(requests)}) + "\n")
    else:
        if not args.sealed_sha256 or args.output is None:
            raise SystemExit("read needs --sealed-sha256 and --output")
        saved = json.loads(verified(args.root / "sealed.json", args.sealed_sha256).read_bytes())
        if saved != json.loads(json.dumps(sealed(args.root, contract, requests))):
            raise ValueError("sealed artifact differs from durable receipts")
        started = datetime.now(UTC).isoformat()
        result = report(load_events(args.events), saved, b3report.funding(args.index))
        result["provenance"] = {
            "started_at_utc": started,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "contract": contract,
            "sealed_sha256": args.sealed_sha256,
            "index_sha256": INDEX_SHA256,
            "dirty": False,
        }
        sha = publish_once(args.output, result)
        sys.stdout.write(
            json.dumps({"sha256": sha, "verdict": result["decision"]["verdict"]}) + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
