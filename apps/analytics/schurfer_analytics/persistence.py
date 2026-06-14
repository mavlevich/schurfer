import json
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

_UPSERT = """
INSERT INTO app.pump_events (base, first_seen_at, last_seen_at, peak_pct, last_pct, exchanges)
VALUES (%s, NOW(), NOW(), %s, %s, %s::jsonb)
ON CONFLICT (base) DO UPDATE SET
    last_seen_at = NOW(),
    last_pct     = EXCLUDED.last_pct,
    exchanges    = EXCLUDED.exchanges,
    first_seen_at = CASE
        WHEN pump_events.last_seen_at < NOW() - INTERVAL '24 hours' THEN NOW()
        ELSE pump_events.first_seen_at
    END,
    peak_pct = CASE
        WHEN pump_events.last_seen_at < NOW() - INTERVAL '24 hours' THEN EXCLUDED.peak_pct
        ELSE GREATEST(pump_events.peak_pct, EXCLUDED.peak_pct)
    END
"""


def _high_24h_pct(ex: dict[str, Any]) -> float:
    """24h peak % via open reconstruction: open = price/(1+change_pct/100), peak = high/open-1."""
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
    """Best estimate of true 24h peak: max of current scan % and high_24h-derived %."""
    candidates: list[float] = [float(pump["max_change_pct"])]
    for ex in pump.get("exchanges", []):
        candidates.append(_high_24h_pct(ex))
    return max(candidates)


async def upsert_pumps(db_url: str, pumps: list[dict[str, Any]]) -> None:
    if not pumps:
        return
    rows = [
        (p["base"], _true_peak_pct(p), p["max_change_pct"], json.dumps(p["exchanges"]))
        for p in pumps
    ]
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.executemany(_UPSERT, rows)
        log.info("persistence.upserted", count=len(rows))
    except Exception as exc:
        log.warning("persistence.failed", err=str(exc))
