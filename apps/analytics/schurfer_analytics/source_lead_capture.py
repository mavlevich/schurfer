"""Forward-only Gate source-lead quote capture.

The capture is measurement-only. It records every new Gate observation from the
current scanner process, including exclusions and failed target lookups. Later
cross-venue confirmation is deliberately absent from this module.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog

from .exchange_registry import EXCHANGE_FACTORIES, ExchangeFactory
from .instruments import instrument_metadata
from .source_lead_contract import CAPTURE_VERSION
from .source_lead_qualification import (
    QUALIFICATION_VERSION,
    VENUE_SELECTOR_VERSION,
    IdentityRegistry,
    QualificationResult,
    load_identity_registry,
    qualify_source_lead,
)

log = structlog.get_logger()

SOURCE_EXCHANGE = "gate"
IDENTITY_MATCH_METHOD = "base_symbol_v1"

_SELECT_SOURCES = """
SELECT
    e.id,
    e.base,
    s.exchange,
    s.symbol,
    s.identity_key,
    s.market_id,
    s.market_type,
    s.quote_asset,
    s.settle_asset,
    s.identity_conflict,
    s.first_seen_at,
    s.first_ticker_at,
    s.first_change_pct,
    s.first_price,
    s.first_volume_24h_usd
FROM app.pump_events AS e
JOIN app.pump_event_sources AS gate
  ON gate.event_id = e.id
 AND gate.exchange = %s
JOIN app.pump_event_sources AS s
  ON s.event_id = e.id
LEFT JOIN app.source_lead_captures AS capture
  ON capture.event_id = e.id
 AND capture.capture_version = %s
WHERE e.id = ANY(%s)
  AND gate.first_seen_at >= %s
  AND capture.id IS NULL
ORDER BY e.id, s.first_seen_at, s.exchange
"""

_INSERT_CAPTURE = """
INSERT INTO app.source_lead_captures (
    event_id, capture_version, source_exchange, base, source_symbol,
    source_identity_key, source_market_id,
    source_occurred_at, source_published_at, source_first_observed_at,
    collector_started_at, capture_started_at, capture_completed_at,
    status, eligibility_reason, source_change_pct, source_price,
    source_volume_24h_usd, first_sources, source_payload,
    error, created_at, updated_at
)
VALUES (
    %s, %s, %s, %s, %s,
    %s, %s,
    %s, NULL, %s,
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s::jsonb, %s::jsonb,
    NULL, NOW(), NOW()
)
ON CONFLICT (event_id, capture_version) DO NOTHING
RETURNING id
"""

_INSERT_TARGET = """
INSERT INTO app.source_lead_target_observations (
    capture_id, target_exchange, status, eligibility_reason,
    identity_match_method, identity_verified,
    observed_at, occurred_at, published_at, latency_ms,
    requested_notional_usd, instrument, ticker, liquidity, error,
    created_at, updated_at
)
VALUES (
    %s, %s, %s, %s,
    %s, %s,
    %s, %s, NULL, %s,
    %s, %s::jsonb, %s::jsonb, %s::jsonb, %s,
    NOW(), NOW()
)
ON CONFLICT (capture_id, target_exchange) DO NOTHING
"""

_COMPLETE_CAPTURE = """
UPDATE app.source_lead_captures
SET status = 'complete',
    capture_completed_at = %s,
    updated_at = NOW()
WHERE id = %s
  AND status = 'collecting'
"""

_INSERT_QUALIFICATION = """
INSERT INTO app.source_lead_qualifications (
    capture_id, qualification_version, identity_registry_version,
    identity_registry_fingerprint, venue_selector_version,
    status, reason, canonical_asset_id,
    selected_target_exchange, selected_round_trip_impact_bps,
    requested_notional_usd, qualified_at, details, created_at, updated_at
)
VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s::jsonb, NOW(), NOW()
)
ON CONFLICT (capture_id, qualification_version) DO NOTHING
"""

_ABANDON_CAPTURES = """
UPDATE app.source_lead_captures
SET status = 'abandoned',
    capture_completed_at = %s,
    error = %s,
    updated_at = NOW()
WHERE id = ANY(%s)
  AND status = 'collecting'
"""

_ABANDON_PREVIOUS_PROCESS_CAPTURES = """
UPDATE app.source_lead_captures
SET status = 'abandoned',
    capture_completed_at = %s,
    error = 'collector_process_restarted',
    updated_at = NOW()
WHERE capture_version = %s
  AND status = 'collecting'
  AND collector_started_at < %s
RETURNING id
"""


@dataclass(frozen=True)
class SourceObservation:
    event_id: int
    base: str
    exchange: str
    symbol: str
    identity_key: str | None
    market_id: str | None
    market_type: str | None
    quote_asset: str | None
    settle_asset: str | None
    identity_conflict: bool
    first_seen_at: datetime
    first_ticker_at: datetime | None
    first_change_pct: float
    first_price: float | None
    first_volume_24h_usd: float | None


@dataclass(frozen=True)
class SourceLeadCandidate:
    event_id: int
    base: str
    source: SourceObservation
    first_sources: tuple[str, ...]
    eligible: bool
    eligibility_reason: str


@dataclass(frozen=True)
class ClaimedCapture:
    capture_id: int
    candidate: SourceLeadCandidate


@dataclass(frozen=True)
class TargetObservation:
    target_exchange: str
    status: str
    eligibility_reason: str
    identity_verified: bool
    observed_at: datetime
    occurred_at: datetime | None
    latency_ms: int
    requested_notional_usd: float
    instrument: dict[str, Any]
    ticker: dict[str, Any]
    liquidity: dict[str, Any]
    error: str | None


def _finite_float(value: Any, *, positive: bool = False) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        return None
    return parsed


def _timestamp(value: Any) -> datetime | None:
    parsed = _finite_float(value)
    if parsed is None or parsed < 0:
        return None
    try:
        return datetime.fromtimestamp(parsed / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _source_from_row(row: tuple[Any, ...]) -> SourceObservation:
    return SourceObservation(
        event_id=int(row[0]),
        base=str(row[1]),
        exchange=str(row[2]),
        symbol=str(row[3]),
        identity_key=str(row[4]) if row[4] is not None else None,
        market_id=str(row[5]) if row[5] is not None else None,
        market_type=str(row[6]) if row[6] is not None else None,
        quote_asset=str(row[7]) if row[7] is not None else None,
        settle_asset=str(row[8]) if row[8] is not None else None,
        identity_conflict=bool(row[9]),
        first_seen_at=row[10],
        first_ticker_at=row[11],
        first_change_pct=float(row[12]),
        first_price=_finite_float(row[13], positive=True),
        first_volume_24h_usd=_finite_float(row[14]),
    )


def build_source_lead_candidates(
    observations: tuple[SourceObservation, ...],
) -> tuple[SourceLeadCandidate, ...]:
    """Classify every new Gate observation without using later confirmation."""
    grouped: dict[int, list[SourceObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.event_id, []).append(observation)

    candidates: list[SourceLeadCandidate] = []
    for event_id in sorted(grouped):
        rows = grouped[event_id]
        gate_rows = [row for row in rows if row.exchange == SOURCE_EXCHANGE]
        if len(gate_rows) != 1:
            raise ValueError("source-lead capture requires exactly one Gate row per event")
        source = gate_rows[0]
        earliest = min(row.first_seen_at for row in rows)
        first_sources = tuple(sorted(row.exchange for row in rows if row.first_seen_at == earliest))

        reason = "eligible"
        if first_sources != (SOURCE_EXCHANGE,):
            reason = "gate_not_unique_first_source"
        elif source.identity_conflict:
            reason = "source_identity_conflict"
        elif source.identity_key is None or source.market_id is None:
            reason = "source_identity_incomplete"
        elif source.market_type != "swap":
            reason = "source_not_swap"
        elif source.quote_asset != "USDT" or source.settle_asset != "USDT":
            reason = "source_not_linear_usdt"

        candidates.append(
            SourceLeadCandidate(
                event_id=event_id,
                base=source.base,
                source=source,
                first_sources=first_sources,
                eligible=reason == "eligible",
                eligibility_reason=reason,
            )
        )
    return tuple(candidates)


async def load_source_lead_candidates(
    db_url: str,
    event_ids: set[int],
    collector_started_at: datetime,
) -> tuple[SourceLeadCandidate, ...]:
    if not event_ids:
        return ()
    connection_context = await psycopg.AsyncConnection.connect(db_url)
    async with connection_context as connection, connection.cursor() as cur:
        await cur.execute(
            _SELECT_SOURCES,
            (SOURCE_EXCHANGE, CAPTURE_VERSION, sorted(event_ids), collector_started_at),
        )
        rows = await cur.fetchall()
    return build_source_lead_candidates(tuple(_source_from_row(row) for row in rows))


def _source_payload(candidate: SourceLeadCandidate) -> dict[str, Any]:
    source = candidate.source
    return {
        "schema_version": 1,
        "identity_conflict": source.identity_conflict,
        "market_type": source.market_type,
        "quote_asset": source.quote_asset,
        "settle_asset": source.settle_asset,
        "timestamp_contract": {
            "occurred_at": "exchange ticker timestamp when available",
            "published_at": "unavailable_for_public_market_ticker",
            "first_observed_at": "scanner observation persisted on first sight",
            "ingested_at": "database created_at",
        },
    }


async def claim_source_lead_captures(
    db_url: str,
    candidates: tuple[SourceLeadCandidate, ...],
    collector_started_at: datetime,
    capture_started_at: datetime,
) -> tuple[ClaimedCapture, ...]:
    claimed: list[ClaimedCapture] = []
    connection_context = await psycopg.AsyncConnection.connect(db_url)
    async with connection_context as connection, connection.cursor() as cur:
        for candidate in candidates:
            source = candidate.source
            completed_at = None if candidate.eligible else capture_started_at
            await cur.execute(
                _INSERT_CAPTURE,
                (
                    candidate.event_id,
                    CAPTURE_VERSION,
                    SOURCE_EXCHANGE,
                    candidate.base,
                    source.symbol,
                    source.identity_key,
                    source.market_id,
                    source.first_ticker_at,
                    source.first_seen_at,
                    collector_started_at,
                    capture_started_at,
                    completed_at,
                    "collecting" if candidate.eligible else "excluded",
                    candidate.eligibility_reason,
                    source.first_change_pct,
                    source.first_price,
                    source.first_volume_24h_usd,
                    json.dumps(candidate.first_sources),
                    json.dumps(_source_payload(candidate)),
                ),
            )
            row = await cur.fetchone()
            if row is not None and candidate.eligible:
                claimed.append(ClaimedCapture(capture_id=int(row[0]), candidate=candidate))
    return tuple(claimed)


async def prepare_source_lead_captures(
    db_url: str,
    event_ids: set[int],
    collector_started_at: datetime,
) -> tuple[ClaimedCapture, ...]:
    """Durably classify every new event before any bounded network capture."""
    if not event_ids:
        return ()
    candidates = await load_source_lead_candidates(
        db_url,
        event_ids,
        collector_started_at,
    )
    if not candidates:
        return ()
    return await claim_source_lead_captures(
        db_url,
        candidates,
        collector_started_at,
        datetime.now(UTC),
    )


async def abandon_source_lead_captures(
    db_url: str,
    claimed: tuple[ClaimedCapture, ...],
    reason: str,
) -> None:
    """Close already-durable claims that cannot reach the network worker."""
    if not claimed:
        return
    connection_context = await psycopg.AsyncConnection.connect(db_url)
    async with connection_context as connection, connection.cursor() as cur:
        await cur.execute(
            _ABANDON_CAPTURES,
            (
                datetime.now(UTC),
                reason[:1000],
                [item.capture_id for item in claimed],
            ),
        )


async def abandon_previous_process_captures(
    db_url: str,
    collector_started_at: datetime,
) -> int:
    """Close claims whose owning scanner process can no longer finish them."""
    connection_context = await psycopg.AsyncConnection.connect(db_url)
    async with connection_context as connection, connection.cursor() as cur:
        await cur.execute(
            _ABANDON_PREVIOUS_PROCESS_CAPTURES,
            (datetime.now(UTC), CAPTURE_VERSION, collector_started_at),
        )
        rows = await cur.fetchall()
    return len(rows)


def _execution_quote(
    levels: Any,
    mid: float,
    target_usd: float,
    side: str,
    contract_size: float,
) -> tuple[float | None, float | None, float]:
    if not isinstance(levels, list):
        return None, None, 0.0
    total_quote = 0.0
    total_base = 0.0
    for level in levels:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        price = _finite_float(level[0], positive=True)
        amount = _finite_float(level[1], positive=True)
        if price is None or amount is None:
            continue
        remaining = target_usd - total_quote
        if remaining <= 0:
            break
        take_usd = min(price * amount * contract_size, remaining)
        total_quote += take_usd
        total_base += take_usd / price
    if total_quote + 1e-9 < target_usd or total_base <= 0:
        return None, None, round(total_quote, 4)
    vwap = total_quote / total_base
    impact = (vwap - mid) / mid if side == "ask" else (mid - vwap) / mid
    return round(vwap, 14), round(max(impact, 0.0) * 10_000, 4), round(total_quote, 4)


def summarize_order_book(
    book: Any,
    *,
    target_usd: float,
    contract_size: float,
) -> dict[str, Any]:
    if not isinstance(book, dict):
        raise ValueError("order book is not an object")
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ValueError("order book has an empty or invalid side")
    best_bid = _finite_float(bids[0][0], positive=True)
    best_ask = _finite_float(asks[0][0], positive=True)
    if best_bid is None or best_ask is None or best_ask < best_bid:
        raise ValueError("order book top is invalid or crossed")
    mid = (best_bid + best_ask) / 2
    bid_vwap, bid_impact, bid_filled = _execution_quote(bids, mid, target_usd, "bid", contract_size)
    ask_vwap, ask_impact, ask_filled = _execution_quote(asks, mid, target_usd, "ask", contract_size)
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": round((best_ask - best_bid) / mid * 10_000, 4),
        "bid_vwap": bid_vwap,
        "bid_impact_bps": bid_impact,
        "bid_filled_notional_usd": bid_filled,
        "ask_vwap": ask_vwap,
        "ask_impact_bps": ask_impact,
        "ask_filled_notional_usd": ask_filled,
    }


def _target_failure(
    exchange: str,
    reason: str,
    started: float,
    target_usd: float,
    *,
    instrument: dict[str, Any] | None = None,
    error: str | None = None,
) -> TargetObservation:
    return TargetObservation(
        target_exchange=exchange,
        status="excluded" if error is None else "fetch_failed",
        eligibility_reason=reason,
        identity_verified=False,
        observed_at=datetime.now(UTC),
        occurred_at=None,
        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        requested_notional_usd=target_usd,
        instrument=instrument or {},
        ticker={},
        liquidity={},
        error=error[:1000] if error else None,
    )


async def capture_target_observation(
    exchange_name: str,
    exchange: Any,
    candidate: SourceLeadCandidate,
    *,
    target_usd: float,
    timeout_seconds: float,
) -> TargetObservation:
    started = time.monotonic()
    symbol = f"{candidate.base}/USDT:USDT"
    markets = exchange.markets if isinstance(exchange.markets, dict) else {}
    market = markets.get(symbol)
    if not isinstance(market, dict):
        return _target_failure(exchange_name, "target_not_listed", started, target_usd)
    metadata = instrument_metadata(exchange_name, symbol, market)
    if market.get("active") is False:
        return _target_failure(
            exchange_name, "target_inactive", started, target_usd, instrument=metadata
        )
    if market.get("swap") is not True or market.get("linear") is not True:
        return _target_failure(
            exchange_name, "target_not_linear_swap", started, target_usd, instrument=metadata
        )
    if metadata.get("quote_asset") != "USDT" or metadata.get("settle_asset") != "USDT":
        return _target_failure(
            exchange_name, "target_not_linear_usdt", started, target_usd, instrument=metadata
        )
    onboarded_at = _timestamp(metadata.get("onboarded_at_ms"))
    if onboarded_at is None:
        return _target_failure(
            exchange_name,
            "target_onboarding_unknown",
            started,
            target_usd,
            instrument=metadata,
        )
    if onboarded_at > candidate.source.first_seen_at:
        return _target_failure(
            exchange_name,
            "target_listed_after_source",
            started,
            target_usd,
            instrument=metadata,
        )

    try:
        ticker, book = await asyncio.wait_for(
            asyncio.gather(exchange.fetch_ticker(symbol), exchange.fetch_order_book(symbol, 50)),
            timeout=timeout_seconds,
        )
        if not isinstance(ticker, dict):
            raise ValueError("ticker is not an object")
        contract_size = _finite_float(metadata.get("contract_size"), positive=True) or 1.0
        liquidity = summarize_order_book(
            book,
            target_usd=target_usd,
            contract_size=contract_size,
        )
        price = _finite_float(ticker.get("last"), positive=True)
        if price is None:
            raise ValueError("target ticker price is unavailable")
        occurred_at = _timestamp(ticker.get("timestamp"))
        observed_at = datetime.now(UTC)
        return TargetObservation(
            target_exchange=exchange_name,
            status="sampled",
            # Symbol equality is deliberately provisional. A registered strategy
            # must replace it with a versioned canonical identity approval.
            eligibility_reason="identity_unverified",
            identity_verified=False,
            observed_at=observed_at,
            occurred_at=occurred_at,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            requested_notional_usd=target_usd,
            instrument=metadata,
            ticker={
                "last": price,
                "change_pct": _finite_float(ticker.get("percentage")),
                "volume_24h_usd": _finite_float(ticker.get("quoteVolume")),
            },
            liquidity=liquidity,
            error=None,
        )
    except Exception as exc:
        return _target_failure(
            exchange_name,
            "target_fetch_failed",
            started,
            target_usd,
            instrument=metadata,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _persist_target_observations(
    db_url: str,
    results: dict[int, list[TargetObservation]],
    qualifications: dict[int, QualificationResult],
    registry_version: str,
    registry_fingerprint: str,
) -> None:
    completed_at = datetime.now(UTC)
    connection_context = await psycopg.AsyncConnection.connect(db_url)
    async with connection_context as connection, connection.cursor() as cur:
        for capture_id, observations in results.items():
            for observation in observations:
                await cur.execute(
                    _INSERT_TARGET,
                    (
                        capture_id,
                        observation.target_exchange,
                        observation.status,
                        observation.eligibility_reason,
                        IDENTITY_MATCH_METHOD,
                        observation.identity_verified,
                        observation.observed_at,
                        observation.occurred_at,
                        observation.latency_ms,
                        observation.requested_notional_usd,
                        json.dumps(observation.instrument),
                        json.dumps(observation.ticker),
                        json.dumps(observation.liquidity),
                        observation.error,
                    ),
                )
            qualification = qualifications[capture_id]
            await cur.execute(
                _INSERT_QUALIFICATION,
                (
                    capture_id,
                    QUALIFICATION_VERSION,
                    registry_version,
                    registry_fingerprint,
                    VENUE_SELECTOR_VERSION,
                    qualification.status,
                    qualification.reason,
                    qualification.canonical_asset_id,
                    qualification.selected_target_exchange,
                    qualification.selected_round_trip_impact_bps,
                    qualification.requested_notional_usd,
                    completed_at,
                    json.dumps(qualification.details),
                ),
            )
            await cur.execute(_COMPLETE_CAPTURE, (completed_at, capture_id))


async def capture_claimed_source_leads(
    db_url: str,
    claimed: tuple[ClaimedCapture, ...],
    *,
    target_exchanges: tuple[str, ...],
    target_usd: float,
    timeout_seconds: float,
    factories: dict[str, ExchangeFactory] | None = None,
    identity_registry: IdentityRegistry | None = None,
) -> None:
    """Capture network observations for claims already durable in PostgreSQL."""
    if not claimed:
        return

    exchange_factories = factories or EXCHANGE_FACTORIES
    registry = identity_registry or load_identity_registry()
    results: dict[int, list[TargetObservation]] = {item.capture_id: [] for item in claimed}
    for exchange_name in target_exchanges:
        factory = exchange_factories.get(exchange_name)
        if factory is None:
            raise ValueError(f"unknown source-lead target exchange: {exchange_name}")
        exchange = factory()
        exchange_started = time.monotonic()
        try:
            await asyncio.wait_for(exchange.load_markets(), timeout=timeout_seconds)
            for item in claimed:
                observation = await capture_target_observation(
                    exchange_name,
                    exchange,
                    item.candidate,
                    target_usd=target_usd,
                    timeout_seconds=timeout_seconds,
                )
                results[item.capture_id].append(observation)
        except Exception as exc:
            for item in claimed:
                results[item.capture_id].append(
                    _target_failure(
                        exchange_name,
                        "target_exchange_unavailable",
                        exchange_started,
                        target_usd,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        finally:
            try:
                await exchange.close(clean_instance_data=True)
            except TypeError:
                await exchange.close()

    qualifications = {
        item.capture_id: qualify_source_lead(
            source_exchange=SOURCE_EXCHANGE,
            source_identity_key=item.candidate.source.identity_key,
            target_observations=tuple(results[item.capture_id]),
            registry=registry,
        )
        for item in claimed
    }
    await _persist_target_observations(
        db_url,
        results,
        qualifications,
        registry.version,
        registry.fingerprint,
    )
    log.info(
        "source_lead.capture_complete",
        captures=len(claimed),
        targets=sum(len(rows) for rows in results.values()),
        version=CAPTURE_VERSION,
    )


class SourceLeadCaptureWorker:
    """Single bounded network worker isolated from the scanner cadence."""

    def __init__(
        self,
        db_url: str,
        *,
        target_exchanges: tuple[str, ...],
        target_usd: float,
        timeout_seconds: float,
        queue_size: int,
        shutdown_timeout_seconds: float,
        collector_started_at: datetime | None = None,
        factories: dict[str, ExchangeFactory] | None = None,
        identity_registry: IdentityRegistry | None = None,
    ) -> None:
        if queue_size <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("source-lead worker bounds must be positive")
        self._db_url = db_url
        self._target_exchanges = target_exchanges
        self._target_usd = target_usd
        self._timeout_seconds = timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._collector_started_at = collector_started_at or datetime.now(UTC)
        self._factories = factories
        self._identity_registry = identity_registry or load_identity_registry()
        self._queue: asyncio.Queue[tuple[ClaimedCapture, ...] | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._task: asyncio.Task[None] | None = None
        self._active: tuple[ClaimedCapture, ...] = ()
        self.queue_dropped_total = 0

    @property
    def pending_batches(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if self._task is not None and self._task.done():
            try:
                self._task.result()
            except Exception as exc:
                log.error("source_lead.capture_worker_restarting", err=str(exc))
            self._task = None
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="source-lead-capture-worker")

    async def submit(self, claimed: tuple[ClaimedCapture, ...]) -> None:
        if not claimed:
            return
        self.start()
        try:
            self._queue.put_nowait(claimed)
        except asyncio.QueueFull:
            self.queue_dropped_total += len(claimed)
            await abandon_source_lead_captures(
                self._db_url,
                claimed,
                "capture_queue_full",
            )
            log.error(
                "source_lead.capture_queue_full",
                captures=len(claimed),
                dropped_total=self.queue_dropped_total,
            )

    async def _run(self) -> None:
        try:
            abandoned = await abandon_previous_process_captures(
                self._db_url,
                self._collector_started_at,
            )
            if abandoned:
                log.warning(
                    "source_lead.previous_process_captures_abandoned",
                    captures=abandoned,
                )
        except Exception as exc:
            # A temporary recovery failure must not disable current forward capture.
            # Old collecting rows remain visible in the production health query.
            log.exception("source_lead.capture_recovery_failed", err=str(exc))

        while True:
            claimed = await self._queue.get()
            if claimed is None:
                self._queue.task_done()
                return
            self._active = claimed
            try:
                await capture_claimed_source_leads(
                    self._db_url,
                    claimed,
                    target_exchanges=self._target_exchanges,
                    target_usd=self._target_usd,
                    timeout_seconds=self._timeout_seconds,
                    factories=self._factories,
                    identity_registry=self._identity_registry,
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    abandon_source_lead_captures(
                        self._db_url,
                        claimed,
                        "capture_worker_cancelled",
                    )
                )
                raise
            except Exception as exc:
                try:
                    await abandon_source_lead_captures(
                        self._db_url,
                        claimed,
                        f"capture_worker_failed: {type(exc).__name__}: {exc}",
                    )
                except Exception as cleanup_exc:
                    # Preserve worker liveness. The stale collecting row is an
                    # explicit, monitored failure rather than a lost observation.
                    log.exception(
                        "source_lead.capture_abandon_failed",
                        err=str(cleanup_exc),
                    )
                log.exception("source_lead.capture_worker_failed", err=str(exc))
            finally:
                self._active = ()
                self._queue.task_done()

    async def _abandon_pending(self, reason: str) -> None:
        while True:
            try:
                claimed = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if claimed is not None:
                    await abandon_source_lead_captures(self._db_url, claimed, reason)
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        if self._task is None:
            return

        async def finish() -> None:
            await self._queue.put(None)
            if self._task is not None:
                await self._task

        try:
            await asyncio.wait_for(finish(), timeout=self._shutdown_timeout_seconds)
        except TimeoutError:
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            await self._abandon_pending("capture_worker_shutdown_timeout")
            log.warning(
                "source_lead.capture_shutdown_timeout",
                timeout_seconds=self._shutdown_timeout_seconds,
            )
        finally:
            self._task = None


async def capture_new_source_leads(
    db_url: str,
    event_ids: set[int],
    collector_started_at: datetime,
    *,
    target_exchanges: tuple[str, ...],
    target_usd: float,
    timeout_seconds: float,
    batch_size: int,
    factories: dict[str, ExchangeFactory] | None = None,
    identity_registry: IdentityRegistry | None = None,
) -> None:
    """Synchronous compatibility helper used by focused tests and one-shot tools."""
    claimed = await prepare_source_lead_captures(
        db_url,
        event_ids,
        collector_started_at,
    )
    for offset in range(0, len(claimed), batch_size):
        await capture_claimed_source_leads(
            db_url,
            claimed[offset : offset + batch_size],
            target_exchanges=target_exchanges,
            target_usd=target_usd,
            timeout_seconds=timeout_seconds,
            factories=factories,
            identity_registry=identity_registry,
        )
