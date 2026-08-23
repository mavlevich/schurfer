"""Retroactive fees/funding backfill for closed paper trades still stuck on
`accounting_version='legacy_price_only_v1'` -- trades closed before
close_trade() started routing every paper trade through
calculate_performance().

Root cause (verified against production, 2026-08-23): 32 pump_short v1
trades, the earliest in the table (2026-07-17 to 2026-07-28), never got a
fees_usd/funding_usd figure computed at all -- fees_usd=funding_usd=0
permanently, accounting_status='legacy'. Every other closed trade already
has fees_usd/funding_usd computed via paper_conservative_costs_v1, even
when net PnL stays honestly unavailable (accounting_status='incomplete').

fees_usd/funding_usd are pure functions of size_usd/entry_price/exit_price/
side/duration -- already-stored columns, no live data needed -- so they can
be recomputed exactly, through the SAME calculate_performance() the live
close path uses. entry_slippage_bps can also be legitimately recovered from
setup_context's market_quality entry-side snapshot: accounting_contract()
already treats this as the correct entry-side source for every other trade
in the table, not a stand-in proxy.

exit_slippage_bps CANNOT be recovered, on purpose. journal.py's own
_SELECT_TRADE_FOR_CLOSE comment explains why: the entry-time snapshot's
opposite book side must never substitute for a fresh at-close capture --
that capture only ever happens live, at the moment of the real close.
Verified directly: all 32 candidate rows have exit_slippage_bps IS NULL on
the row itself (never captured, live or otherwise). So this backfill always
passes exit_slippage_bps=None, and every row lands on
accounting_status='incomplete' -- the same honest ceiling the other 183
already-incomplete trades sit at. Never 'complete', even for the rows that
do have a captured market_quality snapshot; mixing a stale-proxy exit
slippage into a 'complete' row would silently misrepresent confidence
relative to every other genuinely 'complete' row in the table.

Never trusts gross_pnl_usd/gross_pnl_pct blindly: recomputes them from the
same stored inputs purely to verify they already agree with what
close_trade()'s legacy branch wrote at close time. Any disagreement aborts
that one row for manual review rather than silently overwriting it.

Same two-step, manifest-based classify/apply workflow as
legacy_paper_repair.py. No Redis, no exchange client -- this is a pure
function of already-stored columns, so classify is fully deterministic and
apply re-verifies by re-running classify and requiring an exact match
(trade-id set AND every computed field), not just an id-set match.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row
from schurfer_performance import (
    LEGACY_ACCOUNTING_VERSION,
    PAPER_ACCOUNTING_VERSION,
    calculate_performance,
)

from .journal import accounting_contract

log = structlog.get_logger()


def _db_url_from_env() -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL is required")
    return db_url


class BackfillAbortedError(Exception):
    """Raised whenever apply refuses to touch the database at all -- live
    reclassification drifted from the manifest, or rows are in an
    unexpected/partial state. Never partially apply."""


@dataclass(frozen=True)
class BackfillCandidate:
    trade_id: int
    symbol: str
    exchange: str
    side: str
    size_usd: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    fees_usd: float
    funding_usd: float
    net_pnl_usd: float | None
    net_pnl_pct: float | None
    accounting_status: str
    accounting_error: str | None


@dataclass(frozen=True)
class SkippedRow:
    """A candidate row this classifier could not confidently backfill.
    Never silently included -- surfaced for a human to check by hand."""

    trade_id: int
    symbol: str
    exchange: str
    reason: str


@dataclass(frozen=True)
class ClassifyResult:
    candidates: tuple[BackfillCandidate, ...]
    skipped: tuple[SkippedRow, ...]


@dataclass(frozen=True)
class Manifest:
    generated_at: datetime
    trade_ids: tuple[int, ...]  # sorted, the frozen set apply must reproduce
    fingerprint: str  # hashes every candidate's computed fields, not just ids
    candidates: tuple[BackfillCandidate, ...]  # sorted by trade_id


_SELECT_LEGACY_ACCOUNTING_CANDIDATES = """
SELECT id, symbol, exchange, side, size_usd, entry_price, exit_price,
       entry_at, exit_at, setup_context, gross_pnl_usd, gross_pnl_pct,
       exit_slippage_bps
FROM app.trades
WHERE status = 'closed'
  AND accounting_version = %(legacy_version)s
ORDER BY id
"""


def _to_candidate_and_check(row: dict[str, Any]) -> BackfillCandidate | SkippedRow:
    trade_id, symbol, exchange = row["id"], row["symbol"], row["exchange"]

    if row["exit_price"] is None or row["exit_at"] is None:
        return SkippedRow(trade_id, symbol, exchange, "closed trade missing exit_price/exit_at")

    setup_context = row["setup_context"] or {}
    if setup_context.get("paper") is not True:
        return SkippedRow(
            trade_id,
            symbol,
            exchange,
            "setup_context.paper is not true -- this backfill only covers paper accounting",
        )

    # exit_slippage_bps stored on the row itself is the only place a genuine
    # at-close capture could live; if it were somehow present here despite
    # accounting_version still being legacy, that would be an inconsistent
    # state worth a human's eyes, not silently reused or silently ignored.
    if row["exit_slippage_bps"] is not None:
        return SkippedRow(
            trade_id,
            symbol,
            exchange,
            "row already has a captured exit_slippage_bps despite legacy accounting_version "
            "-- inconsistent state, needs manual review before backfilling",
        )

    side = row["side"]
    size_usd = float(row["size_usd"])
    entry_price = float(row["entry_price"])
    exit_price = float(row["exit_price"])
    entry_at, exit_at = row["entry_at"], row["exit_at"]
    duration_minutes = max(0.0, (exit_at - entry_at).total_seconds() / 60)

    _, _, entry_slippage_bps, _ = accounting_contract(setup_context, side=side)
    # exit_slippage_bps is deliberately always None -- see module docstring.
    accounting = calculate_performance(
        position_usd=size_usd,
        entry_price=entry_price,
        exit_price=exit_price,
        side=side,
        duration_minutes=duration_minutes,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=None,
    )
    if accounting.status != "incomplete":
        # Cannot happen given exit_slippage_bps=None above, but a silent
        # future change to calculate_performance's branching must not
        # silently promote one of these rows to 'complete' via this path.
        return SkippedRow(
            trade_id,
            symbol,
            exchange,
            f"unexpected accounting status {accounting.status!r} for exit_slippage_bps=None",
        )

    stored_gross_usd = float(row["gross_pnl_usd"]) if row["gross_pnl_usd"] is not None else None
    stored_gross_pct = float(row["gross_pnl_pct"]) if row["gross_pnl_pct"] is not None else None
    if (
        stored_gross_usd is None
        or stored_gross_pct is None
        or not math.isclose(accounting.gross_pnl_usd, stored_gross_usd, rel_tol=1e-3, abs_tol=0.01)
        or not math.isclose(
            accounting.gross_return_pct, stored_gross_pct, rel_tol=1e-3, abs_tol=0.01
        )
    ):
        return SkippedRow(
            trade_id,
            symbol,
            exchange,
            f"recomputed gross ({accounting.gross_pnl_usd:.4f} usd, "
            f"{accounting.gross_return_pct:.4f}%) disagrees with stored gross "
            f"({stored_gross_usd} usd, {stored_gross_pct}%) -- needs manual review",
        )

    return BackfillCandidate(
        trade_id=trade_id,
        symbol=symbol,
        exchange=exchange,
        side=side,
        size_usd=size_usd,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_at=entry_at,
        exit_at=exit_at,
        fees_usd=round(accounting.fees_usd, 4),
        funding_usd=round(accounting.funding_usd, 4),
        # Always None in practice (exit_slippage_bps=None forces status=
        # "incomplete" above), taken from the real result rather than
        # hardcoded so this stays correct if calculate_performance's
        # branching ever changes.
        net_pnl_usd=round(accounting.net_pnl_usd, 4)
        if accounting.net_pnl_usd is not None
        else None,
        net_pnl_pct=(
            round(accounting.net_return_pct, 4) if accounting.net_return_pct is not None else None
        ),
        accounting_status=accounting.status,
        accounting_error=accounting.error,
    )


async def classify_legacy_accounting(db_url: str) -> ClassifyResult:
    """Read-only, deterministic, no Redis/exchange dependency."""
    async with (
        await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
        aconn.cursor() as cur,
    ):
        await cur.execute(
            _SELECT_LEGACY_ACCOUNTING_CANDIDATES, {"legacy_version": LEGACY_ACCOUNTING_VERSION}
        )
        rows = await cur.fetchall()

    candidates: list[BackfillCandidate] = []
    skipped: list[SkippedRow] = []
    for row in rows:
        result = _to_candidate_and_check(row)
        if isinstance(result, BackfillCandidate):
            candidates.append(result)
        else:
            skipped.append(result)

    return ClassifyResult(candidates=tuple(candidates), skipped=tuple(skipped))


def _candidate_fingerprint_payload(c: BackfillCandidate) -> dict[str, Any]:
    payload = asdict(c)
    payload["entry_at"] = c.entry_at.isoformat()
    payload["exit_at"] = c.exit_at.isoformat()
    return payload


def _fingerprint(candidates: tuple[BackfillCandidate, ...]) -> str:
    payload = [_candidate_fingerprint_payload(c) for c in candidates]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def build_manifest(candidates: list[BackfillCandidate]) -> Manifest:
    sorted_candidates = tuple(sorted(candidates, key=lambda c: c.trade_id))
    return Manifest(
        generated_at=datetime.now(tz=UTC),
        trade_ids=tuple(c.trade_id for c in sorted_candidates),
        fingerprint=_fingerprint(sorted_candidates),
        candidates=sorted_candidates,
    )


def write_manifest(manifest: Manifest, path: Path) -> None:
    payload = {
        "generated_at": manifest.generated_at.isoformat(),
        "trade_ids": list(manifest.trade_ids),
        "fingerprint": manifest.fingerprint,
        "candidates": [_candidate_fingerprint_payload(c) for c in manifest.candidates],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def read_manifest(path: Path) -> Manifest:
    data = json.loads(path.read_text())
    candidates = tuple(
        BackfillCandidate(
            trade_id=c["trade_id"],
            symbol=c["symbol"],
            exchange=c["exchange"],
            side=c["side"],
            size_usd=c["size_usd"],
            entry_price=c["entry_price"],
            exit_price=c["exit_price"],
            entry_at=datetime.fromisoformat(c["entry_at"]),
            exit_at=datetime.fromisoformat(c["exit_at"]),
            fees_usd=c["fees_usd"],
            funding_usd=c["funding_usd"],
            net_pnl_usd=c["net_pnl_usd"],
            net_pnl_pct=c["net_pnl_pct"],
            accounting_status=c["accounting_status"],
            accounting_error=c["accounting_error"],
        )
        for c in data["candidates"]
    )
    return Manifest(
        generated_at=datetime.fromisoformat(data["generated_at"]),
        trade_ids=tuple(data["trade_ids"]),
        fingerprint=data["fingerprint"],
        candidates=candidates,
    )


_SELECT_CURRENT_STATE = """
SELECT id, status, accounting_version FROM app.trades WHERE id = ANY(%(ids)s)
"""

_APPLY_BACKFILL = """
UPDATE app.trades
SET fees_usd = %(fees_usd)s,
    funding_usd = %(funding_usd)s,
    net_pnl_usd = %(net_pnl_usd)s,
    net_pnl_pct = %(net_pnl_pct)s,
    pnl_usd = %(net_pnl_usd)s,
    pnl_pct = %(net_pnl_pct)s,
    accounting_version = %(accounting_version)s,
    accounting_status = %(accounting_status)s,
    accounting_error = %(accounting_error)s,
    updated_at = now()
WHERE id = %(trade_id)s
  AND status = 'closed'
  AND accounting_version = %(legacy_version)s
"""


async def apply_backfill(db_url: str, *, manifest: Manifest) -> int:
    """Re-verifies by re-running classify_legacy_accounting live and
    requiring an EXACT match against the frozen manifest -- same id set AND
    every computed field, not just ids. Idempotent: re-applying an
    already-applied manifest is a no-op. Never partially applies -- any
    drift or mixed state aborts for manual review."""
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
        raise BackfillAbortedError(f"trade ids from manifest no longer exist: {sorted(missing)}")

    already_applied = {
        tid
        for tid, row in current.items()
        if row["status"] == "closed" and row["accounting_version"] == PAPER_ACCOUNTING_VERSION
    }
    still_legacy = {
        tid
        for tid, row in current.items()
        if row["status"] == "closed" and row["accounting_version"] == LEGACY_ACCOUNTING_VERSION
    }
    unexpected = set(current) - already_applied - still_legacy
    if unexpected:
        raise BackfillAbortedError(
            f"trade ids in an unexpected state, needs manual review: {sorted(unexpected)}"
        )
    if already_applied == set(manifest.trade_ids):
        log.info("legacy_accounting_backfill.already_applied", count=len(already_applied))
        return 0
    if already_applied:
        raise BackfillAbortedError(
            f"partial apply detected -- {len(already_applied)} already backfilled, "
            f"{len(still_legacy)} still legacy. Needs manual review, not auto-continuing: "
            f"already_applied={sorted(already_applied)} still_legacy={sorted(still_legacy)}"
        )

    # Live reclassification must reproduce the frozen manifest exactly --
    # every manifest id must still classify as a candidate today AND every
    # candidate's computed fields must still fingerprint identically.
    fresh = await classify_legacy_accounting(db_url)
    fresh_ids = {c.trade_id for c in fresh.candidates}
    manifest_ids = set(manifest.trade_ids)
    fresh_relevant = tuple(
        sorted(
            (c for c in fresh.candidates if c.trade_id in manifest_ids), key=lambda c: c.trade_id
        )
    )
    fresh_fingerprint = _fingerprint(fresh_relevant)
    if not manifest_ids.issubset(fresh_ids) or fresh_fingerprint != manifest.fingerprint:
        raise BackfillAbortedError(
            "live reclassification no longer matches the manifest -- "
            f"manifest had {manifest.trade_ids}, still-classifiable now: "
            f"{sorted(manifest_ids & fresh_ids)}. "
            "Re-run classify and review the diff before applying."
        )

    async with (
        await psycopg.AsyncConnection.connect(db_url) as write_conn,
        write_conn.cursor() as cur,
    ):
        count = 0
        for c in manifest.candidates:
            await cur.execute(
                _APPLY_BACKFILL,
                {
                    "trade_id": c.trade_id,
                    "fees_usd": c.fees_usd,
                    "funding_usd": c.funding_usd,
                    "net_pnl_usd": c.net_pnl_usd,
                    "net_pnl_pct": c.net_pnl_pct,
                    "accounting_version": PAPER_ACCOUNTING_VERSION,
                    "accounting_status": c.accounting_status,
                    "accounting_error": c.accounting_error,
                    "legacy_version": LEGACY_ACCOUNTING_VERSION,
                },
            )
            count += cur.rowcount
        if count != len(manifest.candidates):
            raise BackfillAbortedError(
                f"expected to update {len(manifest.candidates)} rows, actually updated {count} "
                "-- aborting the whole transaction, nothing committed"
            )
    log.info("legacy_accounting_backfill.applied", count=count, trade_ids=list(manifest.trade_ids))
    return count


# --- CLI -------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    classify_parser = sub.add_parser("classify", help="Read-only: write a manifest of candidates")
    classify_parser.add_argument("--out", required=True, type=Path)

    apply_parser = sub.add_parser(
        "apply", help="Re-verify and backfill exactly the manifest's rows"
    )
    apply_parser.add_argument("--report", required=True, type=Path)

    return parser


async def _run_classify(args: argparse.Namespace) -> None:
    db_url = _db_url_from_env()
    result = await classify_legacy_accounting(db_url)

    manifest = build_manifest(list(result.candidates))
    write_manifest(manifest, args.out)

    log.info(
        "legacy_accounting_backfill.classified",
        candidates=len(result.candidates),
        fingerprint=manifest.fingerprint,
        trade_ids=list(manifest.trade_ids),
        manifest_path=str(args.out),
    )
    if result.skipped:
        log.warning(
            "legacy_accounting_backfill.skipped_rows",
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
    count = await apply_backfill(db_url, manifest=manifest)
    log.info(
        "legacy_accounting_backfill.cli_applied",
        updated=count,
        manifest_size=len(manifest.trade_ids),
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        if args.command == "classify":
            asyncio.run(_run_classify(args))
        else:
            asyncio.run(_run_apply(args))
    except BackfillAbortedError as exc:
        log.error("legacy_accounting_backfill.aborted", err=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
