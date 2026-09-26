"""Shadow execution for HYP-012 v2 (source-lead, Bybit): the real path to an order,
measured without placing one.

For every qualified v4 episode (selected venue Bybit) the worker:

1. **Claims first.** Inserts an attempt row in `app.source_lead_shadow_attempts`
   (unique per capture and qualification version) before any quote request,
   keeping the first `first_seen_at`. A crash leaves the row `claimed`; the
   next start marks it `crashed_after_claim` and never re-requests a quote.
2. **Resolves the instrument** from the entered observation's registered
   `identity_key` (native id), exactly one active USDT linear swap in the ccxt
   catalogue with that id; never by ticker.
3. **Takes a fresh Bybit quote at the intended send time**: the raw v5 book with
   its native `ts` and the receive time; the $50 ask VWAP on the same instrument
   and notional as at capture; `qtyStep`/`minOrderQty` from instruments-info.
4. **Records the funnel**: every skip gets its own outcome; a valid intent goes
   through `ShadowBroker` into `trade_decisions` (`decision_id` stored here).

Timing chain (ms): capture `observed_at` -> first seen (`detect_latency_ms`;
over 30 s is flagged `late`, never dropped), `qualified_at` -> first seen
(`from_qualified_ms`), first seen -> quote request (`process_latency_ms`), and
request -> response (`quote_latency_ms`). `quote_change_bps` is the change of the
executable $50 ask VWAP over the delay; no order was sent, so it is not slippage.

Mode: `SOURCE_LEAD_MODE` unset means DISABLED; only `shadow` or `disabled` are
accepted, anything else stops the service at startup. A live broker is a
separate change with its own order-lifecycle review. No return is computed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

import structlog

from . import execution_intent, symbols
from .execution_intent import ExecutionIntent, ExecutionStatus, StrategyIdentity, TradingMode

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

SHADOW_VERSION = "source_lead_shadow_v1"
STRATEGY_NAME = "source_lead"
STRATEGY_VERSION = "1"
# Literal copies of the analytics contract (HYP-012 qualification v4); the
# execution package cannot import analytics. Changing them is a new version.
QUALIFICATION_VERSION = "source_lead_qualified_capture_v4"
COHORT_START = datetime(2026, 9, 29, tzinfo=UTC)
MAX_BOOK_AGE_MS = 2000
MAX_BOOK_CLOCK_SKEW_MS = 1000

TARGET_USD = Decimal(50)
LATE_AFTER = timedelta(seconds=30)
POLL_SECONDS = 1.0
WRITE_ATTEMPTS = 5
HEALTH_KEY = f"market:sourceleadshadow:health:{SHADOW_VERSION}"
BYBIT_BOOK_URL = "https://api.bybit.com/v5/market/orderbook"
BYBIT_INSTRUMENT_URL = "https://api.bybit.com/v5/market/instruments-info"


def resolve_source_lead_mode(cfg: Config) -> TradingMode:
    """Unset is DISABLED (never PAPER); only SHADOW or DISABLED are allowed."""
    raw = cfg.source_lead_mode
    if raw is None:
        return TradingMode.DISABLED
    mode = execution_intent.parse_mode(raw)
    if mode not in (TradingMode.DISABLED, TradingMode.SHADOW):
        raise ValueError(f"SOURCE_LEAD_MODE={raw!r}: only 'shadow' or 'disabled' are allowed")
    if mode is TradingMode.SHADOW and execution_intent.mode_ceiling(cfg) is TradingMode.DISABLED:
        raise ValueError("SOURCE_LEAD_MODE=shadow needs DRY_RUN or AUTO_TRADE (mode ceiling)")
    return mode


# --- pure ------------------------------------------------------------------------


def native_symbol(identity_key: Any) -> str | None:
    """`bybit:swap:ABCUSDT:1700000000000` -> `ABCUSDT`."""
    if not isinstance(identity_key, str):
        return None
    parts = identity_key.split(":")
    if len(parts) != 4 or parts[0] != "bybit" or parts[1] != "swap" or not parts[2]:
        return None
    return parts[2]


def resolve_instrument(exchange: Any, native_id: str) -> symbols.ExecutionInstrument:
    """Exactly one active USDT-settled linear swap whose native id is `native_id`."""
    markets = getattr(exchange, "markets", None) or {}
    matches = [
        m
        for m in markets.values()
        if m.get("id") == native_id
        and m.get("active", True)
        and m.get("swap")
        and m.get("linear")
        and m.get("quote") == "USDT"
        and m.get("settle") == "USDT"
    ]
    if len(matches) != 1:
        raise LookupError(f"{len(matches)} active USDT linear swaps with id {native_id}")
    market = matches[0]
    return symbols.ExecutionInstrument(
        exchange="bybit",
        symbol=str(market["symbol"]),
        native_market_id=native_id,
        base=str(market["base"]),
        quote="USDT",
        settle="USDT",
        market_type="swap",
    )


def ask_vwap_for_notional(asks: Any, target_usd: Decimal) -> Decimal | None:
    """Buy `target_usd` of notional into the asks (the capture's own convention)."""
    spent = Decimal(0)
    bought = Decimal(0)
    for level in asks if isinstance(asks, list) else []:
        try:
            price, size = Decimal(str(level[0])), Decimal(str(level[1]))
        except (ArithmeticError, IndexError, TypeError, ValueError):
            continue
        if not (price.is_finite() and size.is_finite()) or price <= 0 or size <= 0:
            continue
        take_usd = min(price * size, target_usd - spent)
        if take_usd <= 0:
            break
        spent += take_usd
        bought += take_usd / price
    if spent < target_usd or bought <= 0:
        return None
    return spent / bought


def rounded_quantity(target_usd: Decimal, vwap: Decimal, qty_step: Decimal) -> Decimal:
    steps = (target_usd / vwap / qty_step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * qty_step


@dataclass(frozen=True)
class Episode:
    capture_id: int
    observed_at: datetime
    qualified_at: datetime
    capture_ask_vwap: Decimal | None
    identity_key: str | None


def timing(episode: Episode, seen_at: datetime) -> dict[str, Any]:
    detect = seen_at - episode.observed_at
    return {
        "detect_latency_ms": round(detect.total_seconds() * 1000),
        "from_qualified_ms": round((seen_at - episode.qualified_at).total_seconds() * 1000),
        "late": detect > LATE_AFTER,
    }


# --- store -----------------------------------------------------------------------

_DUE = """
SELECT q.capture_id, t.observed_at, q.qualified_at,
       t.liquidity ->> 'ask_vwap' AS ask_vwap,
       t.instrument ->> 'identity_key' AS identity_key
FROM app.source_lead_qualifications q
JOIN app.source_lead_captures c ON c.id = q.capture_id
JOIN app.source_lead_target_observations t
  ON t.capture_id = q.capture_id
 AND t.target_exchange = q.selected_target_exchange
 AND t.status = 'sampled'
WHERE q.qualification_version = %(qv)s
  AND q.status = 'qualified'
  AND q.selected_target_exchange = 'bybit'
  AND c.source_first_observed_at >= %(since)s
  AND NOT EXISTS (
      SELECT 1 FROM app.source_lead_shadow_attempts a
      WHERE a.capture_id = q.capture_id AND a.qualification_version = q.qualification_version
  )
ORDER BY q.qualified_at
LIMIT 50
"""

_CLAIM = """
INSERT INTO app.source_lead_shadow_attempts (
    capture_id, qualification_version, shadow_version, native_symbol,
    instrument_identity_key, observed_at, qualified_at, first_seen_at, outcome, late,
    detect_latency_ms, from_qualified_ms, capture_ask_vwap
) VALUES (
    %(capture_id)s, %(qv)s, %(sv)s, %(symbol)s, %(identity_key)s, %(observed_at)s,
    %(qualified_at)s, %(first_seen_at)s, 'claimed', %(late)s, %(detect_latency_ms)s,
    %(from_qualified_ms)s, %(capture_ask_vwap)s
)
ON CONFLICT (capture_id, qualification_version) DO NOTHING
RETURNING id
"""

_FINAL_COLUMNS = frozenset(
    {
        "outcome",
        "attempts",
        "quote_requested_at",
        "quote_received_at",
        "book_ts_ms",
        "book_age_ms",
        "process_latency_ms",
        "quote_latency_ms",
        "qty_step",
        "min_order_qty",
        "quantity",
        "send_ask_vwap",
        "quote_change_bps",
        "decision_id",
        "error",
    }
)


class ShadowStore:
    def __init__(self, db_url: str) -> None:
        self._url = db_url

    async def _execute(self, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._url, autocommit=True) as conn:
            cur = await conn.execute(sql, params)
            return list(await cur.fetchall()) if cur.description else []

    async def recover_claims(self) -> int:
        rows = await self._execute(
            """
            UPDATE app.source_lead_shadow_attempts
            SET outcome = 'crashed_after_claim', updated_at = now(),
                error = 'process stopped between claim and final write'
            WHERE outcome = 'claimed' AND shadow_version = %(sv)s
            RETURNING id
            """,
            {"sv": SHADOW_VERSION},
        )
        return len(rows)

    async def due(self) -> list[Episode]:
        rows = await self._execute(_DUE, {"qv": QUALIFICATION_VERSION, "since": COHORT_START})
        return [
            Episode(
                capture_id=int(r[0]),
                observed_at=r[1],
                qualified_at=r[2],
                capture_ask_vwap=Decimal(r[3]) if r[3] else None,
                identity_key=r[4],
            )
            for r in rows
        ]

    async def claim(self, episode: Episode, seen_at: datetime) -> int | None:
        rows = await self._execute(
            _CLAIM,
            {
                "capture_id": episode.capture_id,
                "qv": QUALIFICATION_VERSION,
                "sv": SHADOW_VERSION,
                "symbol": native_symbol(episode.identity_key),
                "identity_key": episode.identity_key,
                "observed_at": episode.observed_at,
                "qualified_at": episode.qualified_at,
                "first_seen_at": seen_at,
                "capture_ask_vwap": episode.capture_ask_vwap,
                **timing(episode, seen_at),
            },
        )
        return int(rows[0][0]) if rows else None

    async def finalize(self, row_id: int, fields: dict[str, Any]) -> bool:
        unknown = set(fields) - _FINAL_COLUMNS
        if unknown:
            raise ValueError(f"unknown shadow columns: {sorted(unknown)}")
        assignments = ", ".join(f"{key} = %({key})s" for key in fields)
        rows = await self._execute(
            f"UPDATE app.source_lead_shadow_attempts SET {assignments}, updated_at = now() "  # noqa: S608 -- column names come from the fixed _FINAL_COLUMNS allowlist
            "WHERE id = %(row_id)s AND outcome = 'claimed' RETURNING id",
            {**fields, "row_id": row_id},
        )
        return bool(rows)


# --- venue -----------------------------------------------------------------------


class VenueError(Exception):
    """Timeout, HTTP error or a non-zero retCode."""


@dataclass(frozen=True)
class Spec:
    qty_step: Decimal
    min_order_qty: Decimal


@dataclass(frozen=True)
class Book:
    asks: Any
    ts_ms: int | None
    requested_at: datetime
    received_at: datetime


class BybitQuotes:
    def __init__(self, timeout_seconds: float = 3.0) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._specs: dict[str, tuple[float, Spec]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        import httpx

        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueError(f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise VenueError(
                f"retCode={payload.get('retCode') if isinstance(payload, dict) else None}"
            )
        if not isinstance(payload.get("result"), dict):
            raise VenueError("no result")
        return payload

    async def spec(self, symbol: str) -> Spec:
        cached = self._specs.get(symbol)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        payload = await self._get(BYBIT_INSTRUMENT_URL, {"category": "linear", "symbol": symbol})
        items = payload["result"].get("list") or []
        if len(items) != 1 or items[0].get("symbol") != symbol:
            raise LookupError(f"instruments-info returned {[i.get('symbol') for i in items]}")
        lot = items[0].get("lotSizeFilter") or {}
        spec = Spec(Decimal(str(lot["qtyStep"])), Decimal(str(lot["minOrderQty"])))
        self._specs[symbol] = (time.monotonic(), spec)
        return spec

    async def book(self, symbol: str) -> Book:
        requested_at = datetime.now(UTC)
        payload = await self._get(
            BYBIT_BOOK_URL, {"category": "linear", "symbol": symbol, "limit": "50"}
        )
        received_at = datetime.now(UTC)
        result = payload["result"]
        if result.get("s") != symbol:
            raise VenueError(f"orderbook for {result.get('s')!r}, not {symbol}")
        ts = result.get("ts")
        return Book(
            asks=result.get("a"),
            ts_ms=int(ts) if isinstance(ts, int | str) and str(ts).isdigit() else None,
            requested_at=requested_at,
            received_at=received_at,
        )


# --- one episode -----------------------------------------------------------------


def _ms(delta: timedelta) -> int:
    return round(delta.total_seconds() * 1000)


async def shadow_episode(
    episode: Episode,
    *,
    store: ShadowStore,
    quotes: BybitQuotes,
    exchange: Any,
    broker: execution_intent.Broker,
    cfg: Config,
    rdb: Any,
    now: Any = lambda: datetime.now(UTC),
    sleep: Any = asyncio.sleep,
) -> str:
    seen_at = now()
    row_id = await store.claim(episode, seen_at)
    if row_id is None:
        return "already_claimed"
    fields: dict[str, Any] = {"attempts": 0}
    outcome = await _evaluate(episode, seen_at, fields, quotes, exchange, broker, cfg, rdb)
    fields["outcome"] = outcome
    delay = 1.0
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            written = await store.finalize(row_id, fields)
            return outcome if written else "superseded"
        except Exception as exc:
            if attempt == WRITE_ATTEMPTS:
                raise
            log.warning("source_lead_shadow.write_retry", row_id=row_id, error=str(exc)[:200])
            await sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


async def _evaluate(
    episode: Episode,
    seen_at: datetime,
    fields: dict[str, Any],
    quotes: BybitQuotes,
    exchange: Any,
    broker: execution_intent.Broker,
    cfg: Config,
    rdb: Any,
) -> str:
    symbol = native_symbol(episode.identity_key)
    if symbol is None or episode.capture_ask_vwap is None:
        fields["error"] = "no registered bybit identity_key or capture ask VWAP"
        return "instrument_mismatch"
    try:
        instrument = resolve_instrument(exchange, symbol)
        spec = await quotes.spec(symbol)
    except LookupError as exc:
        fields["error"] = str(exc)[:500]
        return "instrument_mismatch"
    except VenueError as exc:
        fields["error"] = f"instrument: {exc}"[:500]
        return "fetch_failed"
    fields |= {"qty_step": spec.qty_step, "min_order_qty": spec.min_order_qty}

    fields["attempts"] = 1
    try:
        book = await quotes.book(symbol)
    except VenueError as exc:
        fields["error"] = str(exc)[:500]
        return "fetch_failed"
    fields |= {
        "quote_requested_at": book.requested_at,
        "quote_received_at": book.received_at,
        "process_latency_ms": _ms(book.requested_at - seen_at),
        "quote_latency_ms": _ms(book.received_at - book.requested_at),
        "book_ts_ms": book.ts_ms,
    }
    if book.ts_ms is None:
        return "no_book_timestamp"
    age = round(book.received_at.timestamp() * 1000) - book.ts_ms
    fields["book_age_ms"] = age
    if not -MAX_BOOK_CLOCK_SKEW_MS <= age <= MAX_BOOK_AGE_MS:
        return "stale_book"
    vwap = ask_vwap_for_notional(book.asks, TARGET_USD)
    if vwap is None:
        return "insufficient_depth"
    quantity = rounded_quantity(TARGET_USD, vwap, spec.qty_step)
    fields |= {
        "send_ask_vwap": vwap,
        "quantity": quantity,
        "quote_change_bps": (vwap - episode.capture_ask_vwap) / episode.capture_ask_vwap * 10_000,
    }
    if quantity <= 0 or quantity < spec.min_order_qty:
        return "below_min_order"

    context = {
        "strategy": f"{STRATEGY_NAME}_v{STRATEGY_VERSION}",
        "shadow_version": SHADOW_VERSION,
        "capture_id": episode.capture_id,
        "qualification_version": QUALIFICATION_VERSION,
        "observed_at": episode.observed_at.isoformat(),
        "qualified_at": episode.qualified_at.isoformat(),
        "first_seen_at": seen_at.isoformat(),
        **{k: fields[k] for k in ("process_latency_ms", "quote_latency_ms", "book_age_ms")},
        **{k: str(fields[k]) for k in ("send_ask_vwap", "quantity", "quote_change_bps")},
        "capture_ask_vwap": str(episode.capture_ask_vwap),
        **timing(episode, seen_at),
    }
    intent = ExecutionIntent(
        strategy=StrategyIdentity(name=STRATEGY_NAME, version=STRATEGY_VERSION),
        instrument=instrument,
        side="long",
        size_usd=float(TARGET_USD),
        leverage=1,
        score=0,
        setup_context=context,
        idempotency_key=f"source_lead:{episode.capture_id}:{QUALIFICATION_VERSION}",
        price=float(vwap),
    )
    result = await broker.open(intent, cfg=cfg, rdb=rdb)
    if result.status is ExecutionStatus.SHADOW_RECORDED:
        fields["decision_id"] = execution_intent.shadow_decision_id(intent)
        return "shadow_recorded"
    fields["error"] = f"{result.status.value}: {result.reason}"[:500]
    return "broker_rejected"


# --- worker ----------------------------------------------------------------------


async def run_source_lead_shadow(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
    broker: execution_intent.Broker,
    tracker: Any = None,
) -> None:
    if broker.mode is not TradingMode.SHADOW:
        log.info("source_lead_shadow.disabled", mode=broker.mode.value)
        return
    if not cfg.db_url:
        raise RuntimeError("source-lead shadow needs DATABASE_URL")
    exchange = exchanges.get("bybit")
    if exchange is None:
        raise RuntimeError("source-lead shadow needs the bybit market exchange")
    store = ShadowStore(cfg.db_url)
    recovered = await store.recover_claims()
    if recovered:
        log.warning("source_lead_shadow.recovered_claims", count=recovered)
    quotes = BybitQuotes()
    counts: dict[str, int] = {}
    try:
        while True:
            if tracker:
                tracker.tick_started()
            error = ""
            try:
                for episode in await store.due():
                    outcome = await shadow_episode(
                        episode,
                        store=store,
                        quotes=quotes,
                        exchange=exchange,
                        broker=broker,
                        cfg=cfg,
                        rdb=rdb,
                    )
                    counts[outcome] = counts.get(outcome, 0) + 1
                    log.info(
                        "source_lead_shadow.attempt", capture_id=episode.capture_id, outcome=outcome
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                log.error("source_lead_shadow.tick_failed", error=error)
                if tracker:
                    tracker.tick_failed(exc)
            else:
                if tracker:
                    tracker.tick_succeeded()
            await _write_health(rdb, counts, error)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await quotes.close()


async def _write_health(rdb: Any, counts: dict[str, int], error: str) -> None:
    mapping = {
        "status": "degraded" if error else "ok",
        "shadow_version": SHADOW_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "last_error": error,
        **{f"outcomes_{k}": str(v) for k, v in counts.items()},
    }
    try:
        await rdb.hset(HEALTH_KEY, mapping=mapping)
    except Exception as exc:
        log.warning("source_lead_shadow.health_write_failed", error=str(exc))
