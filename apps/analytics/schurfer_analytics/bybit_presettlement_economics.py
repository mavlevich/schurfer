"""B3a frozen native price/mark scope and bounded transport, never a live broker.

Scope preparation uses event metadata only. Fetch requires main-published code,
contract and registry. The separate report reads only sealed immutable receipts.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from .bybit_carry_feasibility import publish_once
from .bybit_post_funding_screen import MINUTE_MS, verified
from .bybit_presettlement_feasibility import FIRST_MS, INDEX_SHA256, LAST_MS

VERSION = "b3a_native_minute_taker_v1"
EVENTS_SHA = "4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7"
EVENTS = "docs/research/evidence/b1-prior-only-4h-2026-11-13/read/events.json.gz"
CONTRACT = "docs/research/b3-economic-screen-proposal-v1.md"
REGISTRY = "docs/research/seen-windows-registry.md"
SCOPE = "docs/research/evidence/b3-economic-screen-proposal-2026-11-13/requests.json.gz"
CAP = 256 * 1024**2
EXITS = {"before": -MINUTE_MS, "through": MINUTE_MS}
ENDPOINTS = {
    "price": "https://api.bybit.com/v5/market/kline",
    "mark": "https://api.bybit.com/v5/market/mark-price-kline",
}


@dataclass(frozen=True)
class Request(transport.Request):
    kind: str = "price"

    @property
    def key(self) -> str:
        return transport.digest(
            f"{VERSION}:{self.kind}:{self.symbol}:{self.start}:{self.end}".encode()
        )


def endpoint(request: transport.Request) -> str:
    if not isinstance(request, Request) or request.kind not in ENDPOINTS:
        raise ValueError("not a registered B3 endpoint")
    return ENDPOINTS[request.kind]


def load_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(
        gzip.decompress(verified(path, EVENTS_SHA).read_bytes())
    )["events_by_decision"]["d30"]
    if len(rows) != 1637 or len({(r["symbol"], r["decision_ms"]) for r in rows}) != 1637:
        raise ValueError("frozen event identity differs")
    for row in rows:
        t, d = row["forecast_settlement_ms"], row["decision_ms"]
        if (
            not re.fullmatch(r"[A-Z0-9]+", row["symbol"])
            or row["interval_minutes"] != 240
            or d != t - 30 * MINUTE_MS
            or t % MINUTE_MS
            or d < FIRST_MS
            or t + 2 * MINUTE_MS - 1 > LAST_MS
        ):
            raise ValueError("event outside registered discovery identity/times")
    return rows


def requests_for(rows: list[dict[str, Any]]) -> list[Request]:
    out = set()
    for row in rows:
        t, symbol = row["forecast_settlement_ms"], row["symbol"]
        for s, kind in ((symbol, "price"), (symbol, "mark"), ("BTCUSDT", "price")):
            out.add(Request(s, t - 30 * MINUTE_MS, t + 2 * MINUTE_MS, kind))
    return sorted(out, key=lambda r: r.key)


def frozen_requests(events: Path, scope: Path) -> list[Request]:
    requests = requests_for(load_events(events))
    expected = [asdict(r) for r in requests]
    if json.loads(gzip.decompress(scope.read_bytes())) != expected:
        raise ValueError("scope differs from frozen events")
    if len(requests) > 4200:
        raise ValueError("scope exceeds registered upper bound")
    return requests


def bars(request: Request, body: bytes) -> dict[int, tuple[float, ...]]:
    """Validate native identity and OHLCV, including conflicts within one response."""
    data = json.loads(body)
    if data["retCode"] != 0:
        raise ValueError("native API error")
    result = data["result"]
    if result["symbol"] != request.symbol or result["category"] != "linear":
        raise ValueError("native identity differs")
    rows = result["list"]
    if not isinstance(rows, list):
        raise ValueError("native rows are not a list")
    out: dict[int, tuple[float, ...]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != (7 if request.kind == "price" else 5):
            raise ValueError("native bar shape differs")
        if any(isinstance(v, bool) or v is None for v in row):
            raise ValueError("boolean/null in native bar")
        at, values = int(row[0]), tuple(float(v) for v in row[1:])
        if str(at) != str(row[0]) or not request.start <= at < request.end or at % MINUTE_MS:
            raise ValueError("native bar outside bound window")
        if not all(math.isfinite(v) for v in values):
            raise ValueError("non-finite native bar")
        o, h, low, c = values[:4]
        if min(o, h, low, c) <= 0 or not low <= min(o, c) <= max(o, c) <= h:
            raise ValueError("invalid native OHLC")
        if any(v < 0 for v in values[4:]):
            raise ValueError("negative volume/turnover")
        if at in out and out[at] != values:
            raise ValueError("conflicting duplicate minute")
        out[at] = values
    return out


def validate(request: transport.Request, body: bytes, status: int, error: str | None) -> str:
    if error == "unsupported_content_encoding":
        return "invalid"
    if error or not 200 <= status < 300:
        return "retry"
    try:
        if json.loads(body).get("retCode") != 0:
            return "retry"
        if not isinstance(request, Request) or request.kind not in ENDPOINTS:
            return "invalid"
        parsed = bars(request, body)
        return (
            "complete"
            if set(parsed) == set(range(request.start, request.end, MINUTE_MS))
            else "incomplete"
        )
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
        return "invalid"


def publication(paths: list[str]) -> dict[str, str]:
    """Require exact code and contract in fetched origin/main before native reads."""
    git = shutil.which("git")
    if git is None:
        raise ValueError("git required")
    transport.clean_revision()
    if subprocess.run(  # noqa: S603 - fixed git arguments
        [git, "merge-base", "--is-ancestor", "HEAD", "origin/main"], check=False
    ).returncode:
        raise ValueError("current revision has not been published in main")
    out = {}
    for path in paths:
        native = Path(path).read_bytes()
        published = subprocess.check_output([git, "show", f"origin/main:{path}"])  # noqa: S603
        if native != published:
            raise ValueError(f"main publication differs: {path}")
        out[path] = transport.digest(native)
    return out


def binding(events: Path, scope: Path) -> dict[str, Any]:
    paths = [
        CONTRACT,
        REGISTRY,
        SCOPE,
        "apps/analytics/schurfer_analytics/bybit_presettlement_economics.py",
        "apps/analytics/schurfer_analytics/bybit_presettlement_bounded_fetch.py",
        "apps/analytics/schurfer_analytics/bybit_presettlement_economic_report.py",
    ]
    published = publication(paths)
    requests = frozen_requests(events, scope)
    limits = transport.Limits(
        attempts=2 * len(requests),
        body_bytes=CAP,
        pilot_attempts=2 * len(requests),
        pilot_bytes=CAP,
    )
    return {
        "rules": VERSION,
        "revision": transport.clean_revision(),
        "publication": published,
        "scope_sha256": transport.digest(scope.read_bytes()),
        "events_sha256": EVENTS_SHA,
        "index_sha256": INDEX_SHA256,
        "limits": asdict(limits),
        "endpoints": ENDPOINTS,
    }


def run(root: Path, contract: dict[str, Any], **kwargs: Any) -> transport.Run:
    return transport.Run(
        root, contract, request_factory=Request, validator=validate, endpoint=endpoint, **kwargs
    )


def sealed(root: Path, contract: dict[str, Any], requests: list[Request]) -> dict[str, Any]:
    with run(root, contract) as state:
        if set(state.requests) != {r.key for r in requests}:
            raise ValueError("run request set differs from frozen scope")
        records = []
        for key in sorted(state.requests):
            attempts = state.receipts[key]
            if not attempts or attempts[-1]["outcome"] == "invalid":
                raise ValueError("unrequested/invalid native input")
            last = attempts[-1]
            if last["outcome"] != "complete" and len(attempts) < 2:
                raise ValueError("unfinished native input")
            if last["error"] == "budget_exhausted":
                raise ValueError("budget stopped; cannot seal")
            request = Request(**last["request"])
            observed: dict[int, tuple[float, ...]] = {}
            for attempt in attempts:
                if attempt["outcome"] not in ("complete", "incomplete"):
                    continue
                parsed = bars(request, base64.b64decode(attempt["body_base64"], validate=True))
                for at, value in parsed.items():
                    if at in observed and observed[at] != value:
                        raise ValueError("native retry values conflict; cannot select a winner")
                    observed[at] = value
            records.append(
                {
                    "key": key,
                    "request": last["request"],
                    "outcome": last["outcome"],
                    "receipt_sha256": transport.digest(
                        state._path(key, last["attempt"]).read_bytes()
                    ),
                    "body_sha256": last["body_sha256"],
                    "body_base64": last["body_base64"],
                }
            )
        return {
            "version": VERSION,
            "binding": state.sha,
            "contract": contract,
            "reserved_attempts_bytes": state.totals(),
            "records": records,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scope", "prepare", "fetch", "seal"))
    parser.add_argument("--events", type=Path, default=Path(EVENTS))
    parser.add_argument("--scope", type=Path, default=Path(SCOPE))
    parser.add_argument("--root", type=Path, default=Path("runtime/research/b3a"))
    args = parser.parse_args(argv)
    rows = load_events(args.events)
    if args.command == "scope":
        raw = json.dumps(
            [asdict(r) for r in requests_for(rows)], sort_keys=True, separators=(",", ":")
        ).encode()
        args.scope.parent.mkdir(parents=True, exist_ok=True)
        with args.scope.open("xb") as stream:
            stream.write(gzip.compress(raw, mtime=0))
        sys.stdout.write(
            json.dumps(
                {
                    "requests": len(requests_for(rows)),
                    "sha256": transport.digest(args.scope.read_bytes()),
                }
            )
            + "\n"
        )
        return 0
    contract = binding(args.events, args.scope)
    requests = frozen_requests(args.events, args.scope)
    if args.command == "prepare":
        transport.prepare(args.root, contract, requests, [r.key for r in requests])
    elif args.command == "fetch":
        with (
            run(args.root, contract) as state,
            httpx.Client(timeout=30, follow_redirects=False) as client,
        ):
            result = state.fetch("pilot", client)  # every frozen key, not a sampled pilot
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        result = sealed(args.root, contract, requests)
        sha = publish_once(args.root / "sealed.json", result)
        sys.stdout.write(json.dumps({"sha256": sha, "requests": len(requests)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
