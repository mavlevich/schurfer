import json
import math
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

_SELECT_OPEN = """
SELECT id, peak_pct, entry_qualified_at
FROM app.pump_events
WHERE base = %s AND closed_at IS NULL
"""

_UPDATE_EPISODE = """
UPDATE app.pump_events
SET last_seen_at = %s,
    last_pct     = %s,
    exchanges    = %s::jsonb,
    peak_pct     = GREATEST(peak_pct, %s),
    entry_qualified_at = COALESCE(entry_qualified_at, %s),
    miss_count   = 0
WHERE id = %s
"""

# episode = max existing episode for this base + 1
_INSERT_EPISODE = """
INSERT INTO app.pump_events
    (base, episode, first_seen_at, entry_qualified_at,
     last_seen_at, peak_pct, last_pct, exchanges)
VALUES (
    %s,
    COALESCE((SELECT MAX(episode) FROM app.pump_events WHERE base = %s), 0) + 1,
    %s, %s, %s, %s, %s, %s::jsonb
)
RETURNING id
"""

_UPSERT_EVENT_SOURCE = """
INSERT INTO app.pump_event_sources (
    event_id, exchange, symbol,
    identity_key, market_id, unified_symbol, display_name,
    market_type, base_asset, quote_asset, settle_asset,
    contract_size, onboarded_at, first_ticker_at, last_ticker_at,
    first_change_pct, last_change_pct, peak_change_pct,
    first_price, last_price,
    first_volume_24h_usd, last_volume_24h_usd,
    first_seen_at, last_seen_at, observation_count
)
VALUES (
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s, 1
)
ON CONFLICT (event_id, exchange) DO UPDATE
SET identity_conflict = app.pump_event_sources.identity_conflict OR (
        (
            app.pump_event_sources.market_id IS NOT NULL
            AND EXCLUDED.market_id IS NOT NULL
            AND app.pump_event_sources.market_id <> EXCLUDED.market_id
        )
        OR (
            app.pump_event_sources.market_type IS NOT NULL
            AND EXCLUDED.market_type IS NOT NULL
            AND app.pump_event_sources.market_type <> EXCLUDED.market_type
        )
        OR (
            app.pump_event_sources.onboarded_at IS NOT NULL
            AND EXCLUDED.onboarded_at IS NOT NULL
            AND app.pump_event_sources.onboarded_at <> EXCLUDED.onboarded_at
        )
    ),
    identity_key       = CASE
        WHEN app.pump_event_sources.identity_key IS NULL
            THEN EXCLUDED.identity_key
        WHEN app.pump_event_sources.market_id = EXCLUDED.market_id
            AND app.pump_event_sources.market_type = EXCLUDED.market_type
            AND app.pump_event_sources.onboarded_at IS NULL
            AND EXCLUDED.onboarded_at IS NOT NULL
            THEN EXCLUDED.identity_key
        ELSE app.pump_event_sources.identity_key
    END,
    market_id          = COALESCE(app.pump_event_sources.market_id, EXCLUDED.market_id),
    unified_symbol     = COALESCE(
        app.pump_event_sources.unified_symbol,
        EXCLUDED.unified_symbol
    ),
    display_name       = COALESCE(
        app.pump_event_sources.display_name,
        EXCLUDED.display_name
    ),
    market_type        = COALESCE(app.pump_event_sources.market_type, EXCLUDED.market_type),
    base_asset         = COALESCE(app.pump_event_sources.base_asset, EXCLUDED.base_asset),
    quote_asset        = COALESCE(app.pump_event_sources.quote_asset, EXCLUDED.quote_asset),
    settle_asset       = COALESCE(app.pump_event_sources.settle_asset, EXCLUDED.settle_asset),
    contract_size      = COALESCE(
        app.pump_event_sources.contract_size,
        EXCLUDED.contract_size
    ),
    onboarded_at       = COALESCE(
        app.pump_event_sources.onboarded_at,
        EXCLUDED.onboarded_at
    ),
    first_ticker_at    = COALESCE(
        app.pump_event_sources.first_ticker_at,
        EXCLUDED.first_ticker_at
    ),
    last_ticker_at     = COALESCE(
        EXCLUDED.last_ticker_at,
        app.pump_event_sources.last_ticker_at
    ),
    last_seen_at       = EXCLUDED.last_seen_at,
    last_change_pct    = EXCLUDED.last_change_pct,
    peak_change_pct    = GREATEST(
        app.pump_event_sources.peak_change_pct,
        EXCLUDED.peak_change_pct
    ),
    last_price         = EXCLUDED.last_price,
    last_volume_24h_usd = EXCLUDED.last_volume_24h_usd,
    observation_count = app.pump_event_sources.observation_count + 1
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
    """Rolling 24h high % via open reconstruction."""
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


def _episode_24h_high_pct(pump: dict[str, Any]) -> float:
    """Highest exchange-derived rolling 24h high observed for this batch."""
    candidates: list[float] = [float(pump["max_change_pct"])]
    for ex in pump.get("exchanges", []):
        candidates.append(_high_24h_pct(ex))
    return max(candidates)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _datetime_ms(value: Any) -> datetime | None:
    parsed = _finite_float(value)
    if parsed is None or parsed < 0:
        return None
    try:
        return datetime.fromtimestamp(parsed / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _source_args(event_id: int, exchange: dict[str, Any]) -> tuple[Any, ...]:
    change_pct = _finite_float(exchange.get("change_pct"))
    if change_pct is None:
        raise ValueError("pump event source requires a finite change_pct")
    price = _finite_float(exchange.get("price"))
    volume = _finite_float(exchange.get("volume_24h_usd"))
    observed_at = _datetime_ms(exchange.get("observed_at_ms")) or datetime.now(UTC)
    return (
        event_id,
        str(exchange["exchange"]),
        str(exchange["symbol"]),
        exchange.get("identity_key"),
        exchange.get("market_id"),
        exchange.get("unified_symbol"),
        exchange.get("display_name"),
        exchange.get("market_type"),
        exchange.get("base_asset"),
        exchange.get("quote_asset"),
        exchange.get("settle_asset"),
        _finite_float(exchange.get("contract_size")),
        _datetime_ms(exchange.get("onboarded_at_ms")),
        _datetime_ms(exchange.get("ticker_timestamp_ms")),
        _datetime_ms(exchange.get("ticker_timestamp_ms")),
        change_pct,
        change_pct,
        change_pct,
        price,
        price,
        volume,
        volume,
        observed_at,
        observed_at,
    )


def _pump_observation_window(pump: dict[str, Any]) -> tuple[datetime, datetime]:
    """Return the earliest/latest venue observation in one scanner batch."""
    observations = [
        observed_at
        for exchange in pump.get("exchanges", [])
        if (observed_at := _datetime_ms(exchange.get("observed_at_ms"))) is not None
    ]
    if not observations:
        now = datetime.now(UTC)
        return now, now
    return min(observations), max(observations)


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


async def upsert_pumps(
    db_url: str,
    pumps: list[dict[str, Any]],
    entry_min_pct: float,
) -> dict[str, int]:
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
                rolling_high_pct = _episode_24h_high_pct(pump)
                last_pct = float(pump["max_change_pct"])
                exchanges_json = json.dumps(pump["exchanges"])
                first_observed_at, last_observed_at = _pump_observation_window(pump)
                entry_qualified_at = first_observed_at if last_pct >= entry_min_pct else None

                await cur.execute(_SELECT_OPEN, (base,))
                row = await cur.fetchone()

                if row:
                    event_id, _, _ = row
                    await cur.execute(
                        _UPDATE_EPISODE,
                        (
                            last_observed_at,
                            last_pct,
                            exchanges_json,
                            rolling_high_pct,
                            entry_qualified_at,
                            event_id,
                        ),
                    )
                else:
                    await cur.execute(
                        _INSERT_EPISODE,
                        (
                            base,
                            base,
                            first_observed_at,
                            entry_qualified_at,
                            last_observed_at,
                            rolling_high_pct,
                            last_pct,
                            exchanges_json,
                        ),
                    )
                    inserted = await cur.fetchone()
                    if inserted is None:
                        raise RuntimeError(f"pump event insert returned no id for {base}")
                    event_id = inserted[0]
                for exchange in pump["exchanges"]:
                    await cur.execute(_UPSERT_EVENT_SOURCE, _source_args(int(event_id), exchange))
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
