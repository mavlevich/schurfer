"""Exit-bar-end order book capture for HYP-012 v4 episodes (diagnostic only).

The registered v2 verdict prices the exit as the close of the 1-minute bar that
opens at ``ceil_minute(entry_at + 30m)`` plus an assumed 15 bps slippage. This
worker samples the real Bybit book of the entered instrument at the moment that
close refers to (the end of that bar), so the proxy can later be calibrated
against a book taken at the same instant. It never computes a return and the v2
verdict never reads its table (``app.source_lead_exit_observations``).

Per qualified v4 episode:

1. **Claim before the request.** A row is inserted as ``claimed`` (unique on
   capture and qualification version). If the process dies between request and
   write, the row stays ``claimed`` and is marked ``crashed_after_claim`` on the
   next start; it is never re-requested, so a crash cannot turn into a later,
   different quote.
2. **Instrument from the entry, not the ticker.** The native symbol is parsed
   from the entered observation's registered ``identity_key``.
3. **Hypothetical quantity.** No fill exists, so the quantity is
   ``requested_notional_usd / ask_vwap`` in base units (Bybit linear contracts
   are one base unit each), rounded down to the instrument's ``qtyStep``. Both
   the raw and the rounded quantity are stored.
4. **Raw book.** The v5 REST book (up to 50 levels a side) is stored with its
   ``ts``, ``cts``, ``seq`` and ``u``, the request and receive times, and a
   SHA-256 of the stored snapshot.
5. **Attempt outcome and timeliness are separate.** Timeliness is by receive
   time against the target: up to 30 s ``on_time``, up to 120 s ``late``,
   otherwise ``missed``. A failed fetch is retried inside the window, and
   ``fetch_failed`` is the result only after the window is spent. Book
   freshness is judged separately (``stale_book``: older than 2000 ms or more
   than 1000 ms ahead), with the same limits qualification v4 uses at entry.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog

from .ohlcv import ONE_MINUTE_MS, ceil_to_timeframe
from .source_lead_contract import IDENTITY_REGISTRY_V4_START
from .source_lead_qualification import (
    MAX_TARGET_BOOK_AGE_MS,
    MAX_TARGET_BOOK_CLOCK_SKEW_MS,
    QUALIFICATION_VERSION,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger()

EXIT_VERSION = "source_lead_exit_book_v1"
HEALTH_KEY = f"market:sourceleadexit:health:{EXIT_VERSION}"
OUTCOME_HORIZON = timedelta(minutes=30)
ON_TIME_LIMIT = timedelta(seconds=30)
LATE_LIMIT = timedelta(seconds=120)
RETRY_SECONDS = 5.0
POLL_SECONDS = 2.0
BOOK_LEVELS = 50
BYBIT_BOOK_URL = "https://api.bybit.com/v5/market/orderbook"
BYBIT_INSTRUMENT_URL = "https://api.bybit.com/v5/market/instruments-info"
SUPPORTED_VENUES = ("bybit",)


# --- pure ------------------------------------------------------------------------


def exit_target_at(entry_at: datetime) -> datetime:
    """End of the v2 exit bar: the instant its OHLCV close refers to."""
    entry_ms = round(entry_at.timestamp() * 1000)
    boundary_ms = ceil_to_timeframe(
        entry_ms + int(OUTCOME_HORIZON.total_seconds() * 1000), ONE_MINUTE_MS
    )
    return datetime.fromtimestamp((boundary_ms + ONE_MINUTE_MS) / 1000, tz=UTC)


def native_symbol(identity_key: Any) -> str | None:
    """`bybit:swap:ABCUSDT:1700000000000` -> `ABCUSDT`."""
    if not isinstance(identity_key, str):
        return None
    parts = identity_key.split(":")
    if len(parts) != 4 or parts[0] != "bybit" or parts[1] != "swap" or not parts[2]:
        return None
    return parts[2]


def hypothetical_quantity(
    notional_usd: Decimal, ask_vwap: Decimal, qty_step: Decimal
) -> tuple[Decimal, Decimal]:
    """(raw, rounded down to qty_step) base quantity bought at entry."""
    if notional_usd <= 0 or ask_vwap <= 0 or qty_step <= 0:
        raise ValueError("notional, ask_vwap and qty_step must be positive")
    raw = notional_usd / ask_vwap
    steps = (raw / qty_step).to_integral_value(rounding=ROUND_FLOOR)
    return raw, steps * qty_step


def _levels(side: Any) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    for level in side if isinstance(side, list) else []:
        try:
            price, size = Decimal(str(level[0])), Decimal(str(level[1]))
        except (ArithmeticError, IndexError, TypeError, ValueError):
            continue
        if price.is_finite() and size.is_finite() and price > 0 and size > 0:
            out.append((price, size))
    return out


@dataclass(frozen=True)
class BookSummary:
    best_bid: Decimal
    best_ask: Decimal
    bid_vwap: Decimal | None
    bid_filled_qty: Decimal
    spread_bps: Decimal
    impact_bps: Decimal | None


def summarize_exit_book(bids: Any, asks: Any, quantity: Decimal) -> BookSummary:
    """Sell `quantity` into the bids. Impact is against the mid, in bps."""
    bid_levels, ask_levels = _levels(bids), _levels(asks)
    if not bid_levels or not ask_levels:
        raise ValueError("empty book side")
    best_bid, best_ask = bid_levels[0][0], ask_levels[0][0]
    if best_ask < best_bid:
        raise ValueError("crossed book")
    mid = (best_bid + best_ask) / 2
    filled = Decimal(0)
    proceeds = Decimal(0)
    for price, size in bid_levels:
        take = min(size, quantity - filled)
        if take <= 0:
            break
        filled += take
        proceeds += take * price
    vwap = proceeds / filled if filled >= quantity and filled > 0 else None
    impact = (mid - vwap) / mid * 10_000 if vwap is not None else None
    return BookSummary(
        best_bid=best_bid,
        best_ask=best_ask,
        bid_vwap=vwap,
        bid_filled_qty=filled,
        spread_bps=(best_ask - best_bid) / mid * 10_000,
        impact_bps=impact,
    )


def timeliness(target_at: datetime, received_at: datetime) -> tuple[str, int]:
    lateness = received_at - target_at
    lateness_ms = round(lateness.total_seconds() * 1000)
    if lateness <= ON_TIME_LIMIT:
        return "on_time", lateness_ms
    if lateness <= LATE_LIMIT:
        return "late", lateness_ms
    return "missed", lateness_ms


def book_is_fresh(book_age_ms: int | None) -> bool:
    return book_age_ms is not None and (
        -MAX_TARGET_BOOK_CLOCK_SKEW_MS <= book_age_ms <= MAX_TARGET_BOOK_AGE_MS
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


# --- episodes --------------------------------------------------------------------


@dataclass(frozen=True)
class DueEpisode:
    capture_id: int
    qualification_version: str
    target_exchange: str
    entry_at: datetime
    notional_usd: Decimal
    ask_vwap: Decimal | None
    identity_key: str | None

    @property
    def target_at(self) -> datetime:
        return exit_target_at(self.entry_at)


_DUE_SQL = """
SELECT q.capture_id, q.qualification_version, q.selected_target_exchange,
       t.observed_at, t.requested_notional_usd,
       t.liquidity ->> 'ask_vwap' AS ask_vwap,
       t.instrument ->> 'identity_key' AS identity_key
FROM app.source_lead_qualifications q
JOIN app.source_lead_target_observations t
  ON t.capture_id = q.capture_id
 AND t.target_exchange = q.selected_target_exchange
 AND t.status = 'sampled'
WHERE q.qualification_version = %(qv)s
  AND q.status = 'qualified'
  AND t.observed_at >= %(since)s
  AND NOT EXISTS (
      SELECT 1 FROM app.source_lead_exit_observations e
      WHERE e.capture_id = q.capture_id
        AND e.qualification_version = q.qualification_version
  )
ORDER BY t.observed_at
LIMIT 500
"""

_CLAIM_SQL = """
INSERT INTO app.source_lead_exit_observations (
    capture_id, qualification_version, exit_version, target_exchange,
    instrument_identity_key, native_symbol, entry_at, target_at, claimed_at,
    outcome, timeliness, entry_ask_vwap, entry_notional_usd
) VALUES (
    %(capture_id)s, %(qv)s, %(ev)s, %(venue)s,
    %(identity_key)s, %(symbol)s, %(entry_at)s, %(target_at)s, now(),
    %(outcome)s, %(timeliness)s, %(ask_vwap)s, %(notional)s
)
ON CONFLICT (capture_id, qualification_version) DO NOTHING
RETURNING id
"""

_RECOVER_SQL = """
UPDATE app.source_lead_exit_observations
SET outcome = 'crashed_after_claim', timeliness = 'missed', updated_at = now(),
    error = 'process stopped between claim and final write'
WHERE outcome = 'claimed' AND exit_version = %(ev)s
"""

_FINAL_COLUMNS = (
    "outcome",
    "timeliness",
    "attempts",
    "requested_at",
    "received_at",
    "lateness_ms",
    "book_ts_ms",
    "book_cts_ms",
    "book_seq",
    "book_update_id",
    "book_age_ms",
    "contract_size",
    "contract_size_source",
    "qty_step",
    "hypothetical_qty_raw",
    "hypothetical_qty",
    "best_bid",
    "best_ask",
    "bid_vwap",
    "bid_filled_qty",
    "spread_bps",
    "impact_bps",
    "book_snapshot",
    "book_sha256",
    "error",
)


class ExitStore:
    """Thin psycopg store. One connection for the advisory lock, a pool-free
    short connection per statement otherwise (volume is ~dozens a day)."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._lock_conn: Any = None

    async def _execute(self, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._url, autocommit=True) as conn:
            cur = await conn.execute(sql, params)
            return list(await cur.fetchall()) if cur.description else []

    async def acquire_lock(self) -> bool:
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self._url, autocommit=True)
        cur = await conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (EXIT_VERSION,))
        row = await cur.fetchone()
        if not row or not row[0]:
            await conn.close()
            return False
        self._lock_conn = conn
        return True

    async def recover_claims(self) -> None:
        await self._execute(_RECOVER_SQL, {"ev": EXIT_VERSION})

    async def due_episodes(self) -> list[DueEpisode]:
        rows = await self._execute(
            _DUE_SQL, {"qv": QUALIFICATION_VERSION, "since": IDENTITY_REGISTRY_V4_START}
        )
        episodes = []
        for capture_id, qv, venue, observed_at, notional, ask_vwap, identity_key in rows:
            episodes.append(
                DueEpisode(
                    capture_id=int(capture_id),
                    qualification_version=str(qv),
                    target_exchange=str(venue),
                    entry_at=observed_at,
                    notional_usd=Decimal(str(notional)),
                    ask_vwap=Decimal(ask_vwap) if ask_vwap else None,
                    identity_key=identity_key,
                )
            )
        return episodes

    async def claim(
        self, episode: DueEpisode, *, outcome: str, timeliness: str | None
    ) -> int | None:
        rows = await self._execute(
            _CLAIM_SQL,
            {
                "capture_id": episode.capture_id,
                "qv": episode.qualification_version,
                "ev": EXIT_VERSION,
                "venue": episode.target_exchange,
                "identity_key": episode.identity_key,
                "symbol": native_symbol(episode.identity_key),
                "entry_at": episode.entry_at,
                "target_at": episode.target_at,
                "outcome": outcome,
                "timeliness": timeliness,
                "ask_vwap": episode.ask_vwap,
                "notional": episode.notional_usd,
            },
        )
        return int(rows[0][0]) if rows else None

    async def finalize(self, row_id: int, fields: dict[str, Any]) -> None:
        unknown = set(fields) - set(_FINAL_COLUMNS)
        if unknown:
            raise ValueError(f"unknown exit columns: {sorted(unknown)}")
        from psycopg.types.json import Jsonb

        params = {
            key: Jsonb(value) if key == "book_snapshot" and value is not None else value
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{key} = %({key})s" for key in params)
        await self._execute(
            f"UPDATE app.source_lead_exit_observations SET {assignments}, updated_at = now() "  # noqa: S608 -- column names come from the fixed _FINAL_COLUMNS allowlist
            "WHERE id = %(row_id)s",
            {**params, "row_id": row_id},
        )


# --- venue -----------------------------------------------------------------------


@dataclass(frozen=True)
class RawBook:
    payload: dict[str, Any]
    requested_at: datetime
    received_at: datetime


class BybitBookClient:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._qty_step: dict[str, tuple[float, Decimal]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def qty_step(self, symbol: str) -> Decimal:
        cached = self._qty_step.get(symbol)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        response = await self._client.get(
            BYBIT_INSTRUMENT_URL, params={"category": "linear", "symbol": symbol}
        )
        response.raise_for_status()
        items = (response.json().get("result") or {}).get("list") or []
        if len(items) != 1:
            raise LookupError(f"bybit instruments-info returned {len(items)} rows for {symbol}")
        step = Decimal(str(items[0]["lotSizeFilter"]["qtyStep"]))
        self._qty_step[symbol] = (time.monotonic(), step)
        return step

    async def book(self, symbol: str) -> RawBook:
        requested_at = datetime.now(UTC)
        response = await self._client.get(
            BYBIT_BOOK_URL,
            params={"category": "linear", "symbol": symbol, "limit": str(BOOK_LEVELS)},
        )
        received_at = datetime.now(UTC)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0 or not isinstance(payload.get("result"), dict):
            raise RuntimeError(f"bybit orderbook retCode={payload.get('retCode')}")
        return RawBook(cast("dict[str, Any]", payload), requested_at, received_at)


# --- capture ---------------------------------------------------------------------


def book_fields(raw: RawBook, target_at: datetime, quantity: Decimal) -> dict[str, Any]:
    """Everything stored for one successful book fetch."""
    result = raw.payload["result"]
    snapshot = {
        "s": result.get("s"),
        "b": result.get("b"),
        "a": result.get("a"),
        "ts": result.get("ts"),
        "u": result.get("u"),
        "seq": result.get("seq"),
        "cts": result.get("cts"),
        "time": raw.payload.get("time"),
    }
    book_ts = _int_or_none(result.get("ts"))
    received_ms = round(raw.received_at.timestamp() * 1000)
    book_age_ms = received_ms - book_ts if book_ts is not None else None
    when, lateness_ms = timeliness(target_at, raw.received_at)
    summary = summarize_exit_book(result.get("b"), result.get("a"), quantity)
    return {
        "outcome": "sampled" if book_is_fresh(book_age_ms) else "stale_book",
        "timeliness": when,
        "requested_at": raw.requested_at,
        "received_at": raw.received_at,
        "lateness_ms": lateness_ms,
        "book_ts_ms": book_ts,
        "book_cts_ms": _int_or_none(result.get("cts")),
        "book_seq": _int_or_none(result.get("seq")),
        "book_update_id": _int_or_none(result.get("u")),
        "book_age_ms": book_age_ms,
        "best_bid": summary.best_bid,
        "best_ask": summary.best_ask,
        "bid_vwap": summary.bid_vwap,
        "bid_filled_qty": summary.bid_filled_qty,
        "spread_bps": summary.spread_bps,
        "impact_bps": summary.impact_bps,
        "book_snapshot": snapshot,
        "book_sha256": hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


async def capture_episode(
    episode: DueEpisode,
    store: ExitStore,
    client: BybitBookClient,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Claim, wait for the target, fetch with retries inside the window, write
    once. Returns the final outcome."""
    target_at = episode.target_at
    if now() > target_at + LATE_LIMIT:
        # Discovered too late (worker down): record the gap, never fetch.
        await store.claim(episode, outcome="missed", timeliness="missed")
        return "missed"
    symbol = native_symbol(episode.identity_key)
    if episode.target_exchange not in SUPPORTED_VENUES:
        await store.claim(episode, outcome="unsupported_venue", timeliness=None)
        return "unsupported_venue"
    if symbol is None or episode.ask_vwap is None:
        await store.claim(episode, outcome="instrument_unresolved", timeliness=None)
        return "instrument_unresolved"
    row_id = await store.claim(episode, outcome="claimed", timeliness=None)
    if row_id is None:
        return "already_claimed"
    fields: dict[str, Any] = {"contract_size": Decimal(1), "contract_size_source": "bybit_linear"}
    try:
        step = await client.qty_step(symbol)
        raw_qty, qty = hypothetical_quantity(episode.notional_usd, episode.ask_vwap, step)
        fields |= {"qty_step": step, "hypothetical_qty_raw": raw_qty, "hypothetical_qty": qty}
    except Exception as exc:
        fields |= {"outcome": "instrument_unresolved", "attempts": 0, "error": str(exc)[:500]}
        await store.finalize(row_id, fields)
        return "instrument_unresolved"

    wait = (target_at - now()).total_seconds()
    if wait > 0:
        await sleep(wait)
    attempts = 0
    last_error = ""
    while now() <= target_at + LATE_LIMIT:
        attempts += 1
        try:
            raw = await client.book(symbol)
            fields |= book_fields(raw, target_at, qty) | {"attempts": attempts}
            await store.finalize(row_id, fields)
            return str(fields["outcome"])
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:500]
            log.warning(
                "source_lead_exit.fetch_failed", capture_id=episode.capture_id, error=last_error
            )
            await sleep(RETRY_SECONDS)
    fields |= {
        "outcome": "fetch_failed",
        "timeliness": "missed",
        "attempts": attempts,
        "error": last_error or "window elapsed before any fetch",
    }
    await store.finalize(row_id, fields)
    return "fetch_failed"


# --- worker ----------------------------------------------------------------------


async def _write_health(redis: Any, mapping: dict[str, Any]) -> None:
    try:
        await redis.hset(
            HEALTH_KEY,
            mapping={key: "" if value is None else str(value) for key, value in mapping.items()},
        )
    except Exception as exc:
        log.warning("source_lead_exit.health_write_failed", error=str(exc))


async def run_worker(database_url: str, redis_addr: str, *, once: bool = False) -> None:
    from redis.asyncio import Redis

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    store = ExitStore(database_url)
    if not await store.acquire_lock():
        raise RuntimeError("another source-lead exit worker already holds the lock")
    await store.recover_claims()
    client = BybitBookClient()
    url = (
        redis_addr if redis_addr.startswith(("redis://", "rediss://")) else f"redis://{redis_addr}"
    )
    redis = Redis.from_url(url, decode_responses=True)
    in_flight: dict[int, asyncio.Task[str]] = {}
    counts: dict[str, int] = {}
    try:
        while True:
            error = ""
            try:
                for episode in await store.due_episodes():
                    if episode.capture_id in in_flight:
                        continue
                    # Start the task a little before the target so the claim and
                    # qty lookup are done when the target arrives.
                    if (episode.target_at - datetime.now(UTC)).total_seconds() > 20:
                        continue
                    in_flight[episode.capture_id] = asyncio.create_task(
                        capture_episode(episode, store, client)
                    )
                for capture_id, task in list(in_flight.items()):
                    if task.done():
                        del in_flight[capture_id]
                        outcome = task.result() if task.exception() is None else "task_error"
                        if task.exception() is not None:
                            log.error(
                                "source_lead_exit.task_failed",
                                capture_id=capture_id,
                                error=str(task.exception()),
                            )
                        counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                log.error("source_lead_exit.tick_failed", error=error)
            await _write_health(
                redis,
                {
                    "status": "degraded" if error else "ok",
                    "exit_version": EXIT_VERSION,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "in_flight": len(in_flight),
                    "last_error": error,
                    **{f"outcomes_{key}": value for key, value in counts.items()},
                },
            )
            if once and not in_flight:
                return
            await asyncio.sleep(POLL_SECONDS)
    finally:
        for task in in_flight.values():
            task.cancel()
        await client.close()
        await redis.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", help="one pass, for smoke tests")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    asyncio.run(run_worker(database_url, os.getenv("REDIS_ADDR", "redis:6379"), once=args.once))


if __name__ == "__main__":
    main()


__all__ = (
    "EXIT_VERSION",
    "BookSummary",
    "DueEpisode",
    "capture_episode",
    "exit_target_at",
    "hypothetical_quantity",
    "native_symbol",
    "summarize_exit_book",
    "timeliness",
)
