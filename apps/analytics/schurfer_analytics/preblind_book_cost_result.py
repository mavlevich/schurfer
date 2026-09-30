"""One registered read of pre-blind quote costs, from a durable input snapshot."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from . import preblind_book_cost_baseline as baseline
from .reporting import json_ready, normalize_code_revision
from .source_lead_multi_source_report import complete_digest, load_verified, write_once

ARTIFACT_DIR = Path("/runtime/research/preblind-book-cost-baseline")
INPUTS_NAME = "inputs.json"
RESULT_NAME = "result.json"
_PAPER_TIMES = (
    "watch_decision_at",
    "entry_quote_observed_at",
    "exit_quote_observed_at",
    "entry_exchange_event_at",
    "exit_exchange_event_at",
)
_SOURCE_TIMES = ("source_first_observed_at", "observed_at")


def _query_sha256() -> str:
    queries = (baseline.PAPER_ROWS, baseline.SOURCE_ROWS, baseline.EXCLUDED_COUNTS)
    return hashlib.sha256("\n".join(map(str, queries)).encode()).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive timestamp in registered input")
        return value.isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unexpected registered input type: {type(value).__name__}")


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _json_value(value) for key, value in row.items()} for row in rows]


def _restore_rows(rows: Any, time_columns: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("registered input rows are not a list")
    restored: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("registered input row is not an object")
        item = dict(row)
        for key in time_columns:
            value = item.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"registered {key} is not an ISO timestamp")
                timestamp = datetime.fromisoformat(value)
                if timestamp.tzinfo is None:
                    raise ValueError(f"registered {key} lacks timezone")
                item[key] = timestamp
        restored.append(item)
    return restored


def _validate_inputs(inputs: dict[str, Any], *, revision: str | None = None) -> None:
    expected = {
        "reader_version": baseline.READER_VERSION,
        "window_start": baseline.WINDOW_START.isoformat(),
        "window_end": baseline.WINDOW_END.isoformat(),
        "query_sha256": _query_sha256(),
    }
    for field, value in expected.items():
        if inputs.get(field) != value:
            raise ValueError(f"registered input has an unexpected {field}")
    if revision is not None and inputs.get("code_revision") != revision:
        raise ValueError("open input snapshot pins another code revision")


def _result_from_inputs(inputs: dict[str, Any], inputs_sha256: str) -> dict[str, Any]:
    paper = _restore_rows(inputs["paper_rows"], _PAPER_TIMES)
    source = _restore_rows(inputs["source_rows"], _SOURCE_TIMES)
    excluded = inputs["excluded_counts"]
    if not isinstance(excluded, dict) or not all(
        isinstance(value, int) and value >= 0 for value in excluded.values()
    ):
        raise ValueError("invalid registered exclusion counts")
    report = baseline.summarize_cost_rows(paper, source, excluded)
    report.update(
        {
            "inputs_sha256": inputs_sha256,
            "query_sha256": inputs["query_sha256"],
            "code_revision": inputs["code_revision"],
            "working_tree_dirty": inputs["working_tree_dirty"],
            "database_read_at_utc": inputs["database_read_at_utc"],
            "paper_rows_read": len(paper),
            "source_rows_read": len(source),
        }
    )
    return report


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(json_ready(payload), indent=2, sort_keys=True).encode() + b"\n"


async def report_once(
    db_url: str,
    *,
    code_revision: str,
    working_tree_dirty: bool,
    artifact_dir: Path = ARTIFACT_DIR,
) -> tuple[dict[str, Any], str]:
    """Freeze one database read, then publish or verify one deterministic result."""
    revision = normalize_code_revision(code_revision)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with (artifact_dir / ".read.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inputs_path = artifact_dir / INPUTS_NAME
        result_path = artifact_dir / RESULT_NAME
        if result_path.exists() and (result_path.with_name(RESULT_NAME + ".sha256")).exists():
            if not inputs_path.exists():
                raise ValueError("completed result lacks its frozen inputs")
            complete_digest(inputs_path)
            inputs, inputs_sha = load_verified(inputs_path)
            _validate_inputs(inputs)
            report, result_sha = load_verified(result_path)
            if report.get("inputs_sha256") != inputs_sha:
                raise ValueError("result pins another input snapshot")
            return report, result_sha

        if inputs_path.exists():
            complete_digest(inputs_path)
            inputs, inputs_sha = load_verified(inputs_path)
            _validate_inputs(inputs, revision=revision)
            if inputs.get("working_tree_dirty") != working_tree_dirty:
                raise ValueError("open input snapshot pins another dirty-tree state")
        else:
            database_now, paper, source, excluded = await baseline.load_registered_rows(db_url)
            inputs = {
                "reader_version": baseline.READER_VERSION,
                "window_start": baseline.WINDOW_START.isoformat(),
                "window_end": baseline.WINDOW_END.isoformat(),
                "query_sha256": _query_sha256(),
                "code_revision": revision,
                "working_tree_dirty": working_tree_dirty,
                "database_read_at_utc": database_now.isoformat(),
                "artifact_prepared_at_utc": datetime.now(UTC).isoformat(),
                "paper_rows": _serialize_rows(paper),
                "source_rows": _serialize_rows(source),
                "excluded_counts": excluded,
            }
            inputs_sha = write_once(inputs_path, inputs)

        report = _result_from_inputs(inputs, inputs_sha)
        if result_path.exists():
            if result_path.read_bytes() != _body(report):
                raise ValueError("incomplete result differs from frozen-input replay")
            complete_digest(result_path)
        else:
            write_once(result_path, report)
        verified, result_sha = load_verified(result_path)
        return verified, result_sha


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--clean-tree", action="store_true")
    args = parser.parse_args()
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required")
    report, result_sha = asyncio.run(
        report_once(
            db_url,
            code_revision=args.code_revision,
            working_tree_dirty=not args.clean_tree,
        )
    )
    sys.stdout.write(
        json.dumps(
            {
                "artifact": str(ARTIFACT_DIR / RESULT_NAME),
                "sha256": result_sha,
                "paper_rows_read": report["paper_rows_read"],
                "source_rows_read": report["source_rows_read"],
                "groups": len(report["groups"]),
            },
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
