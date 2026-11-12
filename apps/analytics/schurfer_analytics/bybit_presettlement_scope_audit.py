"""Prove the prior-only superset contains every synthetic-trigger classifier admission.

Uses only the pinned discovery index and synthetic premium closes. No market request,
real premium, perpetual price or strategy return is read. Run as a module from a clean
checkout; every forecast and every registered d goes through the real classify().
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from . import bybit_presettlement_feasibility as b1
from .bybit_post_funding_screen import Index, load_index, verified

VERSION = "b1_classify_scope_containment_v1"
_INDEX: Index | None = None
_ROWS: dict[str, list[b1.Interval]] = {}
_KEYS: set[tuple[str, int]] = set()


class SyntheticCloses(Mapping[int, float]):
    """A constant synthetic close at every queried minute; no real series exists."""

    def __getitem__(self, _key: int) -> float:
        return -0.0045

    def __iter__(self) -> Iterator[int]:
        raise TypeError("synthetic closes support point queries only")

    def __len__(self) -> int:
        raise TypeError("synthetic closes support point queries only")


def _audit_symbol(symbol: str) -> dict[str, Any]:
    if _INDEX is None:
        raise RuntimeError("audit not initialized")
    times = sorted(_INDEX.instruments[symbol].funding)
    closes = SyntheticCloses()
    statuses: Counter[str] = Counter()
    admitted: Counter[str] = Counter()
    missing = []
    count = 0
    for iv in _ROWS[symbol]:
        for d in b1.DECISION_OFFSETS_MIN:
            label, _ = b1.classify(_INDEX, iv, closes, d, times)
            count += 1
            statuses[label] += 1
            if label == "admitted":
                admitted[f"d{d}"] += 1
                if iv.key not in _KEYS:
                    missing.append({"symbol": symbol, "start_ms": iv.s1, "d_min": d})
    return {
        "classifications": count,
        "status_counts": dict(statuses),
        "admitted_by_d": dict(admitted),
        "missing": missing,
    }


def audit_scope(index: Index, *, workers: int = 1) -> dict[str, Any]:
    global _INDEX, _ROWS, _KEYS  # initialized once before forking read-only workers
    if not 1 <= workers <= 3:
        raise ValueError("use one to three workers")
    started = time.monotonic()
    every = b1.intervals(index)
    _INDEX = index
    _ROWS = defaultdict(list)
    for iv in every:
        _ROWS[iv.symbol].append(iv)
    _KEYS = {iv.key for iv in b1.event_superset(index, every)}
    primary = {iv.key for iv in every if iv.key in _KEYS and b1.interval_class(iv.n_min) == "4h"}
    sample = {iv.key for iv in b1.validation_sample(every)}
    symbols = sorted(_ROWS)
    if workers > 1 and "fork" not in mp.get_all_start_methods():
        raise ValueError("parallel audit requires fork; use --workers 1")
    if workers == 1:
        results = [_audit_symbol(symbol) for symbol in symbols]
    else:
        # No index is pickled per task. The children inherit read-only inputs;
        # only aggregate counts and omitted IDs return to the parent.
        with mp.get_context("fork").Pool(workers) as pool:
            results = pool.map(_audit_symbol, symbols)
    labels: Counter[str] = Counter()
    admitted: Counter[str] = Counter()
    missing = []
    for row in results:
        labels.update(row["status_counts"])
        admitted.update(row["admitted_by_d"])
        missing.extend(row["missing"])
    return {
        "version": VERSION,
        "status": "fail" if missing else "pass",
        "forecast_intervals": len(every),
        "event_superset_intervals": len(_KEYS),
        "classifications": sum(r["classifications"] for r in results),
        "four_hour_candidate_intervals": len(primary),
        "four_hour_s1_pool_intervals": len(primary | sample),
        "four_hour_sample_overlap": len(primary & sample),
        "sample_other_class_candidates": len((_KEYS & sample) - primary),
        "decision_offsets_minutes": list(b1.DECISION_OFFSETS_MIN),
        "status_counts": dict(sorted(labels.items())),
        "admitted_by_d": dict(sorted(admitted.items())),
        "admitted_outside_superset": len(missing),
        "omitted_ids": missing,
        "synthetic_close": -0.0045,
        "classifier_monkeypatched": False,
        "real_premium_or_returns_read": False,
        "market_requests": 0,
        "workers": workers,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3, choices=(1, 2, 3))
    args = parser.parse_args()
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is required")
    if subprocess.check_output(  # noqa: S603 - discovered local Git, fixed arguments
        [git, "status", "--porcelain"], text=True
    ).strip():
        raise SystemExit("scope audit requires a clean checkout")
    revision = subprocess.check_output(  # noqa: S603 - discovered local Git, fixed arguments
        [git, "rev-parse", "HEAD"], text=True
    ).strip()
    index = load_index(verified(args.index, b1.INDEX_SHA256))
    if index.stage != b1.STAGE:
        raise SystemExit("scope audit requires the discovery index")
    report = audit_scope(index, workers=args.workers)
    report.update(
        code_revision=revision,
        working_tree_dirty=False,
        index_sha256=b1.INDEX_SHA256,
        classifier_sha256=hashlib.sha256(Path(b1.__file__).read_bytes()).hexdigest(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    sys.stdout.write(json.dumps({k: v for k, v in report.items() if k != "omitted_ids"}) + "\n")
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
