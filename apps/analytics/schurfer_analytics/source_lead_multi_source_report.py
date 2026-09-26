"""HYP-012b report: run one registered stage (discovery or holdout) and write a
write-once artifact. See source_lead_multi_source.py and
docs/research/source-lead-multi-source-hyp012b-v1.md.

- `--stage discovery` evaluates the formal family and the exploratory venues.
- `--stage holdout` requires the discovery artifact; it evaluates only the venues that
  survived discovery, plus the exploratory venues with no verdict.

Each stage is read once; its artifact directory must not exist yet.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .ohlcv import ONE_MINUTE_MS, Candle
from .reporting import json_ready, normalize_code_revision
from .source_lead_multi_source import (
    EXPLORATORY_SOURCES,
    FAMILY_VERSION,
    FORMAL_SOURCES,
    HOLDOUT_END,
    Candidate,
    Outcome,
    build_candidates,
    evaluate,
    family_verdicts,
    stage_window,
    venue_result,
)
from .source_lead_repository import SourceLeadRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

ARTIFACT_NAME = "result.json"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
_FETCH_CONCURRENCY = 4
_FETCH_ATTEMPTS = 3


async def fetch_bybit_catalog() -> tuple[dict[str, tuple[str, int]], str]:
    """base -> (native id, launchTime ms) for USDT linear perpetuals, and a hash."""
    import httpx

    items: list[dict[str, Any]] = []
    cursor = ""
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {"category": "linear", "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            response = await client.get(BYBIT_INSTRUMENTS_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode") != 0:
                raise RuntimeError(f"bybit instruments-info retCode={payload.get('retCode')}")
            items.extend(payload["result"]["list"])
            cursor = str(payload["result"].get("nextPageCursor") or "")
            if not cursor:
                break
    catalog: dict[str, tuple[str, int]] = {}
    for item in items:
        if (
            item.get("contractType") == "LinearPerpetual"
            and item.get("quoteCoin") == "USDT"
            and item.get("settleCoin") == "USDT"
        ):
            catalog[str(item["baseCoin"]).upper()] = (str(item["symbol"]), int(item["launchTime"]))
    digest = hashlib.sha256(json.dumps(sorted(catalog.items())).encode()).hexdigest()
    return catalog, digest


async def fetch_bars(
    candidates: Sequence[Candidate],
) -> dict[int, tuple[Candle | None, ...] | None]:
    """(entry bar, exit bar) per event id, from the Bybit 1-minute klines; None when the
    request still failed after retries (recorded as `bar_fetch_failed`, never as a
    missing bar)."""
    import ccxt.async_support as ccxt

    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
    out: dict[int, tuple[Candle | None, ...] | None] = {}
    try:
        await exchange.load_markets()
        by_id = exchange.markets_by_id

        async def one(candidate: Candidate) -> None:
            markets = by_id.get(candidate.bybit_native_id) or []
            markets = markets if isinstance(markets, list) else [markets]
            linear = [m for m in markets if m.get("swap") and m.get("linear")]
            if len(linear) != 1:
                out[candidate.event_id] = (None, None)
                return
            symbol = linear[0]["symbol"]
            rows = None
            for attempt in range(1, _FETCH_ATTEMPTS + 1):
                try:
                    async with semaphore:
                        rows = await exchange.fetch_ohlcv(
                            symbol, "1m", since=candidate.entry_ms - ONE_MINUTE_MS, limit=40
                        )
                    break
                except Exception:
                    if attempt == _FETCH_ATTEMPTS:
                        out[candidate.event_id] = None
                        return
                    await asyncio.sleep(2.0 * attempt)
            if rows is None:
                out[candidate.event_id] = None
                return
            bars = {
                int(r[0]): Candle(
                    int(r[0]),
                    float(r[1]),
                    float(r[2]),
                    float(r[3]),
                    float(r[4]),
                    float(r[5]) if r[5] is not None else None,
                )
                for r in rows
            }
            out[candidate.event_id] = (
                bars.get(candidate.entry_ms),
                bars.get(candidate.exit_bar_ms),
            )

        await asyncio.gather(*(one(c) for c in candidates))
    finally:
        await exchange.close()
    return out


def _discovery_survivors(path: Path) -> list[str]:
    payload = json.loads((path / ARTIFACT_NAME).read_text(encoding="utf-8"))
    stored = (path / f"{ARTIFACT_NAME}.sha256").read_text(encoding="utf-8").strip()
    if hashlib.sha256((path / ARTIFACT_NAME).read_bytes()).hexdigest() != stored:
        raise ValueError("discovery artifact does not match its sha256")
    if payload.get("family_version") != FAMILY_VERSION or payload.get("stage") != "discovery":
        raise ValueError("not a discovery artifact of this family")
    return [s for s, v in payload["verdicts"].items() if v == "survives"]


async def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    stage: str = args.stage
    start, end = stage_window(stage)
    if end > HOLDOUT_END:
        raise ValueError("refusing to read on or after the HYP-012 v2 cohort start")
    if stage == "holdout":
        if args.discovery_artifact is None:
            raise ValueError("--stage holdout requires --discovery-artifact")
        formal = _discovery_survivors(args.discovery_artifact)
    else:
        formal = list(FORMAL_SOURCES)
    catalog, catalog_sha = await fetch_bybit_catalog()
    repository = SourceLeadRepository.from_url(os.environ["DATABASE_URL"])
    try:
        # A small margin before the window so an event's earlier observations load too.
        events = await repository.load(start - timedelta(hours=6), end)
    finally:
        await repository.close()
    sources = [*formal, *EXPLORATORY_SOURCES]
    candidates, funnel = build_candidates(
        events, bybit_launch_ms=catalog, stage=stage, sources=sources
    )
    bars = await fetch_bars(candidates) if candidates else {}
    if args.funnel_only:
        return funnel_only_payload(stage, candidates, bars, funnel, catalog_sha)
    outcomes: list[Outcome] = [outcome_for(c, bars) for c in candidates]
    formal_results = [venue_result(s, outcomes) for s in formal]
    exploratory_results = [venue_result(s, outcomes) for s in EXPLORATORY_SOURCES]
    verdicts = family_verdicts(formal_results, stage=stage) if formal_results else {}
    return {
        "family_version": FAMILY_VERSION,
        "stage": stage,
        "window": [start.isoformat(), end.isoformat()],
        "tested_family": formal,
        "generated_at": datetime.now(UTC).isoformat(),
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
        "bybit_catalog_sha256": catalog_sha,
        "funnel": funnel,
        "formal_results": [asdict(r) for r in formal_results],
        "exploratory_results": [asdict(r) for r in exploratory_results],
        "verdicts": verdicts,
    }


def outcome_for(candidate: Candidate, bars: dict[int, tuple[Candle | None, ...] | None]) -> Outcome:
    if candidate.event_id in bars and bars[candidate.event_id] is None:
        return Outcome(candidate, False, "bar_fetch_failed", None)
    entry, exit_bar = bars.get(candidate.event_id) or (None, None)
    return evaluate(candidate, entry, exit_bar)


def funnel_only_payload(
    stage: str,
    candidates: Sequence[Candidate],
    bars: dict[int, tuple[Candle | None, ...] | None],
    funnel: dict[str, int],
    catalog_sha: str,
) -> dict[str, Any]:
    """Resolution status per venue from bar presence only: no price is compared and no
    return is computed, so this is safe before the registration is signed off."""
    counts: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        fetched = bars.get(candidate.event_id)
        entry, exit_bar = fetched if fetched is not None else (None, None)
        present = (
            entry is not None
            and entry.ts_ms == candidate.entry_ms
            and exit_bar is not None
            and exit_bar.ts_ms == candidate.exit_bar_ms
        )
        row = counts.setdefault(
            candidate.source_exchange,
            {"candidates": 0, "bars_present": 0, "fetch_failed": 0, "weeks": {}, "assets": set()},
        )
        row["candidates"] += 1
        if fetched is None and candidate.event_id in bars:
            row["fetch_failed"] += 1
        if present:
            row["bars_present"] += 1
            row["assets"].add(candidate.cluster_key)
            row["weeks"][candidate.week] = row["weeks"].get(candidate.week, 0) + 1
    for row in counts.values():
        row["assets"] = len(row["assets"])
    return {
        "family_version": FAMILY_VERSION,
        "stage": stage,
        "mode": "funnel_only",
        "bybit_catalog_sha256": catalog_sha,
        "funnel": funnel,
        "resolution": counts,
    }


def write_artifact(out_dir: Path, payload: dict[str, Any]) -> str:
    out_dir.mkdir(parents=True, exist_ok=False)  # write-once: one read per stage
    body = json.dumps(json_ready(payload), indent=2, sort_keys=True).encode() + b"\n"
    (out_dir / ARTIFACT_NAME).write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    (out_dir / f"{ARTIFACT_NAME}.sha256").write_text(digest + "\n", encoding="utf-8")
    return digest


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# HYP-012b {payload['stage']} ({payload['window'][0]} .. {payload['window'][1]})",
        "",
        "| source | resolved | assets | max week | mean net % | 95% CI | p | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def row(result: dict[str, Any], verdict: str) -> str:
        def f(value: Any, digits: int = 3) -> str:
            return "n/a" if value is None else f"{value:.{digits}f}"

        return (
            f"| {result['source']} | {result['resolved']} | {result['assets']} | "
            f"{f(result['max_week_share'], 2)} | {f(result['mean_net_pct'])} | "
            f"[{f(result['ci_lower_pct'])}, {f(result['ci_upper_pct'])}] | "
            f"{f(result['p_value'], 4)} | {verdict} |"
        )

    lines += [
        row(r, payload["verdicts"].get(r["source"], "n/a")) for r in payload["formal_results"]
    ]
    lines += ["", "Exploratory (no verdict):", ""]
    lines += [row(r, "exploratory") for r in payload["exploratory_results"]]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stage", choices=("discovery", "holdout"), required=True)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--discovery-artifact", type=Path, default=None)
    parser.add_argument(
        "--funnel-only",
        action="store_true",
        help="resolution counts only (bar presence); computes no return, writes no artifact",
    )
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    parser.set_defaults(working_tree_dirty=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.funnel_only:
        payload = asyncio.run(run_stage(args))
        sys.stdout.write(json.dumps(json_ready(payload), indent=2, sort_keys=True) + "\n")
        return
    if args.out_dir is None:
        raise SystemExit("--out-dir is required for a stage read")
    if args.out_dir.exists():
        raise SystemExit(f"{args.out_dir} exists: each stage is read exactly once")
    payload = asyncio.run(run_stage(args))
    digest = write_artifact(args.out_dir, payload)
    sys.stdout.write(render_markdown(payload))
    sys.stdout.write(f"\nartifact sha256 {digest}\n")


if __name__ == "__main__":
    main()
