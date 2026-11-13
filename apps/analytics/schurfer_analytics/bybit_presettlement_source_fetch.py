"""B2 source-quality pilot: fixed 24 native files, bounded durable transport only."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring_tape import Budget, ledger_spent
from .bybit_post_funding_screen import verified
from .bybit_presettlement_book_scope import END, FIRST, day, urls
from .bybit_presettlement_bounded_fetch import clean_revision, fsync_dir

PLAN_SHA = "39c5bda6f438f51e90fbb899a2eb93250acf995727d032a6e725f5a011fe8630"
VERSION = "b2_source_pilot_fetch_v1"
CHUNK = 64 * 1024
CAP = 2 * 1024**3


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [*plan["original_pilot_rows"], *plan["additional_format_fixtures"]]
    out = []
    for row in rows:
        if not day(FIRST) <= row["day"] < day(END):
            raise ValueError("source day outside discovery")
        expected = urls(row["symbol"], row["day"])
        for kind, prefix in ((row["book_kind"], "book"), ("trades", "trade")):
            url = row[f"{prefix}_url"]
            if url != expected[kind]:
                raise ValueError("native request identity differs")
            out.append(
                {
                    "symbol": row["symbol"],
                    "day": row["day"],
                    "kind": kind,
                    "url": url,
                    "bytes": row[f"{prefix}_bytes"],
                    "etag": row[f"{prefix}_etag"],
                    "id": hashlib.sha256(url.encode()).hexdigest(),
                }
            )
    if len({r["id"] for r in out}) != len(out):
        raise ValueError("duplicate native source file")
    return out


def prepare(root: Path, contract: dict[str, Any]) -> None:
    if root.exists():
        return
    root.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{root.name}.prepare-", dir=root.parent))
    try:
        for folder in ("attempts", "headers", "parts", "bodies", "completed", "errors"):
            (temp / folder).mkdir()
        with (temp / "budget.jsonl").open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        publish_once(temp / "run.json", contract)
        fsync_dir(temp)
        temp.rename(root)
        fsync_dir(root.parent)
    finally:
        if temp.exists():
            shutil.rmtree(temp)  # our unpublished staging directory only


def fetch(root: Path, contract: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    prepare(root, contract)
    if json.loads((root / "run.json").read_bytes()) != contract:
        raise ValueError("source run binding differs")
    ledger = root / "budget.jsonl"
    if not ledger.is_file():
        raise ValueError("missing budget ledger; cannot reset it")
    binding = file_sha(root / "run.json")
    todo = {row["id"]: row for row in contract["tasks"]}
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempts: dict[str, int] = dict.fromkeys(todo, 0)
        names: set[str] = set()
        last_at = 0.0
        for path in sorted((root / "attempts").glob("*.json")):
            row = json.loads(path.read_bytes())
            if row["binding"] != binding or row["id"] not in todo:
                raise ValueError("attempt outside bound run")
            attempts[row["id"]] += 1
            names.add(path.stem)
            last_at = max(last_at, row["started_at"])
        if sum(attempts.values()) > 48 or any(n > 2 for n in attempts.values()):
            raise ValueError("attempt ledger exceeds fixed limits")
        reservations: set[str] = set()
        for line in ledger.read_text().splitlines():
            entry = json.loads(line)
            name = entry["what"]
            key, attempt = name.rsplit("-", 1)
            if name not in names or attempt not in ("1", "2") or name in reservations:
                raise ValueError("byte reservation outside attempt ledger")
            expected = ((todo[key]["bytes"] + CHUNK - 1) // CHUNK + 1) * CHUNK
            if entry["bytes"] != expected:
                raise ValueError("invalid byte reservation")
            reservations.add(name)
        for path in (root / "parts").glob("*.partial"):
            if path.stem not in reservations:
                raise ValueError("partial body without byte reservation")
        for path in (root / "bodies").iterdir():
            key = path.name.split(".", 1)[0]
            if not any(f"{key}-{n}" in reservations for n in (1, 2)):
                raise ValueError("native body without byte reservation")
        spent = ledger_spent(ledger)
        if spent > contract["byte_cap"]:
            raise ValueError("byte ledger exceeds cap")
        shared = multiprocessing.get_context("spawn").Value("q", spent)
        budget = Budget(contract["byte_cap"], shared, ledger)
        result: dict[str, Any] = {}
        for key, task in todo.items():
            suffix = ".csv.gz" if task["kind"] == "trades" else ".data.zip"
            target = root / "bodies" / f"{key}{suffix}"
            receipt = root / "completed" / f"{key}.json"
            if receipt.exists():
                record = json.loads(receipt.read_bytes())
                if (
                    record["binding"] != binding
                    or not target.is_file()
                    or target.stat().st_size != task["bytes"]
                    or file_sha(target) != record["sha256"]
                ):
                    raise ValueError("completed native file or receipt corrupt")
                result[key] = record
                continue
            if target.exists():
                # Publication happens only after a complete fsynced body. Its matching
                # headers were persisted before the body, so the commit gap is recoverable.
                headers = sorted((root / "headers").glob(f"{key}-*.json"))
                if not headers or target.stat().st_size != task["bytes"]:
                    raise ValueError("orphan native body cannot be verified")
                header = json.loads(headers[-1].read_bytes())
                if (
                    header["binding"] != binding
                    or header["etag"] != task["etag"]
                    or header["bytes"] != task["bytes"]
                ):
                    raise ValueError("orphan body's header binding differs")
                record = {
                    "binding": binding,
                    "task": task,
                    "path": str(target),
                    "sha256": file_sha(target),
                    "recovered_after_publication": True,
                }
                publish_once(receipt, record)
                result[key] = record
                continue
            while attempts[key] < 2:
                delay = last_at + 1 / 3 - time.time()
                if delay > 0:
                    time.sleep(delay)
                if sum(attempts.values()) >= 48:
                    raise RuntimeError("global attempt cap exhausted")
                attempts[key] += 1
                last_at = time.time()
                name = f"{key}-{attempts[key]}"
                publish_once(
                    root / "attempts" / f"{name}.json",
                    {"binding": binding, "id": key, "started_at": last_at},
                )
                part = root / "parts" / f"{name}.partial"
                try:
                    with client.stream(
                        "GET",
                        task["url"],
                        follow_redirects=False,
                        headers={"Accept-Encoding": "identity"},
                    ) as response:
                        if response.status_code != 200:
                            raise httpx.HTTPStatusError(
                                "source HTTP failure", request=response.request, response=response
                            )
                        length = response.headers.get("content-length")
                        etag = response.headers.get("etag")
                        if length != str(task["bytes"]) or etag != task["etag"]:
                            raise ValueError("source version/size differs from frozen HEAD")
                        publish_once(
                            root / "headers" / f"{name}.json",
                            {
                                "binding": binding,
                                "etag": etag,
                                "bytes": int(length),
                                "content_encoding": response.headers.get("content-encoding"),
                            },
                        )
                        # Whole-file reservation, rounded up plus an extra read guard.
                        # The conservative reservation survives failed/partial attempts.
                        budget.reserve(((task["bytes"] + CHUNK - 1) // CHUNK + 1) * CHUNK, name)
                        size = 0
                        with part.open("xb") as out:
                            for chunk in response.iter_raw(chunk_size=CHUNK):
                                size += len(chunk)
                                if size > task["bytes"]:
                                    raise ValueError("body exceeds frozen native size")
                                out.write(chunk)
                            out.flush()
                            os.fsync(out.fileno())
                        if size != task["bytes"]:
                            raise httpx.ReadError("incomplete native body")
                        os.link(part, target)
                        fsync_dir(target.parent)
                        record = {
                            "binding": binding,
                            "task": task,
                            "path": str(target),
                            "sha256": file_sha(target),
                            "recovered_after_publication": False,
                        }
                        publish_once(receipt, record)
                        result[key] = record
                    break
                except httpx.HTTPError:
                    publish_once(
                        root / "errors" / f"{name}.json", {"error": "transport_or_http_failure"}
                    )
            if key not in result:
                raise RuntimeError("source file incomplete after two attempts; no replacement")
        manifest = {
            "version": VERSION,
            "binding": binding,
            "files": result,
            "complete": len(result),
            "attempts": sum(attempts.values()),
            "reserved_bytes": ledger_spent(ledger),
        }
        path = root / "manifest.json"
        if path.exists():
            if json.loads(path.read_bytes()) != manifest:
                raise ValueError("sealed source manifest differs")
        else:
            publish_once(path, manifest)
        return {k: v for k, v in manifest.items() if k != "files"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(verified(args.plan, PLAN_SHA).read_bytes())
    todo = tasks(plan)
    if len(todo) != 24 or sum(t["bytes"] for t in todo) != 1_555_848_890:
        raise ValueError("fixed pilot differs")
    args.run.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.run.parent).free < 6 * 1024**3:
        raise ValueError("at least 6 GiB free space is required")
    contract = {
        "version": VERSION,
        "revision": clean_revision(),
        "working_tree_dirty": False,
        "source_sha256": file_sha(Path(__file__)),
        "plan_sha256": PLAN_SHA,
        "byte_cap": CAP,
        "tasks": todo,
        "max_attempts": 48,
        "rate": 3,
    }
    with httpx.Client(timeout=120, trust_env=False) as client:
        result = fetch(args.run, contract, client)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
