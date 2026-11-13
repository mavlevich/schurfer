"""B2 archive scope and HEAD inventory only: never downloads a book/trade body."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import Pacer
from .bybit_post_funding_screen import DAY_MS, verified
from .bybit_presettlement_bounded_fetch import clean_revision

if TYPE_CHECKING:
    from collections.abc import Mapping

VERSION = "b2_presettlement_archive_metadata_v1"
EVENTS_SHA = "4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7"
REPORT_SHA = "96ed96d58514815203aa221a35bce5ed19a37b9b5316947148375a033170311e"
FIRST = 1_727_740_800_000  # 2024-10-01 UTC
END = 1_759_276_800_000  # 2025-10-01 UTC, exclusive
MAX_ATTEMPTS_PER_URL = 2
REQUESTS_PER_SECOND = 3.0
MAX_WAIT_MS = 300_000
ARRIVAL_MS = 1_000
PRE_ROLL_MS = 60_000


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def urls(symbol: str, date: str) -> dict[str, str]:
    return {
        "ob500": f"https://quote-saver.bycsi.com/orderbook/linear/{symbol}/{date}_{symbol}_ob500.data.zip",
        "ob200": f"https://quote-saver.bycsi.com/orderbook/linear/{symbol}/{date}_{symbol}_ob200.data.zip",
        "trades": f"https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz",
    }


def scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Use every accepted offset; reject identity drift and out-of-window expansion."""
    if payload["report_sha256"] != REPORT_SHA:
        raise ValueError("event list is not bound to the accepted B1 report")
    groups = payload["events_by_decision"]
    if set(groups) != {"d30", "d15", "d10"}:
        raise ValueError("decision offsets differ")
    days: set[tuple[str, str]] = set()
    identities: set[tuple[str, int, str]] = set()
    for name, rows in groups.items():
        d = int(name[1:])
        for row in rows:
            symbol, decision = row["symbol"], row["decision_ms"]
            if (
                not re.fullmatch(r"[A-Z0-9]+", symbol)
                or row["interval_minutes"] != 240
                or decision != row["forecast_settlement_ms"] - d * 60_000
                or row["premium_observed_until_ms"] != decision - 60_000
                or not decision - PRE_ROLL_MS >= FIRST
                or decision + ARRIVAL_MS + MAX_WAIT_MS >= END
            ):
                raise ValueError("invalid or out-of-window event")
            identity = (symbol, row["interval_start_ms"], name)
            if identity in identities:
                raise ValueError("duplicate event identity")
            identities.add(identity)
            # Explicitly cover rollover, even though the frozen hourly grid has none.
            for date_ms in range(
                (decision - PRE_ROLL_MS) // DAY_MS,
                (decision + ARRIVAL_MS + MAX_WAIT_MS) // DAY_MS + 1,
            ):
                days.add((symbol, day(date_ms * DAY_MS)))
    tasks = [
        {"symbol": symbol, "day": date, "kind": kind, "url": url}
        for symbol, date in sorted(days)
        for kind, url in sorted(urls(symbol, date).items())
    ]
    return {
        "version": VERSION,
        "events_sha256": EVENTS_SHA,
        "report_sha256": REPORT_SHA,
        "arrival_ms": ARRIVAL_MS,
        "maximum_wait_ms": MAX_WAIT_MS,
        "pre_roll_ms": PRE_ROLL_MS,
        "events_by_offset": {name: len(rows) for name, rows in groups.items()},
        "symbol_days": len(days),
        "symbols": len({s for s, _ in days}),
        "tasks": tasks,
        "method": "HEAD",
        "body_download_allowed": False,
        "requests_per_second": REQUESTS_PER_SECOND,
        "max_attempts": len(tasks) * MAX_ATTEMPTS_PER_URL,
    }


def response_record(url: str, response: httpx.Response) -> dict[str, Any]:
    headers = {
        name: response.headers[name]
        for name in ("content-length", "content-type", "etag", "last-modified", "accept-ranges")
        if name in response.headers
    }
    length = headers.get("content-length", "")
    size = int(length) if length.isdecimal() and int(length) > 0 else None
    status = response.status_code
    error_document = headers.get("content-type", "").split(";")[0].strip().lower() in (
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/xml",
        "text/xml",
    )
    available = status == 200 and not error_document
    return {
        "url": url,
        "method": "HEAD",
        "status": status,
        "headers": headers,
        "size_bytes": size if available else None,
        "body_read": False,
        "state": "available" if available else "not_found" if status == 404 else "unknown",
    }


def inventory(plan: Mapping[str, Any], responses: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    def record(url: str) -> dict[str, Any]:
        return responses.get(sha(url.encode()), {"state": "unknown", "size_bytes": None})

    pairs = sorted({(row["symbol"], row["day"]) for row in plan["tasks"]})
    books = trades = 0
    unknown_size = missing_books = missing_trades = 0
    selected: Counter[str] = Counter()
    for symbol, date in pairs:
        targets = urls(symbol, date)
        options = [(kind, record(targets[kind])) for kind in ("ob500", "ob200")]
        chosen = next(((kind, r) for kind, r in options if r["state"] == "available"), None)
        if chosen is None:
            missing_books += 1
        else:
            kind, row = chosen
            selected[kind] += 1
            if row["size_bytes"] is None:
                unknown_size += 1
            else:
                books += row["size_bytes"]
        trade = record(targets["trades"])
        if trade["state"] != "available":
            missing_trades += 1
        elif trade["size_bytes"] is None:
            unknown_size += 1
        else:
            trades += trade["size_bytes"]
    return {
        "version": VERSION,
        "symbol_days": len(pairs),
        "selected_book_depths": dict(selected),
        "book_bytes_known": books,
        "trade_bytes_known": trades,
        "combined_bytes_known": books + trades,
        "selected_files_unknown_size": unknown_size,
        "symbol_days_without_verified_book": missing_books,
        "symbol_days_without_verified_trades": missing_trades,
        "complete_size_inventory": not (unknown_size or missing_books or missing_trades),
        "head_states": dict(Counter(r["state"] for r in responses.values())),
        "book_selection": "verified ob500 preferred, then ob200; both HEAD responses retained",
        "not_a_fill_or_profitability_result": True,
    }


async def probe(
    plan: dict[str, Any], root: Path, client: httpx.AsyncClient, revision: str
) -> dict[str, Any]:
    """Persist every attempt before HTTP; resuming never repeats completed HEADs."""
    contract = {
        "plan": plan,
        "revision": revision,
        "source_sha256": sha(Path(__file__).read_bytes()),
    }
    run_path = root / "run.json"
    if root.exists():
        if not run_path.is_file() or not (root / "attempts").is_dir():
            raise ValueError("incomplete run initialization; do not reset the directory")
        if json.loads(run_path.read_bytes()) != contract:
            raise ValueError("resume binding differs")
    else:
        root.mkdir(parents=True)
        (root / "attempts").mkdir()
        (root / "responses").mkdir()
        publish_once(run_path, contract)
    binding_sha = sha(run_path.read_bytes())
    known = {sha(t["url"].encode()): t["url"] for t in plan["tasks"]}
    starts: dict[str, int] = Counter()
    last_start = 0.0
    responses: dict[str, dict[str, Any]] = {}
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempts = sorted((root / "attempts").glob("*.json"))
        for p in attempts:
            row = json.loads(p.read_bytes())
            key = row["key"]
            if row["binding"] != binding_sha or known.get(key) != row["url"]:
                raise ValueError("attempt outside bound scope")
            starts[key] += 1
            if starts[key] > MAX_ATTEMPTS_PER_URL:
                raise ValueError("per-URL attempt budget exceeded")
            last_start = max(last_start, row["started_at"])
            saved = root / "responses" / p.name
            if saved.exists():
                response = json.loads(saved.read_bytes())
                if response["binding"] != binding_sha or response["url"] != row["url"]:
                    raise ValueError("saved response binding differs")
                responses[key] = response
        if len(attempts) > plan["max_attempts"]:
            raise ValueError("global attempt budget exceeded")
        pacer = Pacer(REQUESTS_PER_SECOND)
        pacer.next_at = time.monotonic() + max(
            0, last_start + 1 / REQUESTS_PER_SECOND - time.time()
        )
        gate = asyncio.Semaphore(4)
        used = len(attempts)

        async def one(task: dict[str, Any]) -> None:
            nonlocal used
            url = task["url"]
            key = sha(url.encode())
            async with gate:
                while starts[key] < MAX_ATTEMPTS_PER_URL:
                    old = responses.get(key)
                    if old and old.get("status") not in (None, 429) and old["status"] < 500:
                        return
                    await pacer.wait()
                    if used >= plan["max_attempts"]:
                        raise RuntimeError("HEAD attempt budget exhausted")
                    starts[key] += 1
                    used += 1
                    name = f"{key}-{starts[key]}.json"
                    publish_once(
                        root / "attempts" / name,
                        {
                            "binding": binding_sha,
                            "key": key,
                            "url": url,
                            "started_at": time.time(),
                            "attempt": starts[key],
                        },
                    )
                    try:
                        async with client.stream("HEAD", url, follow_redirects=False) as response:
                            row = response_record(url, response)
                    except httpx.HTTPError:
                        row = {
                            "url": url,
                            "method": "HEAD",
                            "status": None,
                            "state": "unknown",
                            "size_bytes": None,
                            "body_read": False,
                            "error": "transport_error",
                        }
                    row["binding"] = binding_sha
                    publish_once(root / "responses" / name, row)
                    responses[key] = row

        await asyncio.gather(*(one(task) for task in plan["tasks"]))
        result = inventory(plan, responses) | {"attempts": used, "binding_sha256": binding_sha}
        output = root / "inventory.json"
        if output.exists():
            if json.loads(output.read_bytes()) != result:
                raise ValueError("sealed metadata inventory differs")
        else:
            publish_once(output, result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "probe"))
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(gzip.decompress(verified(args.events, EVENTS_SHA).read_bytes()))
    plan = scope(payload)
    if args.action == "plan":
        publish_once(args.output_dir / "scope.json", plan)
        result = {k: v for k, v in plan.items() if k != "tasks"}
    else:
        revision = clean_revision()

        async def run() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                return await probe(plan, args.output_dir, client, revision)

        result = asyncio.run(run())
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
