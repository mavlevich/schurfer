"""HYP-012b report: run one registered stage (discovery or holdout) as two phases with a
durable claim between them. See source_lead_multi_source.py and
docs/research/source-lead-multi-source-hyp012b-v1.md.

Files in the stage directory, each written once:

- `inputs.json` (+ `.sha256`), from `--phase prepare`: the Bybit catalogue as fetched
  (trading and delisted contracts), the outcome-blind funnel, the candidates after the
  route identity check, and their raw Bybit 1-minute klines. No return is computed.
- `claim.json`, taken by `--phase read` before any return is computed. It pins the
  inputs hash, the ordered tested family and, at holdout, the discovery result hash. A
  claim without a result (a crashed read) is resumed only on exactly what it pins, on the
  stored inputs, never on a new fetch.
- `result.json` (+ `.sha256`): computed from the stored inputs only. At holdout only the
  survivors are evaluated.

Every file is published atomically (temp file, fsync, hard link), so a crash leaves
either no file or a whole one. A crash between a file and its digest is finished on the
next run: the inputs digest is written from the whole file, and a result digest only if
the stored result equals its replay.

`--phase all` prepares when no inputs exist and then reads. `--phase funnel` prints the
outcome-blind resolution counts and writes nothing. Every phase refuses a stage whose
window end plus the maturation lag has not passed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .ohlcv import Candle
from .reporting import json_ready, normalize_code_revision
from .source_lead_multi_source import (
    EXPLORATORY_SOURCES,
    FAMILY_VERSION,
    FORMAL_SOURCES,
    HOLDOUT_END,
    BybitInstrument,
    Candidate,
    Outcome,
    assert_stage_mature,
    build_candidates,
    evaluate,
    family_verdicts,
    floor_shortfalls,
    route_identity_reason,
    stage_window,
    venue_result,
)
from .source_lead_repository import SourceLeadRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

INPUTS_NAME = "inputs.json"
CLAIM_NAME = "claim.json"
RESULT_NAME = "result.json"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
# Trading contracts are the default; delisted ones must be asked for, or the catalogue
# would silently drop every perpetual delisted since the window.
CATALOGUE_STATUSES = ("Trading", "PreLaunch", "Delivering", "Closed")
_FETCH_CONCURRENCY = 4
_FETCH_ATTEMPTS = 3

RawBars = dict[int, list[list[str]] | None]


class AlreadyReadError(RuntimeError):
    """The stage has a completed read; it is never read again."""


async def _get_json(client: Any, url: str, params: dict[str, str]) -> dict[str, Any]:
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            if payload.get("retCode") != 0:
                raise RuntimeError(
                    f"bybit retCode={payload.get('retCode')} {payload.get('retMsg')}"
                )
            return payload
        except Exception:
            if attempt == _FETCH_ATTEMPTS:
                raise
            await asyncio.sleep(2.0 * attempt)
    raise AssertionError("unreachable")


async def fetch_bybit_instruments() -> list[dict[str, Any]]:
    """Raw USDT linear perpetuals of every catalogue status, sorted by symbol."""
    import httpx

    by_symbol: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for status in CATALOGUE_STATUSES:
            cursor = ""
            while True:
                params = {"category": "linear", "status": status, "limit": "1000"}
                if cursor:
                    params["cursor"] = cursor
                payload = await _get_json(client, BYBIT_INSTRUMENTS_URL, params)
                for item in payload["result"]["list"]:
                    if (
                        item.get("contractType") == "LinearPerpetual"
                        and item.get("quoteCoin") == "USDT"
                        and item.get("settleCoin") == "USDT"
                    ):
                        by_symbol[str(item["symbol"])] = item
                cursor = str(payload["result"].get("nextPageCursor") or "")
                if not cursor:
                    break
    return [by_symbol[k] for k in sorted(by_symbol)]


def parse_instruments(raw: Sequence[dict[str, Any]]) -> tuple[BybitInstrument, ...]:
    return tuple(
        BybitInstrument(
            native_id=str(item["symbol"]),
            base=str(item["baseCoin"]).upper(),
            launch_ms=int(item["launchTime"]),
            delivery_ms=int(item.get("deliveryTime") or 0),
        )
        for item in raw
    )


async def fetch_klines(candidates: Sequence[Candidate]) -> RawBars:
    """Raw Bybit 1-minute klines from the reference minute to the exit bar, per event id;
    None when the request still failed after retries (`bar_fetch_failed`)."""
    import httpx

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
    out: RawBars = {}
    async with httpx.AsyncClient(timeout=30) as client:

        async def one(candidate: Candidate) -> None:
            params = {
                "category": "linear",
                "symbol": candidate.bybit_native_id,
                "interval": "1",
                "start": str(candidate.reference_ms),
                "end": str(candidate.exit_bar_ms),
                "limit": "40",
            }
            try:
                async with semaphore:
                    payload = await _get_json(client, BYBIT_KLINE_URL, params)
            except Exception:
                out[candidate.event_id] = None
                return
            rows = [[str(v) for v in row[:6]] for row in payload["result"]["list"]]
            out[candidate.event_id] = sorted(rows, key=lambda r: int(r[0]))

        await asyncio.gather(*(one(c) for c in candidates))
    return out


def candles(rows: list[list[str]] | None) -> dict[int, Candle]:
    return {
        int(r[0]): Candle(
            int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        )
        for r in rows or []
    }


def apply_route_identity(
    candidates: Sequence[Candidate], bars: RawBars
) -> tuple[tuple[Candidate, ...], dict[str, int]]:
    """Drop candidates whose route fails the pre-entry identity check. A failed kline
    fetch is not an identity failure: those stay and resolve as `bar_fetch_failed`."""
    kept: list[Candidate] = []
    statuses: dict[str, int] = {}
    for candidate in candidates:
        if bars.get(candidate.event_id) is None and candidate.event_id in bars:
            kept.append(candidate)
            continue
        reference = candles(bars.get(candidate.event_id)).get(candidate.reference_ms)
        reason = route_identity_reason(candidate, reference)
        if reason:
            key = f"route_identity:{reason}"
            statuses[key] = statuses.get(key, 0) + 1
            continue
        kept.append(candidate)
    return tuple(kept), statuses


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    row = asdict(candidate)
    row["source_at"] = candidate.source_at.isoformat()
    return row


def _candidate_from_json(row: dict[str, Any]) -> Candidate:
    return Candidate(**{**row, "source_at": datetime.fromisoformat(row["source_at"])})


async def prepare_inputs(
    stage: str, *, now: datetime, code_revision: str, working_tree_dirty: bool
) -> dict[str, Any]:
    """Fetch and freeze everything a stage read needs. Computes no return."""
    assert_stage_mature(stage, now)
    start, end = stage_window(stage)
    if end > HOLDOUT_END:
        raise ValueError("refusing to read on or after the HYP-012 v2 cohort start")
    raw_instruments = await fetch_bybit_instruments()
    repository = SourceLeadRepository.from_url(os.environ["DATABASE_URL"])
    try:
        # A small margin before the window so an event's earlier observations load too.
        events = await repository.load(start - timedelta(hours=6), end)
    finally:
        await repository.close()
    # Every family venue is prepared for both stages; the holdout read keeps only the
    # discovery survivors, so the inputs never depend on a discovery outcome.
    candidates, funnel = build_candidates(
        events,
        bybit_instruments=parse_instruments(raw_instruments),
        stage=stage,
        sources=[*FORMAL_SOURCES, *EXPLORATORY_SOURCES],
    )
    raw_bars = await fetch_klines(candidates) if candidates else {}
    kept, identity = apply_route_identity(candidates, raw_bars)
    funnel = {**funnel, **identity}
    return {
        "family_version": FAMILY_VERSION,
        "stage": stage,
        "window": [start.isoformat(), end.isoformat()],
        "prepared_at": now.isoformat(),
        "code_revision": normalize_code_revision(code_revision),
        "working_tree_dirty": working_tree_dirty,
        "bybit_instruments": raw_instruments,
        "bybit_instruments_sha256": _sha(json.dumps(raw_instruments, sort_keys=True).encode()),
        "event_ids_sha256": _sha(json.dumps(sorted(e.event_id for e in events)).encode()),
        "funnel": dict(sorted(funnel.items())),
        "candidates": [_candidate_json(c) for c in kept],
        "bars": {str(c.event_id): raw_bars.get(c.event_id) for c in kept},
    }


def resolution_summary(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per source: candidates, fetch failures, and episodes with both the entry and the
    exit bar present. Bar presence only: no price is compared."""
    counts: dict[str, dict[str, Any]] = {}
    for row in inputs["candidates"]:
        candidate = _candidate_from_json(row)
        raw = inputs["bars"].get(str(candidate.event_id))
        present = candles(raw)
        entry_ok = candidate.entry_ms in present and candidate.exit_bar_ms in present
        out = counts.setdefault(
            candidate.source_exchange,
            {"candidates": 0, "bars_present": 0, "fetch_failed": 0, "weeks": {}, "assets": set()},
        )
        out["candidates"] += 1
        if raw is None:
            out["fetch_failed"] += 1
        if entry_ok:
            out["bars_present"] += 1
            out["assets"].add(candidate.cluster_key)
            out["weeks"][candidate.week] = out["weeks"].get(candidate.week, 0) + 1
    for out in counts.values():
        out["assets"] = len(out["assets"])
        out["max_week_share"] = (
            round(max(out["weeks"].values()) / out["bars_present"], 3)
            if out["bars_present"]
            else None
        )
    return dict(sorted(counts.items()))


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(json_ready(payload), indent=2, sort_keys=True).encode() + b"\n"


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_once(path: Path, body: bytes) -> None:
    """Atomic and write-once: the bytes go to a unique temp file, are fsynced, then
    hard-linked to `path`, which fails if `path` exists. A reader sees either no file or
    the whole file, never a partial one."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _digest_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _write_digest(path: Path, digest: str) -> None:
    """Durably publish the digest: the temp bytes are fsynced before the rename, the
    directory after it."""
    tmp = path.with_name(f".{path.name}.sha256.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(digest.encode() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(_digest_path(path))
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def write_once(path: Path, payload: dict[str, Any]) -> str:
    """Publish `path` once, then its `.sha256`; refuses if `path` exists."""
    body = _body(payload)
    _publish_once(path, body)
    digest = _sha(body)
    _write_digest(path, digest)
    return digest


def complete_digest(path: Path) -> None:
    """Finish a publication interrupted between the file and its digest. Safe because
    the file itself is published atomically, so an existing one is always whole."""
    if path.exists() and not _digest_path(path).exists():
        _write_digest(path, _sha(path.read_bytes()))


def load_verified(path: Path) -> tuple[dict[str, Any], str]:
    body = path.read_bytes()
    stored = _digest_path(path).read_text(encoding="utf-8").strip()
    digest = _sha(body)
    if digest != stored:
        raise ValueError(f"{path} does not match its sha256")
    return json.loads(body), digest


_CLAIM_KEYS = ("family_version", "stage", "inputs_sha256", "tested_family", "discovery_sha256")


def take_claim(stage_dir: Path, expected: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The durable claim, taken before any return is computed. It pins the inputs, the
    ordered tested family and (at holdout) the discovery result. A claim left by a
    crashed read is resumed only if it pins exactly the same; otherwise it is refused."""
    claim_path = stage_dir / CLAIM_NAME
    claim = {**expected, "claimed_at": now.isoformat()}
    try:
        _publish_once(claim_path, _body(claim))
    except FileExistsError:
        existing: dict[str, Any] = json.loads(claim_path.read_text(encoding="utf-8"))
        for key in _CLAIM_KEYS:
            if existing.get(key) != expected[key]:
                raise ValueError(f"the open claim pins another {key}; refusing") from None
        return existing
    return claim


def discovery_survivors(discovery_dir: Path) -> tuple[list[str], str]:
    """The ordered discovery survivors and the discovery result hash. A discovery result
    without its digest is refused: only the discovery read may finish it, because only it
    checks the result against its replay from the claim and the inputs."""
    if not _digest_path(discovery_dir / RESULT_NAME).exists():
        raise ValueError(
            "the discovery result has no digest; finish it with the discovery read first"
        )
    payload, digest = load_verified(discovery_dir / RESULT_NAME)
    if payload.get("family_version") != FAMILY_VERSION or payload.get("stage") != "discovery":
        raise ValueError("not a discovery artifact of this family")
    survivors = [s for s in payload["tested_family"] if payload["verdicts"].get(s) == "survives"]
    return survivors, digest


def outcome_for(candidate: Candidate, raw: list[list[str]] | None, fetched: bool) -> Outcome:
    if fetched and raw is None:
        return Outcome(candidate, False, "bar_fetch_failed", None)
    bars = candles(raw)
    return evaluate(candidate, bars.get(candidate.entry_ms), bars.get(candidate.exit_bar_ms))


def compute_result(
    inputs: dict[str, Any], inputs_sha: str, claim: dict[str, Any]
) -> dict[str, Any]:
    """Pure: the stage result from the stored inputs and the claim, so a replay gives the
    same bytes. Outcomes are computed only for the venues in scope: the formal family
    and, at discovery only, the exploratory venues. At holdout nothing outside the
    survivors is evaluated, so those windows stay unread."""
    stage = inputs["stage"]
    formal: list[str] = list(claim["tested_family"])
    exploratory = list(EXPLORATORY_SOURCES) if stage == "discovery" else []
    in_scope = {*formal, *exploratory}
    outcomes = [
        outcome_for(
            candidate,
            inputs["bars"].get(str(candidate.event_id)),
            str(candidate.event_id) in inputs["bars"],
        )
        for candidate in map(_candidate_from_json, inputs["candidates"])
        if candidate.source_exchange in in_scope
    ]
    formal_results = [venue_result(s, outcomes) for s in formal]
    exploratory_results = [venue_result(s, outcomes) for s in exploratory]
    verdicts = family_verdicts(formal_results, stage=stage) if formal_results else {}
    return {
        "family_version": FAMILY_VERSION,
        "stage": stage,
        "window": inputs["window"],
        "tested_family": formal,
        "inputs_sha256": inputs_sha,
        "discovery_sha256": claim["discovery_sha256"],
        "claimed_at": claim["claimed_at"],
        "code_revision": inputs["code_revision"],
        "funnel": inputs["funnel"],
        "formal_results": [asdict(r) for r in formal_results],
        "exploratory_results": [asdict(r) for r in exploratory_results],
        "verdicts": verdicts,
        "floor_shortfalls": (
            {r.source: floor_shortfalls(r) for r in formal_results} if stage == "holdout" else {}
        ),
    }


def read_stage(
    stage: str, stage_dir: Path, discovery_dir: Path | None, now: datetime
) -> dict[str, Any]:
    complete_digest(stage_dir / INPUTS_NAME)
    inputs, inputs_sha = load_verified(stage_dir / INPUTS_NAME)
    if inputs.get("family_version") != FAMILY_VERSION or inputs.get("stage") != stage:
        raise ValueError(f"{stage_dir} does not hold {stage} inputs of this family")
    assert_stage_mature(stage, now)
    if stage == "holdout":
        if discovery_dir is None:
            raise ValueError("the holdout read requires --discovery-artifact")
        formal, discovery_sha = discovery_survivors(discovery_dir)
    else:
        formal, discovery_sha = list(FORMAL_SOURCES), None
    expected = {
        "family_version": FAMILY_VERSION,
        "stage": stage,
        "inputs_sha256": inputs_sha,
        "tested_family": formal,
        "discovery_sha256": discovery_sha,
    }
    result_path = stage_dir / RESULT_NAME
    if result_path.exists():
        if _digest_path(result_path).exists():
            raise AlreadyReadError(f"{stage_dir} was already read")
        # Interrupted between the result and its digest: finish only if the stored
        # result is exactly what the claim and the same inputs produce.
        claim = take_claim(stage_dir, expected, now)
        payload = compute_result(inputs, inputs_sha, claim)
        if _body(payload) != result_path.read_bytes():
            raise ValueError("the stored result differs from its replay; refusing")
        _write_digest(result_path, _sha(result_path.read_bytes()))
        return payload
    claim = take_claim(stage_dir, expected, now)
    payload = compute_result(inputs, inputs_sha, claim)
    write_once(result_path, payload)
    return payload


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

    for result in payload["formal_results"]:
        verdict = payload["verdicts"].get(result["source"], "n/a")
        shortfall = payload.get("floor_shortfalls", {}).get(result["source"])
        if shortfall:
            verdict += f" (below floor: {', '.join(shortfall)})"
        lines.append(row(result, verdict))
    lines += ["", "Exploratory (no verdict):", ""]
    lines += [row(r, "exploratory") for r in payload["exploratory_results"]]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stage", choices=("discovery", "holdout"), required=True)
    parser.add_argument("--phase", choices=("funnel", "prepare", "read", "all"), default="all")
    parser.add_argument("--stage-dir", type=Path, default=None)
    parser.add_argument("--discovery-artifact", type=Path, default=None)
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    parser.set_defaults(working_tree_dirty=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    now = datetime.now(UTC)

    def prepare() -> dict[str, Any]:
        return asyncio.run(
            prepare_inputs(
                args.stage,
                now=now,
                code_revision=args.code_revision,
                working_tree_dirty=args.working_tree_dirty,
            )
        )

    if args.phase == "funnel":
        inputs = prepare()
        summary = {"funnel": inputs["funnel"], "resolution": resolution_summary(inputs)}
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return
    if args.stage_dir is None:
        raise SystemExit("--stage-dir is required")
    inputs_path = args.stage_dir / INPUTS_NAME
    complete_digest(inputs_path)
    if args.phase in ("prepare", "all") and not inputs_path.exists():
        inputs = prepare()
        args.stage_dir.mkdir(parents=True, exist_ok=True)
        digest = write_once(inputs_path, inputs)
        sys.stdout.write(f"inputs sha256 {digest}\n")
        sys.stdout.write(json.dumps(resolution_summary(inputs), indent=2, sort_keys=True) + "\n")
    elif args.phase == "prepare":
        raise SystemExit(f"{inputs_path} exists: inputs are prepared once")
    if args.phase in ("read", "all"):
        payload = read_stage(args.stage, args.stage_dir, args.discovery_artifact, now)
        sys.stdout.write(render_markdown(payload))
        digest = (args.stage_dir / f"{RESULT_NAME}.sha256").read_text(encoding="utf-8").strip()
        sys.stdout.write(f"\nresult sha256 {digest}\n")


if __name__ == "__main__":
    main()
