"""One-time reconciliation for legacy paper trades stranded `open` in
Postgres with no way to ever be discovered or closed by the running
service.

Root cause (verified against production, `entry_at` all before commit
29ccd71 "enforce canonical instrument identity"): the pre-canonical-identity
`run_early_momentum_trigger` opened a new paper position with only a
non-atomic `find_open_trade_id` read-then-write check -- no reservation, no
CAS. Rapid-fire duplicate breakouts on the same symbol each independently
passed that check and each called `paper.open_paper`, which does an
unconditional `SET` on `position:paper:{exchange}:{base}` -- so only the
*last* writer's position survived in Redis; every earlier duplicate's `open`
row in `app.trades` has no reachable position and will never be closed by
`_tick`, `close_paper`, or anything else. Whether a given row is still the
key's owner is therefore not "does the key exist" but "does the key's
payload still point at this row's trade_id" -- see `classify_orphans`.

Never call any of this from a running service. `find_open_trade_id`
(journal.py) has no strategy filter -- ANY strategy's stranded `open` row
blocks every other strategy's future entries on that same symbol, so this
classifier is written to be reusable by `--strategy` argument, not hardcoded
to one strategy's name.

Two-step, manifest-based workflow (matches the token-history dataset's own
"atomically-written, re-verified-before-write manifest.json" convention in
ROADMAP.md, not invented here):

    python -m schurfer_execution.legacy_paper_repair classify \\
        --strategy early_momentum_v1 --before 2026-08-20T17:31:04Z \\
        --out manifest.json

    python -m schurfer_execution.legacy_paper_repair apply \\
        --report manifest.json

`classify` is fully read-only (a Redis error, or a symbol this process can't
resolve against live exchange market metadata, aborts that one row's
classification -- "couldn't check" must never be read as "no position
found"; those rows are reported separately as needing manual review, never
silently folded into the candidate set). `apply` re-runs the exact same
classification live and refuses to touch the database unless it reproduces
the frozen manifest's trade-id set and fingerprint byte-for-byte; a partial
prior apply (some rows already cancelled, some still open) also aborts for
manual review rather than silently continuing. A full second `--apply` of an
already-applied manifest is a no-op, not an error.

Never fabricates an exit: only `status`, `accounting_status`,
`accounting_error`, and an appended `notes` line are written.
`exit_price`/`exit_at`/every PnL column stay NULL forever, exactly the
"missing accounting stays unresolved, never assumed" rule this codebase
already applies everywhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import redis.asyncio as aioredis
import structlog
from psycopg.rows import dict_row

from .exchanges import MARKET_EXCHANGE_FACTORIES
from .paper import _TRADE_ID_KEY, paper_key
from .symbols import resolve_execution_instrument

log = structlog.get_logger()

ACCOUNTING_ERROR = "legacy_duplicate_without_durable_position"


class RepairAbortedError(Exception):
    """Raised whenever apply refuses to touch the database at all -- live
    classification drifted from the manifest, or the manifest's own rows
    are in an unexpected/partial state. Never partially apply."""


@dataclass(frozen=True)
class OrphanCandidate:
    trade_id: int
    symbol: str
    exchange: str
    side: str
    entry_at: datetime
    entry_price: float
    size_usd: float
    leverage: float
    accounting_status: str


@dataclass(frozen=True)
class SkippedRow:
    """A candidate row classify_orphans could not confidently classify
    either way. Never auto-included as an orphan -- surfaced for a human to
    check by hand."""

    trade_id: int
    symbol: str
    exchange: str
    reason: str


@dataclass(frozen=True)
class ClassifyResult:
    candidates: tuple[OrphanCandidate, ...]
    skipped: tuple[SkippedRow, ...]


@dataclass(frozen=True)
class Manifest:
    strategy: str
    before: datetime
    generated_at: datetime
    trade_ids: tuple[int, ...]  # sorted, the frozen set apply must reproduce
    fingerprint: str
    candidates: tuple[OrphanCandidate, ...]  # full audit detail, sorted by trade_id


_SELECT_OPEN_PAPER_CANDIDATES = """
SELECT id, symbol, exchange, side, entry_at, entry_price, size_usd, leverage, accounting_status
FROM app.trades
WHERE status = 'open'
  AND setup_context->>'paper' = 'true'
  AND setup_context->>'strategy' = %(strategy)s
  AND episode_id IS NULL
  AND entry_at < %(before)s
  AND accounting_status IS DISTINCT FROM 'complete'
ORDER BY entry_at
"""


def _native_market_id(db_symbol: str) -> str:
    """Legacy `app.trades.symbol` for this strategy generation was written
    as `f"{base}/USDT:USDT"` where `base` was actually the collector's raw
    (pre-canonical-identity) symbol string, e.g. `"RAYDIUMUSDT/USDT:USDT"`
    for a market whose real CCXT unified symbol is `"RAYDIUM/USDT:USDT"`.
    The part before the slash is coincidentally the exchange's true native
    market id (`"RAYDIUMUSDT"`), verified directly against Bybit's public
    `/v5/market/instruments-info` -- resolve *that*, never the base after
    it, through `resolve_execution_instrument` to get the real base."""
    return db_symbol.split("/")[0]


def _resolve_base(exchange_client: Any, *, db_symbol: str) -> str | None:
    """Returns the true base this row's symbol maps to under current
    canonical-identity resolution, or None if the symbol can't be resolved
    against loaded exchange market metadata (delisted, renamed -- must not
    guess)."""
    try:
        instrument = resolve_execution_instrument(exchange_client, _native_market_id(db_symbol))
    except (ValueError, RuntimeError):
        return None
    return instrument.base


async def _current_position_owner(rdb: Any, *, exchange: str, base: str) -> tuple[bool, int | None]:
    """Returns (position_exists, owner_trade_id). owner_trade_id is None
    either when no position exists, or one exists but its owner can't be
    determined (ambiguous -- caller must not guess)."""
    # A Redis error here raises straight through -- fail closed.
    raw = await rdb.get(paper_key(exchange, base))
    if not raw:
        return False, None

    payload: dict[str, Any] = json.loads(raw)
    embedded = payload.get("trade_id")
    if embedded is not None:
        return True, int(embedded)

    # Older payloads (written before trade_id embedding) fall back to the
    # separate trade:id:paper:* key, same as close_paper itself does.
    fallback = await rdb.get(_TRADE_ID_KEY.format(exchange=exchange, base=base.upper()))
    return True, int(fallback) if fallback else None


async def classify_orphans(
    db_url: str, rdb: Any, *, strategy: str, before: datetime
) -> ClassifyResult:
    """Read-only. A Redis error propagates straight out (fail closed) --
    "Redis is unreachable" must never be treated as "no position exists,
    therefore orphaned", which is exactly backwards."""
    async with (
        await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
        aconn.cursor() as cur,
    ):
        await cur.execute(_SELECT_OPEN_PAPER_CANDIDATES, {"strategy": strategy, "before": before})
        rows = await cur.fetchall()

    by_exchange: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_exchange.setdefault(row["exchange"], []).append(row)

    candidates: list[OrphanCandidate] = []
    skipped: list[SkippedRow] = []
    clients: list[Any] = []
    try:
        for exchange, exchange_rows in by_exchange.items():
            factory = MARKET_EXCHANGE_FACTORIES.get(exchange)
            if factory is None:
                for row in exchange_rows:
                    skipped.append(
                        SkippedRow(
                            trade_id=row["id"],
                            symbol=row["symbol"],
                            exchange=exchange,
                            reason=f"no market client factory registered for exchange {exchange!r}",
                        )
                    )
                continue

            client = factory()
            clients.append(client)
            await client.load_markets()

            for row in exchange_rows:
                base = _resolve_base(client, db_symbol=row["symbol"])
                if base is None:
                    skipped.append(
                        SkippedRow(
                            trade_id=row["id"],
                            symbol=row["symbol"],
                            exchange=exchange,
                            reason="could not resolve symbol against live exchange market metadata",
                        )
                    )
                    continue

                exists, owner_trade_id = await _current_position_owner(
                    rdb, exchange=exchange, base=base
                )
                if not exists:
                    candidates.append(_to_candidate(row))
                    continue
                if owner_trade_id is None:
                    skipped.append(
                        SkippedRow(
                            trade_id=row["id"],
                            symbol=row["symbol"],
                            exchange=exchange,
                            reason=(
                                f"position exists at {paper_key(exchange, base)} "
                                "but its owning trade_id is ambiguous"
                            ),
                        )
                    )
                    continue
                if owner_trade_id != row["id"]:
                    candidates.append(_to_candidate(row))
                # else: this row is still the live position's owner -- not orphaned.
    finally:
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)

    return ClassifyResult(candidates=tuple(candidates), skipped=tuple(skipped))


def _to_candidate(row: dict[str, Any]) -> OrphanCandidate:
    return OrphanCandidate(
        trade_id=row["id"],
        symbol=row["symbol"],
        exchange=row["exchange"],
        side=row["side"],
        entry_at=row["entry_at"],
        entry_price=float(row["entry_price"]),
        size_usd=float(row["size_usd"]),
        leverage=float(row["leverage"]),
        accounting_status=row["accounting_status"],
    )


def _fingerprint(trade_ids: tuple[int, ...], *, strategy: str, before: datetime) -> str:
    payload = {"strategy": strategy, "before": before.isoformat(), "trade_ids": list(trade_ids)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def build_manifest(
    candidates: list[OrphanCandidate], *, strategy: str, before: datetime
) -> Manifest:
    sorted_candidates = tuple(sorted(candidates, key=lambda c: c.trade_id))
    trade_ids = tuple(c.trade_id for c in sorted_candidates)
    return Manifest(
        strategy=strategy,
        before=before,
        generated_at=datetime.now(tz=UTC),
        trade_ids=trade_ids,
        fingerprint=_fingerprint(trade_ids, strategy=strategy, before=before),
        candidates=sorted_candidates,
    )


def write_manifest(manifest: Manifest, path: Path) -> None:
    payload = {
        "strategy": manifest.strategy,
        "before": manifest.before.isoformat(),
        "generated_at": manifest.generated_at.isoformat(),
        "trade_ids": list(manifest.trade_ids),
        "fingerprint": manifest.fingerprint,
        "candidates": [
            {
                "trade_id": c.trade_id,
                "symbol": c.symbol,
                "exchange": c.exchange,
                "side": c.side,
                "entry_at": c.entry_at.isoformat(),
                "entry_price": c.entry_price,
                "size_usd": c.size_usd,
                "leverage": c.leverage,
                "accounting_status": c.accounting_status,
            }
            for c in manifest.candidates
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def read_manifest(path: Path) -> Manifest:
    data = json.loads(path.read_text())
    candidates = tuple(
        OrphanCandidate(
            trade_id=c["trade_id"],
            symbol=c["symbol"],
            exchange=c["exchange"],
            side=c["side"],
            entry_at=datetime.fromisoformat(c["entry_at"]),
            entry_price=c["entry_price"],
            size_usd=c["size_usd"],
            leverage=c["leverage"],
            accounting_status=c["accounting_status"],
        )
        for c in data["candidates"]
    )
    return Manifest(
        strategy=data["strategy"],
        before=datetime.fromisoformat(data["before"]),
        generated_at=datetime.fromisoformat(data["generated_at"]),
        trade_ids=tuple(data["trade_ids"]),
        fingerprint=data["fingerprint"],
        candidates=candidates,
    )


_SELECT_CURRENT_STATE = """
SELECT id, status, accounting_error FROM app.trades WHERE id = ANY(%(ids)s)
"""

_APPLY_CANCEL = """
UPDATE app.trades
SET status = 'cancelled',
    accounting_status = 'incomplete',
    accounting_error = %(accounting_error)s,
    notes = COALESCE(notes || E'\n', '') || %(note)s,
    updated_at = now()
WHERE id = ANY(%(ids)s)
  AND status = 'open'
"""


async def apply_repair(db_url: str, rdb: Any, *, manifest: Manifest) -> int:
    """Re-verifies the manifest against live state before touching
    anything, then cancels exactly the frozen id set in one statement.
    Idempotent: re-applying an already-applied manifest is a no-op, not an
    error. Never partially applies -- any drift or mixed state aborts for
    manual review."""
    if not manifest.trade_ids:
        return 0

    async with (
        await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
        aconn.cursor() as cur,
    ):
        await cur.execute(_SELECT_CURRENT_STATE, {"ids": list(manifest.trade_ids)})
        current = {row["id"]: row for row in await cur.fetchall()}

    missing = set(manifest.trade_ids) - set(current)
    if missing:
        raise RepairAbortedError(f"trade ids from manifest no longer exist: {sorted(missing)}")

    already_applied = {
        tid
        for tid, row in current.items()
        if row["status"] == "cancelled" and row["accounting_error"] == ACCOUNTING_ERROR
    }
    still_open = {tid for tid, row in current.items() if row["status"] == "open"}
    unexpected = set(current) - already_applied - still_open
    if unexpected:
        raise RepairAbortedError(
            f"trade ids in an unexpected state, needs manual review: {sorted(unexpected)}"
        )
    if already_applied == set(manifest.trade_ids):
        log.info("legacy_paper_repair.already_applied", count=len(already_applied))
        return 0
    if already_applied:
        raise RepairAbortedError(
            f"partial apply detected -- {len(already_applied)} already cancelled, "
            f"{len(still_open)} still open. Needs manual review, not auto-continuing: "
            f"already_applied={sorted(already_applied)} still_open={sorted(still_open)}"
        )

    # Live classification must reproduce the frozen manifest exactly --
    # count AND the precise id set AND the fingerprint. A Redis error, or an
    # unresolvable symbol, propagates/skips exactly as in classify_orphans
    # (fail closed) -- any such drift also fails this comparison below.
    fresh = await classify_orphans(db_url, rdb, strategy=manifest.strategy, before=manifest.before)
    fresh_ids = tuple(sorted(c.trade_id for c in fresh.candidates))
    fresh_fingerprint = _fingerprint(fresh_ids, strategy=manifest.strategy, before=manifest.before)
    if fresh_ids != manifest.trade_ids or fresh_fingerprint != manifest.fingerprint:
        raise RepairAbortedError(
            "live classification no longer matches the manifest -- "
            f"manifest had {manifest.trade_ids}, live now has {fresh_ids}. "
            "Re-run classify and review the diff before applying."
        )

    note = (
        f"legacy_paper_repair: cancelled {datetime.now(tz=UTC).isoformat()}, "
        f"manifest fingerprint {manifest.fingerprint}"
    )
    async with (
        await psycopg.AsyncConnection.connect(db_url) as write_conn,
        write_conn.cursor() as cur,
    ):
        await cur.execute(
            _APPLY_CANCEL,
            {
                "ids": list(manifest.trade_ids),
                "accounting_error": ACCOUNTING_ERROR,
                "note": note,
            },
        )
        count = cur.rowcount
    log.info("legacy_paper_repair.applied", count=count, trade_ids=list(manifest.trade_ids))
    return count


# --- CLI -------------------------------------------------------------------


def _parse_iso8601(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    classify_parser = sub.add_parser("classify", help="Read-only: write a manifest of candidates")
    classify_parser.add_argument("--strategy", required=True)
    classify_parser.add_argument("--before", required=True, type=_parse_iso8601)
    classify_parser.add_argument("--out", required=True, type=Path)

    apply_parser = sub.add_parser("apply", help="Re-verify and cancel exactly the manifest's rows")
    apply_parser.add_argument("--report", required=True, type=Path)

    return parser


def _rdb_from_env() -> Any:
    addr = os.getenv("REDIS_ADDR", "localhost:6379")
    host, _, port = addr.partition(":")
    return aioredis.from_url(f"redis://{host}:{port or '6379'}", socket_timeout=5.0)


def _db_url_from_env() -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL is required")
    return db_url


async def _run_classify(args: argparse.Namespace) -> None:
    db_url = _db_url_from_env()
    rdb = _rdb_from_env()
    try:
        result = await classify_orphans(db_url, rdb, strategy=args.strategy, before=args.before)
    finally:
        await rdb.aclose()

    manifest = build_manifest(list(result.candidates), strategy=args.strategy, before=args.before)
    write_manifest(manifest, args.out)

    by_symbol: dict[str, int] = {}
    for c in result.candidates:
        by_symbol[c.symbol] = by_symbol.get(c.symbol, 0) + 1

    log.info(
        "legacy_paper_repair.classified",
        strategy=args.strategy,
        before=args.before.isoformat(),
        candidates=len(result.candidates),
        fingerprint=manifest.fingerprint,
        by_symbol=dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])),
        trade_ids=list(manifest.trade_ids),
        manifest_path=str(args.out),
    )

    if result.skipped:
        log.warning(
            "legacy_paper_repair.skipped_rows",
            count=len(result.skipped),
            rows=[
                {
                    "trade_id": s.trade_id,
                    "exchange": s.exchange,
                    "symbol": s.symbol,
                    "reason": s.reason,
                }
                for s in result.skipped
            ],
        )


async def _run_apply(args: argparse.Namespace) -> None:
    manifest = read_manifest(args.report)
    db_url = _db_url_from_env()
    rdb = _rdb_from_env()
    try:
        count = await apply_repair(db_url, rdb, manifest=manifest)
    finally:
        await rdb.aclose()
    log.info(
        "legacy_paper_repair.cli_applied", cancelled=count, manifest_size=len(manifest.trade_ids)
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        if args.command == "classify":
            asyncio.run(_run_classify(args))
        else:
            asyncio.run(_run_apply(args))
    except RepairAbortedError as exc:
        log.error("legacy_paper_repair.aborted", err=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
