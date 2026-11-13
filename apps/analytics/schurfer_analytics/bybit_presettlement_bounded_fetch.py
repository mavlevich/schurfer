"""Bounded supplemental B1 premium transport; no prices, returns or outcome selection.

``prepare`` is offline. ``pilot`` and ``full`` require a separate owner fetch go.
The frozen 64-ID pilot and full run share one durable, non-resettable ledger.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import bybit_presettlement_feasibility as b1
from .bybit_carry_feasibility import publish_once
from .bybit_post_funding_screen import DAY_MS, MINUTE_MS, verified

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

RULES = "b1_prior_only_4h_bounded_transport_v1"
ENDPOINT = "https://api.bybit.com/v5/market/premium-index-price-kline"
SCOPE_SHA = "0b11db40c505452e05620c22d1aa9aa3038cd95c6ae3ce2e524f232d8168ab6a"
PILOT_SHA = "cab2d6d09ce986e1408b9ea298822a203d87ca20708fd28a8c7a9f3e71b82855"
CHUNK = 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Request:
    symbol: str
    start: int
    end: int

    @property
    def key(self) -> str:
        return digest(f"{self.symbol}:{self.start}:{self.end}".encode())

    @property
    def params(self) -> dict[str, str | int]:
        return {"category": "linear", "symbol": self.symbol, "interval": "1",
                "start": self.start, "end": self.end - 1, "limit": 1000}  # fmt: skip


@dataclass(frozen=True)
class Limits:
    attempts: int = 23_320
    body_bytes: int = 512 * 1024 * 1024
    pilot_attempts: int = 128
    pilot_bytes: int = 10 * 1024 * 1024
    per_id: int = 2
    spacing: float = 1 / 3


class BudgetExceededError(RuntimeError):
    """A durable cap stopped the run; restarting cannot replenish it."""


def frozen_requests(scope: Path, pilot: Path) -> tuple[list[Request], list[str]]:
    rows = json.loads(gzip.decompress(verified(scope, SCOPE_SHA).read_bytes()))
    sample = json.loads(gzip.decompress(verified(pilot, PILOT_SHA).read_bytes()))
    missing = [r for r in rows if not r["previously_requested_same_bounds"]]
    requests = [Request(r["symbol"], r["start_ms"], r["end_exclusive_ms"]) for r in missing]
    expected = sorted(
        requests,
        key=lambda r: digest(f"b1-minute-scope-repair-pilot-v1:{r.symbol}:{r.start}".encode()),
    )[:64]
    pilot_ids = [Request(r["symbol"], r["start_ms"], r["end_exclusive_ms"]).key for r in sample]
    if len(rows) != 19_845 or len(requests) != 11_660 or pilot_ids != [r.key for r in expected]:
        raise ValueError("frozen scope/pilot count or identity differs")
    for r in requests:
        if (
            not re.fullmatch(r"[A-Z0-9]+", r.symbol)
            or r.end - r.start != 240 * MINUTE_MS
            or r.start % MINUTE_MS
            or r.start < b1.FIRST_MS - DAY_MS
            or r.end > b1.LAST_MS
        ):
            raise ValueError("request outside the registered 4h discovery scope")
    if len({r.key for r in requests}) != len(requests):
        raise ValueError("duplicate request in scope")
    return sorted(requests, key=lambda r: r.key), pilot_ids


def clean_revision() -> str:
    git = shutil.which("git")
    if git is None:
        raise ValueError("git is required to bind a clean revision")
    # Fixed argv, no user-supplied executable or shell expansion.
    status = subprocess.check_output([git, "status", "--porcelain"], text=True)  # noqa: S603
    if status:
        raise ValueError("run only from a committed clean worktree")
    return subprocess.check_output([git, "rev-parse", "HEAD"], text=True).strip()  # noqa: S603


def binding(scope: Path, pilot: Path, index: Path, base: Path) -> dict[str, Any]:
    verified(index, b1.INDEX_SHA256)
    if b1.MINUTES_SHA256 is None:
        raise ValueError("original minute snapshot is not pinned")
    verified(base, b1.MINUTES_SHA256)
    return {"rules": RULES, "revision": clean_revision(), "endpoint": ENDPOINT,
            "transport_sha256": digest(Path(__file__).read_bytes()),
            "classifier_sha256": digest(Path(b1.__file__).read_bytes()),
            "index_sha256": b1.INDEX_SHA256, "base_sha256": b1.MINUTES_SHA256,
            "scope_sha256": digest(scope.read_bytes()), "pilot_sha256": digest(pilot.read_bytes()),
            "limits": asdict(Limits())}  # fmt: skip


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(
    root: Path, contract: dict[str, Any], requests: Sequence[Request], pilot: list[str]
) -> None:
    """Publish a complete offline run directory, never repair an incomplete existing one."""
    if len({r.key for r in requests}) != len(requests) or not set(pilot) <= {
        r.key for r in requests
    }:
        raise ValueError("invalid request/pilot identities")
    if not pilot or len(set(pilot)) != len(pilot):
        raise ValueError("empty or repeated pilot IDs")
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.prepare-", dir=root.parent))
    try:
        run = {"contract": contract, "requests": [asdict(r) for r in requests], "pilot": pilot}
        sha = publish_once(staging / "run.json", run)
        (staging / "attempts").mkdir()
        with contextlib.closing(sqlite3.connect(staging / "ledger.sqlite")) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE state (
                    binding TEXT PRIMARY KEY, last_request REAL NOT NULL,
                    total_attempts INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    pilot_attempts INTEGER NOT NULL DEFAULT 0,
                    pilot_bytes INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE attempts (
                    request TEXT NOT NULL, n INTEGER NOT NULL, phase TEXT NOT NULL,
                    reserved INTEGER NOT NULL DEFAULT 0, receipt_sha TEXT,
                    PRIMARY KEY(request,n));
            """)
            db.execute("INSERT INTO state(binding,last_request) VALUES (?,0)", (sha,))
        fsync_dir(staging)
        if root.exists():
            raise FileExistsError("run already exists; resume it without reinitializing")
        staging.rename(root)
        fsync_dir(root.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)  # only our unpublished initialization directory


def validate(r: Request, body: bytes, status: int, error: str | None) -> str:
    """Return complete/incomplete/retry/invalid; preserve native OHLC without repair."""
    if error == "unsupported_content_encoding":
        return "invalid"
    if error or not 200 <= status < 300:
        return "retry"
    try:
        data = json.loads(body)
        if data["retCode"] != 0:
            return "retry"
        result = data["result"]
        if result["symbol"] != r.symbol or result["category"] != "linear":
            return "invalid"
        values: dict[int, tuple[float, ...]] = {}
        rows = result["list"]
        if not isinstance(rows, list):
            return "invalid"
        for row in rows:
            if not isinstance(row, list) or len(row) != 5 or isinstance(row[0], bool):
                return "invalid"
            ts = int(row[0])
            if str(ts) != str(row[0]) or not r.start <= ts < r.end or ts % MINUTE_MS:
                return "invalid"
            if any(isinstance(v, bool) for v in row[1:]):
                return "invalid"
            ohlc = tuple(float(v) for v in row[1:])
            if not all(math.isfinite(v) for v in ohlc) or (ts in values and values[ts] != ohlc):
                return "invalid"
            values[ts] = ohlc
        expected = set(range(r.start, r.end, MINUTE_MS))
        return "complete" if values.keys() == expected else "incomplete"
    except (ValueError, TypeError, KeyError, OverflowError):
        return "invalid"


class Run:
    """Single-writer ledger. Each attempt and body-read reservation commits before I/O.

    Bytes are conservative 1 KiB reservations, including the final EOF probe. They
    are never refunded, even after a crash. This bounds bytes exposed by the HTTP
    body iterator before parsing, not kernel/socket buffers or HTTP headers.
    """

    def __init__(
        self,
        root: Path,
        contract: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        request_factory: Callable[..., Request] = Request,
        validator: Callable[[Request, bytes, int, str | None], str] = validate,
        endpoint: Callable[[Request], str] | None = None,
    ) -> None:
        self.root, self.now, self.sleep = root, now, sleep
        # B1 premium and B3 price/mark share the durable transport, not decoders.
        # Defaults preserve old request identities and receipts byte for byte.
        self.validator = validator
        self.endpoint = endpoint or (lambda _: ENDPOINT)
        run_path, ledger = root / "run.json", root / "ledger.sqlite"
        if not run_path.is_file() or not ledger.is_file():
            raise ValueError("missing run binding or ledger; do not reinitialize")
        raw = run_path.read_bytes()
        run = json.loads(raw)
        if run["contract"] != contract:
            raise ValueError("run binding differs; resume cannot change code, scope or limits")
        self.sha = digest(raw)
        self.requests = {r.key: r for row in run["requests"] for r in [request_factory(**row)]}
        if len(self.requests) != len(run["requests"]):
            raise ValueError("repeated request identity in run binding")
        self.pilot = run["pilot"]
        self.limits = Limits(**contract["limits"])
        self.lock = (root / "writer.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(ledger)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.row_factory = sqlite3.Row
            state = self.db.execute("SELECT * FROM state").fetchall()
            if len(state) != 1 or state[0]["binding"] != self.sha:
                raise ValueError("ledger binding differs")
            self.receipts: dict[str, list[dict[str, Any]]] = {k: [] for k in self.requests}
            self._recover()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    def close(self) -> None:
        self.db.close()
        self.lock.close()

    def __enter__(self) -> Run:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def totals(self, phase: str | None = None) -> tuple[int, int]:
        # Constant-cost counters; independently reconcile against every attempt on resume.
        query = (
            "SELECT pilot_attempts,pilot_bytes FROM state"
            if phase == "pilot"
            else "SELECT total_attempts,total_bytes FROM state"
        )
        row = self.db.execute(query).fetchone()
        return int(row[0]), int(row[1])

    def _path(self, key: str, n: int) -> Path:
        return self.root / "attempts" / f"{key}-{n}.json"

    def _recover(self) -> None:
        """Adopt an atomically saved receipt after a crash; abandon unsaved attempts."""
        rows = self.db.execute("SELECT * FROM attempts ORDER BY request,n").fetchall()
        known = {self._path(row["request"], row["n"]).name for row in rows}
        if {p.name for p in (self.root / "attempts").glob("*.json")} != {
            p for p in known if (self.root / "attempts" / p).exists()
        }:
            raise ValueError("receipt exists without a charged ledger attempt")
        for row in rows:
            key, n = row["request"], row["n"]
            if (
                key not in self.requests
                or n != len(self.receipts[key]) + 1
                or n > self.limits.per_id
                or row["reserved"] < 0
                or row["phase"] not in ("pilot", "full")
                or (row["phase"] == "pilot") != (key in self.pilot)
            ):
                raise ValueError("invalid ledger attempt")
            path = self._path(key, n)
            if path.exists():
                raw = path.read_bytes()
                sha = digest(raw)
                if row["receipt_sha"] not in (None, sha):
                    raise ValueError("saved receipt checksum differs")
                receipt = json.loads(raw)
                self._verify_receipt(receipt, row)
                if row["receipt_sha"]:
                    self.receipts[key].append(receipt)
                else:
                    self._commit_receipt(key, n, receipt, sha)
            elif row["receipt_sha"]:
                raise ValueError("saved receipt is missing")
            else:
                self._save(key, n, row["phase"], b"", 0, "interrupted")
        for phase, max_attempts, max_bytes in (
            (None, self.limits.attempts, self.limits.body_bytes),
            ("pilot", self.limits.pilot_attempts, self.limits.pilot_bytes),
        ):
            a, b = self.totals(phase)
            selected = [r for r in rows if phase is None or r["phase"] == phase]
            if (a, b) != (len(selected), sum(r["reserved"] for r in selected)):
                raise ValueError("ledger counters disagree with charged attempts")
            if a > max_attempts or b > max_bytes or a < 0 or b < 0:
                raise ValueError("ledger exceeds the immutable budget")

    def _verify_receipt(self, receipt: dict[str, Any], row: sqlite3.Row) -> None:
        r = self.requests[row["request"]]
        body = base64.b64decode(receipt["body_base64"], validate=True)
        if (
            receipt["binding"] != self.sha
            or receipt["request"] != asdict(r)
            or receipt["attempt"] != row["n"]
            or receipt["phase"] != row["phase"]
            or digest(body) != receipt["body_sha256"]
            or len(body) > row["reserved"]
            or receipt["outcome"]
            != self.validator(r, body, receipt["http_status"], receipt["error"])
        ):
            raise ValueError("receipt identity, bytes or validation differs")

    def _commit_receipt(self, key: str, n: int, receipt: dict[str, Any], sha: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE attempts SET receipt_sha=? WHERE request=? AND n=?", (sha, key, n)
            )
        self.receipts[key].append(receipt)

    def _save(
        self, key: str, n: int, phase: str, body: bytes, status: int, error: str | None
    ) -> str:
        receipt = {
            "binding": self.sha,
            "request": asdict(self.requests[key]),
            "attempt": n,
            "phase": phase,
            "http_status": status,
            "error": error,
            "body_base64": base64.b64encode(body).decode(),
            "body_sha256": digest(body),
            "outcome": self.validator(self.requests[key], body, status, error),
        }
        sha = publish_once(self._path(key, n), receipt)
        self._commit_receipt(key, n, receipt, sha)
        return str(receipt["outcome"])

    def _reserve(self, key: str, n: int, phase: str) -> None:
        with self.db:
            for p, limit in ((None, self.limits.body_bytes), ("pilot", self.limits.pilot_bytes)):
                if (p is None or phase == p) and self.totals(p)[1] + CHUNK > limit:
                    raise BudgetExceededError("response-body reservation cap exhausted")
            self.db.execute(
                "UPDATE attempts SET reserved=reserved+? WHERE request=? AND n=?", (CHUNK, key, n)
            )
            self.db.execute(
                "UPDATE state SET total_bytes=total_bytes+?,pilot_bytes=pilot_bytes+?",
                (CHUNK, CHUNK if phase == "pilot" else 0),
            )

    def _start(self, key: str, phase: str) -> int:
        last = float(self.db.execute("SELECT last_request FROM state").fetchone()[0])
        delay = last + self.limits.spacing - self.now()
        if delay > 0:
            self.sleep(delay)
        # Durable charge BEFORE client.stream opens a connection.
        with self.db:
            if self.totals()[0] >= self.limits.attempts or (
                phase == "pilot" and self.totals("pilot")[0] >= self.limits.pilot_attempts
            ):
                raise BudgetExceededError("HTTP attempt cap exhausted")
            if self.totals()[1] + CHUNK > self.limits.body_bytes or (
                phase == "pilot" and self.totals("pilot")[1] + CHUNK > self.limits.pilot_bytes
            ):
                raise BudgetExceededError("response-body reservation cap exhausted")
            n = len(self.receipts[key]) + 1
            if n > self.limits.per_id:
                raise BudgetExceededError("per-ID attempt cap exhausted")
            self.db.execute("INSERT INTO attempts(request,n,phase) VALUES (?,?,?)", (key, n, phase))
            self.db.execute(
                "UPDATE state SET last_request=?,total_attempts=total_attempts+1,"
                "pilot_attempts=pilot_attempts+?",
                (self.now(), int(phase == "pilot")),
            )
        return n

    def _one(self, key: str, phase: str, client: httpx.Client) -> str:
        n = self._start(key, phase)
        body, status, error = bytearray(), 0, None
        try:
            with client.stream(
                "GET",
                self.endpoint(self.requests[key]),
                params=self.requests[key].params,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                if response.headers.get("content-encoding", "identity") != "identity":
                    error = "unsupported_content_encoding"
                else:
                    chunks = response.iter_raw(chunk_size=CHUNK)
                    while True:
                        self._reserve(key, n, phase)
                        chunk = next(chunks, None)
                        if chunk is None:
                            break
                        body.extend(chunk)
        except BudgetExceededError:
            self._save(key, n, phase, bytes(body), status, "budget_exhausted")
            raise
        except httpx.HTTPError:
            error = "transport_error"  # never persist a URL, credentials or exception body
        return self._save(key, n, phase, bytes(body), status, error)

    def _summary(self, phase: str) -> dict[str, Any]:
        keys = self.pilot if phase == "pilot" else list(self.requests)
        counts: dict[str, int] = {}
        terminal = 0
        for key in keys:
            values = self.receipts[key]
            outcome = values[-1]["outcome"] if values else "not_requested"
            counts[outcome] = counts.get(outcome, 0) + 1
            terminal += outcome in ("complete", "invalid") or len(values) >= self.limits.per_id
        complete = counts.get("complete", 0)
        return {
            "binding": self.sha,
            "phase": phase,
            "fixed_intervals": len(keys),
            "counts": counts,
            "complete": complete,
            "terminal_intervals": terminal,
            "passed": terminal == len(keys)
            and complete / len(keys) >= 0.95
            and not counts.get("invalid", 0),
            "attempts_and_reserved_bytes": self.totals(),
            "pilot_attempts_and_reserved_bytes": self.totals("pilot"),
        }

    def fetch(self, phase: str, client: httpx.Client) -> dict[str, Any]:
        if phase not in ("pilot", "full"):
            raise ValueError("unknown fetch phase")
        pilot_path = self.root / "pilot.json"
        if phase == "full":
            if not pilot_path.exists():
                raise ValueError("the fixed pilot must be completed first")
            pilot = json.loads(pilot_path.read_bytes())
            if (
                pilot["binding"] != self.sha
                or not pilot["passed"]
                or not self._summary("pilot")["passed"]
            ):
                raise ValueError("fixed pilot failed; full run is blocked")
        elif pilot_path.exists():
            saved: dict[str, Any] = json.loads(pilot_path.read_bytes())
            if saved["binding"] != self.sha or saved["counts"] != self._summary("pilot")["counts"]:
                raise ValueError("saved pilot differs from its receipts")
            return saved  # immutable failure/pass; no resampling
        keys = self.pilot if phase == "pilot" else list(self.requests)
        for key in keys:
            while len(self.receipts[key]) < self.limits.per_id:
                if self.receipts[key] and self.receipts[key][-1]["outcome"] in (
                    "complete",
                    "invalid",
                ):
                    break
                outcome = self._one(key, "pilot" if key in self.pilot else "full", client)
                if outcome == "invalid":
                    break
            if self.receipts[key] and self.receipts[key][-1]["outcome"] == "invalid":
                break  # explicit fatal integrity failure, never silently skip a bad response
        result = self._summary(phase)
        if phase == "pilot":
            publish_once(pilot_path, result)
        return result

    def _records(self) -> Iterator[dict[str, Any]]:
        yield {"kind": "version", "symbol": "", "params": {}, "response": {"version": b1.VERSION}}
        for key in sorted(self.requests):
            r, attempts = self.requests[key], self.receipts[key]
            if not attempts or attempts[-1]["outcome"] == "invalid":
                raise ValueError("incomplete run or invalid response; cannot seal")
            receipt = attempts[-1]
            if receipt["outcome"] != "complete" and len(attempts) < self.limits.per_id:
                raise ValueError("nonterminal request; cannot seal")
            if receipt["error"] == "budget_exhausted":
                raise ValueError("budget stopped the run; cannot seal")
            if receipt["outcome"] in ("complete", "incomplete"):
                yield {
                    "kind": "premium_1",
                    "symbol": r.symbol,
                    "params": r.params,
                    "response": json.loads(base64.b64decode(receipt["body_base64"])),
                }
            if receipt["outcome"] != "complete":
                yield {
                    "kind": "fetch_error",
                    "symbol": r.symbol,
                    "params": {"s1": r.start},
                    "response": {"error": receipt["outcome"]},
                }

    def seal(self) -> dict[str, Any]:
        """Stream deterministic gzip without materializing millions of native bars."""
        pilot_path = self.root / "pilot.json"
        if (
            not pilot_path.exists()
            or not json.loads(pilot_path.read_bytes())["passed"]
            or (not self._summary("pilot")["passed"])
        ):
            raise ValueError("pilot has not passed")
        target = self.root / "supplement.jsonl.gz"
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=self.root)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as gz:
                    for row in self._records():
                        line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                        gz.write(line.encode())
                stream.flush()
                os.fsync(stream.fileno())
            sha = digest(temp.read_bytes())
            if target.exists():
                if digest(target.read_bytes()) != sha:
                    raise ValueError("sealed supplement differs")
            else:
                os.link(temp, target)
                fsync_dir(self.root)
        finally:
            temp.unlink()
        result = {
            **self._summary("full"),
            "supplement_sha256": sha,
            "receipt_checksums": dict(
                self.db.execute(
                    "SELECT request || '-' || n,receipt_sha FROM attempts ORDER BY request,n"
                )
            ),
        }
        manifest = self.root / "manifest.json"
        if manifest.exists():
            if json.loads(manifest.read_bytes()) != json.loads(json.dumps(result)):
                raise ValueError("sealed manifest differs")
        else:
            publish_once(manifest, result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "pilot", "full", "seal", "status"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--base-minutes", type=Path, required=True)
    args = parser.parse_args()
    requests, pilot = frozen_requests(args.scope, args.pilot)
    contract = binding(args.scope, args.pilot, args.index, args.base_minutes)
    if args.action == "prepare":
        prepare(args.run, contract, requests, pilot)
        result: dict[str, Any] = {"prepared": True, "requests": len(requests), "pilot": len(pilot)}
    else:
        with Run(args.run, contract) as run:
            if [asdict(r) for r in run.requests.values()] != [
                asdict(r) for r in requests
            ] or run.pilot != pilot:
                raise ValueError("run request IDs differ from the frozen scope")
            if args.action in ("pilot", "full"):
                with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                    result = run.fetch(args.action, client)
            elif args.action == "seal":
                result = run.seal()
            else:
                result = run._summary("full")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
