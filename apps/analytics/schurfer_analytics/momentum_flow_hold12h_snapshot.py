"""HYP-015 hold12h formal-read input snapshot and write-once files.

The formal verdict is computed from a SNAPSHOT of its inputs, never from live rows: the
WATCH denominator, every probe with every field, and the raw funding settlements and
coverage runs of each route. The snapshot is serialized canonically, so its sha256 pins
every input by construction (no hand-picked field list to fall out of date). It is
written once and pinned in the claim BEFORE the verdict is computed; a resumed attempt
recomputes from the same bytes.

The write helpers publish a file whole or not at all: bytes go to a unique temp file,
are fsynced, and are hard-linked to the final name (which fails if the name exists);
the directory is then fsynced, so a power loss cannot leave a claim pointing at a file
that is not on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .momentum_flow_hold12h_funding import StoredFundingSource
from .momentum_flow_hold12h_verdict_report import (
    HorizonOutcome,
    InstrumentRoute,
    ProbeRecord,
    WatchDecision,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

SNAPSHOT_VERSION = "hold12h_formal_inputs_snapshot_v1"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _route(route: InstrumentRoute) -> list[str]:
    return [route.exchange, route.market_type, route.market_id, route.unified_symbol]


def _probe(probe: ProbeRecord) -> dict[str, Any]:
    return {
        "watch_id": probe.watch_id,
        "route": _route(probe.route),
        "entry_at": _iso(probe.entry_at),
        "entry_ok": probe.entry_ok,
        "exit_at": _iso(probe.exit_at),
        "exit_resolved": probe.exit_resolved,
        "exit_reason": probe.exit_reason,
        "actual_gross_return_pct": probe.actual_gross_return_pct,
        "actual_notional_usd": probe.actual_notional_usd,
        "max_adverse_return_pct": probe.max_adverse_return_pct,
        "horizons": [
            [h.horizon_minutes, h.resolved, _iso(h.observed_at), h.gross_return_pct, h.notional_usd]
            for h in sorted(probe.horizons.values(), key=lambda h: h.horizon_minutes)
        ],
    }


def holdings_window(probes: dict[str, ProbeRecord]) -> tuple[datetime, datetime] | None:
    """The span of every interval the verdict can ask funding for: the earliest entry to
    the latest exit or horizon observation. None when there is no probe."""
    starts = [p.entry_at for p in probes.values()]
    ends = [
        t
        for p in probes.values()
        for t in (p.exit_at, *(h.observed_at for h in p.horizons.values()))
        if t is not None
    ]
    if not starts:
        return None
    return min(starts), max([*ends, *starts])


def snapshot_bytes(
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: StoredFundingSource,
    *,
    window: tuple[datetime, datetime] | None,
) -> bytes:
    """Canonical bytes of every verdict input. Funding keeps only what overlaps
    ``window``; every interval the verdict evaluates lies inside it."""
    payload = {
        "version": SNAPSHOT_VERSION,
        "watches": [
            [w.watch_id, w.canonical_asset, _iso(w.decision_at)]
            for w in sorted(watches, key=lambda w: w.watch_id)
        ],
        "probes": [_probe(probes[k]) for k in sorted(probes)],
        "funding": funding.to_payload(window),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def snapshot_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def inputs_from_snapshot(
    body: bytes,
) -> tuple[tuple[WatchDecision, ...], dict[str, ProbeRecord], StoredFundingSource]:
    payload = json.loads(body)
    if payload.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"unknown snapshot version {payload.get('version')!r}")
    watches = tuple(
        WatchDecision(watch_id=w, canonical_asset=a, decision_at=_dt(d))  # type: ignore[arg-type]
        for w, a, d in payload["watches"]
    )
    probes: dict[str, ProbeRecord] = {}
    for row in payload["probes"]:
        probes[row["watch_id"]] = ProbeRecord(
            watch_id=row["watch_id"],
            route=InstrumentRoute(*row["route"]),
            entry_at=_dt(row["entry_at"]),  # type: ignore[arg-type]
            entry_ok=row["entry_ok"],
            exit_at=_dt(row["exit_at"]),
            exit_resolved=row["exit_resolved"],
            exit_reason=row["exit_reason"],
            actual_gross_return_pct=row["actual_gross_return_pct"],
            actual_notional_usd=row["actual_notional_usd"],
            max_adverse_return_pct=row["max_adverse_return_pct"],
            horizons={
                m: HorizonOutcome(m, resolved, _dt(observed), gross, notional)
                for m, resolved, observed, gross, notional in row["horizons"]
            },
        )
    return watches, probes, StoredFundingSource.from_payload(payload["funding"])


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_once(path: Path, body: bytes) -> None:
    """Durable and write-once: the whole file or no file, never an overwrite."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
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


def publish_once_or_same(path: Path, body: bytes) -> None:
    """Like ``publish_once``, but an existing file with the same bytes is accepted (a
    resumed attempt re-publishing what an earlier one already wrote)."""
    try:
        publish_once(path, body)
    except FileExistsError:
        if path.read_bytes() != body:
            raise
