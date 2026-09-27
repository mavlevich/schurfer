"""Archive MEXC USDT-perpetual klines before they roll off the public API.

MEXC serves 1-minute contract klines for only about the last 30 days (checked
2026-09-27: 2026-08-28 empty, 2026-08-30 present), while 5-minute bars go back further.
The source-venue early-detection research needs those bars, so this tool stores them
locally before they disappear. It only stores: no return is computed here.

- One gzip JSON-lines file per symbol and interval, written once (temp file, fsync, hard
  link that fails if the file exists), plus a manifest with a status per symbol
  (`complete`, `empty` when the symbol had no bar in the window, or `failed`), the row
  count, the first and last bar and the sha256 of every file.
- The window and interval are pinned by the first run; another window is refused. A
  rerun verifies every file against the manifest, skips verified symbols, retries failed
  ones, and exits non-zero while any symbol is still failed.
- The window end may not pass BLIND_END (2026-09-29T00:00Z): the HYP-012 v2 cohort is
  blind from then on for every venue, because pumps are shared across venues.
- Archiving is not analysis. Bars inside the unread HYP-012b holdout (weeks 36-39) are
  stored now and may only be analysed after the HYP-012c read has completed.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://contract.mexc.com"
BLIND_END = datetime(2026, 9, 29, tzinfo=UTC)
INTERVAL_SECONDS = {"Min1": 60, "Min5": 300}
MAX_BARS_PER_REQUEST = 2000
_CONCURRENCY = 16
_ATTEMPTS = 4
_HEADERS = {"User-Agent": "schurfer-research/1.0"}
_MIN_REQUEST_GAP_SECONDS = 0.11  # about 9 requests per second, under MEXC's 20 per 2 s


class _Throttle:
    def __init__(self, gap: float) -> None:
        self._gap = gap
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(loop.time(), self._next) + self._gap


_THROTTLE = _Throttle(_MIN_REQUEST_GAP_SECONDS)


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            await _THROTTLE.wait()
            response = await client.get(f"{BASE_URL}{path}", params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is not True:
                raise RuntimeError(f"{path} code={payload.get('code')}")
            return payload.get("data")
        except Exception:
            if attempt == _ATTEMPTS:
                raise
            await asyncio.sleep(1.5 * attempt)
    raise AssertionError("unreachable")


async def usdt_perpetuals(client: httpx.AsyncClient) -> list[str]:
    data = await _get(client, "/api/v1/contract/detail", {})
    return sorted(
        str(c["symbol"])
        for c in data or []
        if c.get("quoteCoin") == "USDT" and c.get("settleCoin") == "USDT"
    )


def bars_from_payload(data: dict[str, Any] | None) -> list[dict[str, float | int]]:
    """MEXC returns parallel arrays; time is in seconds."""
    if not data:
        return []
    times = data.get("time") or []
    return [
        {
            "t": int(times[i]),
            "o": float(data["open"][i]),
            "h": float(data["high"][i]),
            "l": float(data["low"][i]),
            "c": float(data["close"][i]),
            "v": float(data["vol"][i]),
            "a": float(data["amount"][i]),
        }
        for i in range(len(times))
    ]


async def fetch_symbol(
    client: httpx.AsyncClient, symbol: str, interval: str, start: int, end: int
) -> list[dict[str, float | int]]:
    """Every bar with start <= t < end, deduplicated and ordered."""
    step = INTERVAL_SECONDS[interval] * MAX_BARS_PER_REQUEST
    bars: dict[int, dict[str, float | int]] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + step, end)
        data = await _get(
            client,
            f"/api/v1/contract/kline/{symbol}",
            {"interval": interval, "start": cursor, "end": chunk_end},
        )
        for bar in bars_from_payload(data):
            if start <= int(bar["t"]) < end:
                bars[int(bar["t"])] = bar
        cursor = chunk_end
    return [bars[t] for t in sorted(bars)]


def _body(rows: list[dict[str, float | int]]) -> bytes:
    return gzip.compress(
        b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in rows), mtime=0
    )


def write_once(path: Path, rows: list[dict[str, float | int]]) -> str:
    """Write-once: temp file, fsync, then a hard link that fails if the file exists."""
    body = _body(rows)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return hashlib.sha256(body).hexdigest()


def read_rows(path: Path) -> tuple[list[dict[str, float]], str]:
    body = path.read_bytes()
    rows = [json.loads(line) for line in gzip.decompress(body).splitlines()]
    return rows, hashlib.sha256(body).hexdigest()


def entry_for(rows: list[dict[str, Any]], digest: str, start: int, end: int) -> dict[str, Any]:
    """`complete` (bars, all inside the window), `empty` (no bar: not listed then), or
    `failed` when a bar falls outside the window."""
    first = rows[0]["t"] if rows else None
    last = rows[-1]["t"] if rows else None
    if not rows:
        status = "empty"
    elif start <= int(rows[0]["t"]) and int(rows[-1]["t"]) < end:
        status = "complete"
    else:
        status = "failed"
    return {"status": status, "rows": len(rows), "first": first, "last": last, "sha256": digest}


class ArchiveMismatchError(RuntimeError):
    """The archive on disk does not match its manifest; nothing is overwritten."""


async def archive(out_dir: Path, interval: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Archive every USDT perpetual for one pinned window. The window and interval are
    fixed by the first run; a run with another window is refused. A symbol is skipped
    only when its file exists and matches the manifest sha256; a mismatch refuses the
    run. Failed symbols are retried on the next run."""
    if end > BLIND_END:
        raise ValueError(f"refusing to archive past {BLIND_END.isoformat()} (HYP-012 v2 blind)")
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unknown interval {interval!r}")
    lo, hi = int(start.timestamp()), int(end.timestamp())
    target = out_dir / interval
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / "manifest.json"
    window = [start.isoformat(), end.isoformat()]
    if manifest_path.exists():
        manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        if manifest.get("interval") != interval or manifest.get("window") != window:
            raise ValueError(
                f"{target} is pinned to {manifest.get('interval')} {manifest.get('window')}; "
                f"refusing {interval} {window}"
            )
    else:
        manifest = {
            "interval": interval,
            "window": window,
            "source": f"{BASE_URL}/api/v1/contract/kline",
            "symbols": {},
        }
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    def save_manifest() -> None:
        manifest["failures"] = sorted(
            s for s, v in manifest["symbols"].items() if v.get("status") == "failed"
        )
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        tmp = manifest_path.with_name(".manifest.json.tmp")
        tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        tmp.replace(manifest_path)

    # Verify every archived file against the manifest before trusting it. Files written
    # by a run that died before its manifest update are adopted with their own status;
    # entries from an older manifest without a status get one from their rows.
    mismatched = []
    for path in sorted(target.glob("*.jsonl.gz")):
        symbol = path.name.removesuffix(".jsonl.gz")
        rows, digest = read_rows(path)
        known = manifest["symbols"].get(symbol)
        if known is not None and known.get("sha256") != digest:
            mismatched.append(symbol)
            continue
        if known is None or "status" not in known:
            manifest["symbols"][symbol] = entry_for(rows, digest, lo, hi)
    if mismatched:
        raise ArchiveMismatchError(f"files differ from the manifest: {mismatched[:10]}")
    for symbol, known in list(manifest["symbols"].items()):
        if (
            known.get("status") in ("complete", "empty")
            and not (target / f"{symbol}.jsonl.gz").exists()
        ):
            raise ArchiveMismatchError(f"{symbol} is in the manifest but its file is missing")
    save_manifest()

    async with httpx.AsyncClient(timeout=30, headers=_HEADERS) as client:
        symbols = await usdt_perpetuals(client)

        async def one(symbol: str) -> None:
            known = manifest["symbols"].get(symbol)
            if known is not None and known.get("status") in ("complete", "empty"):
                return
            path = target / f"{symbol}.jsonl.gz"
            try:
                async with semaphore:
                    rows = await fetch_symbol(client, symbol, interval, lo, hi)
                if path.exists():  # a failed entry whose file exists: never overwrite
                    raise ArchiveMismatchError(f"{path} exists for a failed symbol")
                digest = write_once(path, rows)
                manifest["symbols"][symbol] = entry_for(rows, digest, lo, hi)
            except ArchiveMismatchError:
                raise
            except Exception as exc:
                manifest["symbols"][symbol] = {"status": "failed", "error": type(exc).__name__}
            save_manifest()

        await asyncio.gather(*(one(s) for s in symbols))
    save_manifest()
    statuses = [v["status"] for v in manifest["symbols"].values()]
    return {
        "listed_now": len(symbols),
        "complete": statuses.count("complete"),
        "empty": statuses.count("empty"),
        "failed": statuses.count("failed"),
        "rows": sum(v.get("rows", 0) for v in manifest["symbols"].values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--interval", choices=sorted(INTERVAL_SECONDS), required=True)
    parser.add_argument("--start", required=True, help="UTC ISO instant, inclusive")
    parser.add_argument("--end", required=True, help="UTC ISO instant, exclusive")
    args = parser.parse_args()
    summary = asyncio.run(
        archive(
            args.out_dir,
            args.interval,
            datetime.fromisoformat(args.start).astimezone(UTC),
            datetime.fromisoformat(args.end).astimezone(UTC),
        )
    )
    sys.stdout.write(json.dumps(summary) + "\n")
    if summary["failed"]:
        raise SystemExit(f"{summary['failed']} symbols failed; rerun to retry them")


if __name__ == "__main__":
    main()
