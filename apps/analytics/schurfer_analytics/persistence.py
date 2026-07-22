import json
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

_SELECT_OPEN = """
SELECT id, peak_pct
FROM app.pump_events
WHERE base = %s AND closed_at IS NULL
"""

_UPDATE_EPISODE = """
UPDATE app.pump_events
SET last_seen_at = NOW(),
    last_pct     = %s,
    exchanges    = %s::jsonb,
    peak_pct     = GREATEST(peak_pct, %s),
    miss_count   = 0
WHERE id = %s
"""

# episode = max existing episode for this base + 1
_INSERT_EPISODE = """
INSERT INTO app.pump_events
    (base, episode, first_seen_at, last_seen_at, peak_pct, last_pct, exchanges)
VALUES (
    %s,
    COALESCE((SELECT MAX(episode) FROM app.pump_events WHERE base = %s), 0) + 1,
    NOW(), NOW(), %s, %s, %s::jsonb
)
RETURNING id
"""

_SELECT_OPEN_ALL = """
SELECT id, base, last_pct, peak_pct
FROM app.pump_events
WHERE closed_at IS NULL
"""

# Increment miss counter for a single episode that disappeared from the live scan
_INCREMENT_MISS = """
UPDATE app.pump_events
SET miss_count = miss_count + 1
WHERE id = %s
"""

# Close all episodes whose miss counter has reached the threshold.
# retrace_pct = pct pts given back from peak (negative = pullback from peak)
_CLOSE_DUE = """
UPDATE app.pump_events
SET closed_at   = NOW(),
    retrace_pct = last_pct - peak_pct
WHERE closed_at IS NULL AND miss_count >= %s
RETURNING base, miss_count
"""


def _high_24h_pct(ex: dict[str, Any]) -> float:
    """24h peak % via open reconstruction: open = price/(1+change_pct/100)."""
    try:
        price = float(ex["price"])
        change_pct = float(ex["change_pct"])
        high_24h = float(ex["high_24h"])
        if price <= 0 or high_24h <= 0 or change_pct <= -100:
            return 0.0
        open_24h = price / (1 + change_pct / 100)
        return round((high_24h / open_24h - 1) * 100, 2)
    except (ValueError, ZeroDivisionError, KeyError):
        return 0.0


def _true_peak_pct(pump: dict[str, Any]) -> float:
    """Best estimate of true 24h peak: max of current % and high_24h-derived %."""
    candidates: list[float] = [float(pump["max_change_pct"])]
    for ex in pump.get("exchanges", []):
        candidates.append(_high_24h_pct(ex))
    return max(candidates)


_UPDATE_LAST_PCT = (
    "UPDATE app.pump_events SET last_pct = %s "
    "WHERE base = %s AND closed_at IS NULL AND last_seen_at > NOW() - INTERVAL '24 hours'"
)

_INSERT_OI_SNAPSHOT = (
    "INSERT INTO app.oi_snapshots (event_id, base, exchange, oi_usd, recorded_at) "
    "VALUES (%s, %s, %s, %s, NOW())"
)

_INSERT_FR_SNAPSHOT = (
    "INSERT INTO app.funding_rate_snapshots (event_id, base, exchange, rate, recorded_at) "
    "VALUES (%s, %s, %s, %s, NOW())"
)

_SELECT_OPEN_EPISODE_IDS = (
    "SELECT base, id FROM app.pump_events WHERE base = ANY(%s) AND closed_at IS NULL"
)


async def get_tracked_bases(db_url: str) -> frozenset[str]:
    """Return bases that have an active (still-open) pump_event in the last 24h."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT base FROM app.pump_events "
                "WHERE closed_at IS NULL AND last_seen_at > NOW() - INTERVAL '24 hours'"
            )
            rows = await cur.fetchall()
        return frozenset(r[0] for r in rows)
    except Exception as exc:
        log.warning("persistence.get_tracked.failed", err=str(exc))
        return frozenset()


async def update_last_pct(db_url: str, updates: dict[str, float]) -> None:
    """Update only last_pct for tracked tokens that dropped below threshold."""
    if not updates:
        return
    rows = [(pct, base) for base, pct in updates.items()]
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.executemany(_UPDATE_LAST_PCT, rows)
        log.info("persistence.updated_last_pct", count=len(rows))
    except Exception as exc:
        log.warning("persistence.update_last_pct.failed", err=str(exc))


async def get_open_episode_ids(db_url: str, bases: set[str]) -> dict[str, int]:
    """Map base -> id of its currently open pump episode, for the given bases."""
    if not bases:
        return {}
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_OPEN_EPISODE_IDS, (list(bases),))
            rows = await cur.fetchall()
        return {base: event_id for base, event_id in rows}
    except Exception as exc:
        log.warning("persistence.get_open_episode_ids_failed", err=str(exc))
        return {}


async def insert_oi_snapshots(db_url: str, snapshots: list[dict[str, Any]]) -> None:
    """Insert one OI snapshot row per (base, exchange), scoped to its pump episode.

    Snapshots without a resolved event_id are dropped — without an open episode
    there is nothing to compare the OI delta against, so storing them would
    mix data across unrelated episodes for the same token.
    """
    rows = [
        (s["event_id"], s["base"], s["exchange"], s["oi_usd"])
        for s in snapshots
        if s.get("event_id") is not None
    ]
    if not rows:
        return
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.executemany(_INSERT_OI_SNAPSHOT, rows)
        log.info("persistence.oi_snapshots_inserted", count=len(rows))
    except Exception as exc:
        log.warning("persistence.insert_oi_snapshots_failed", err=str(exc))


async def insert_funding_rate_snapshots(db_url: str, snapshots: list[dict[str, Any]]) -> None:
    """Insert one funding rate snapshot row per (base, exchange), scoped to its pump episode.

    Snapshots without a resolved event_id are dropped — same rationale as OI snapshots.
    """
    rows = [
        (s["event_id"], s["base"], s["exchange"], s["rate"])
        for s in snapshots
        if s.get("event_id") is not None
    ]
    if not rows:
        return
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.executemany(_INSERT_FR_SNAPSHOT, rows)
        log.info("persistence.fr_snapshots_inserted", count=len(rows))
    except Exception as exc:
        log.warning("persistence.insert_fr_snapshots_failed", err=str(exc))


async def upsert_pumps(db_url: str, pumps: list[dict[str, Any]]) -> dict[str, int]:
    """Persist every live pump and return its open episode id.

    The mapping is returned only after the transaction commits. An empty mapping for a
    non-empty input means persistence failed, so the caller must not publish a new Redis
    snapshot whose pumps cannot yet be scored or attributed.
    """
    if not pumps:
        return {}
    episode_ids: dict[str, int] = {}
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            for pump in pumps:
                base = pump["base"]
                peak = _true_peak_pct(pump)
                last_pct = float(pump["max_change_pct"])
                exchanges_json = json.dumps(pump["exchanges"])

                await cur.execute(_SELECT_OPEN, (base,))
                row = await cur.fetchone()

                if row:
                    event_id, _ = row
                    await cur.execute(_UPDATE_EPISODE, (last_pct, exchanges_json, peak, event_id))
                else:
                    await cur.execute(_INSERT_EPISODE, (base, base, peak, last_pct, exchanges_json))
                    inserted = await cur.fetchone()
                    if inserted is None:
                        raise RuntimeError(f"pump event insert returned no id for {base}")
                    event_id = inserted[0]
                episode_ids[base] = int(event_id)
        log.info("persistence.upserted", count=len(pumps))
        return episode_ids
    except Exception as exc:
        log.warning("persistence.upsert_failed", err=str(exc))
        return {}


async def close_retrace(db_url: str, live_bases: set[str], close_after_misses: int = 3) -> None:
    """Increment miss_count for absent episodes; close those that exceed the threshold.

    Episodes that disappear from the live scan for close_after_misses consecutive
    scans are closed. A single data gap or exchange hiccup will not close an episode.
    """
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_OPEN_ALL)
            open_events: list[tuple[int, str, float, float]] = await cur.fetchall()

            for event_id, base, _last_pct, _peak_pct in open_events:
                if base not in live_bases:
                    await cur.execute(_INCREMENT_MISS, (event_id,))

            await cur.execute(_CLOSE_DUE, (close_after_misses,))
            closed = await cur.fetchall()
            for base, miss_count in closed:
                log.info("persistence.episode_closed", base=base, after_misses=miss_count)
            if closed:
                log.info("persistence.retrace_total", closed=len(closed))
    except Exception as exc:
        log.warning("persistence.retrace_failed", err=str(exc))
