"""Price snapshot scheduler: records token price at +1h, +4h, +24h after first detection."""

import json
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

OFFSETS: list[tuple[str, int]] = [
    ("+1h", 3600),
    ("+4h", 14400),
    ("+24h", 86400),
]

# Events due for a snapshot at a given offset that don't have one yet
_SELECT_DUE = """
SELECT e.id, e.base, e.exchanges
FROM app.pump_events e
WHERE e.first_seen_at <= NOW() - (%s * INTERVAL '1 second')
  AND NOT EXISTS (
      SELECT 1 FROM app.pump_event_snapshots s
      WHERE s.event_id = e.id AND s.offset_label = %s
  )
"""

_INSERT_SNAPSHOT = """
INSERT INTO app.pump_event_snapshots
    (event_id, offset_label, recorded_at, price, change_pct, exchanges)
VALUES (%s, %s, NOW(), %s, %s, %s::jsonb)
ON CONFLICT (event_id, offset_label) DO NOTHING
"""


def _extract_price(exchanges: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Return (price, change_pct) from the best exchange in the stored snapshot."""
    for ex in exchanges:
        try:
            price = float(ex["price"])
            change_pct = float(ex["change_pct"])
            if price > 0:
                return price, change_pct
        except (KeyError, ValueError):
            continue
    return None, None


async def take_due_snapshots(db_url: str) -> None:
    """Check for events due a snapshot and record them using the last known price.

    Uses the exchanges JSONB already stored in pump_events (from the last scanner
    update) rather than making live API calls — keeps this dependency-free and fast.
    Snapshots recorded this way reflect the last-seen price, not necessarily the
    exact price at the offset boundary; accuracy improves with shorter scan intervals.
    """
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            for label, seconds in OFFSETS:
                await cur.execute(_SELECT_DUE, (seconds, label))
                due: list[tuple[int, str, list[dict[str, Any]]]] = await cur.fetchall()

                for event_id, base, exchanges in due:
                    price, change_pct = _extract_price(exchanges)
                    await cur.execute(
                        _INSERT_SNAPSHOT,
                        (
                            event_id,
                            label,
                            price,
                            change_pct,
                            json.dumps(exchanges),
                        ),
                    )
                    log.info(
                        "snapshot.recorded",
                        base=base,
                        offset=label,
                        price=price,
                        change_pct=change_pct,
                    )
    except Exception as exc:
        log.warning("snapshots.failed", err=str(exc))
