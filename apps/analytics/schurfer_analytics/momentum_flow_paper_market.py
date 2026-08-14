"""Exact-venue executable quote capture for the momentum paper probe."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .momentum_flow_paper_contract import PaperContract


@dataclass(frozen=True)
class ExecutableQuote:
    symbol: str
    unified_symbol: str
    market_id: str
    side: str
    requested_at: datetime
    observed_at: datetime
    exchange_event_at: datetime | None
    latency_ms: int
    best_bid: float
    best_ask: float
    mid: float
    spread_bps: float
    vwap: float
    impact_bps: float
    filled_notional_usd: float
    contract_size: float


@dataclass(frozen=True)
class QuoteFailure:
    symbol: str
    side: str
    requested_at: datetime
    failed_at: datetime
    latency_ms: int
    reason: str
    error: str


QuoteResult = ExecutableQuote | QuoteFailure


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def execution_vwap(
    levels: list[Any],
    *,
    target_usd: float,
    contract_size: float,
) -> tuple[float | None, float]:
    """Return the executable VWAP and visible filled notional for one book side."""

    total_quote = 0.0
    total_base = 0.0
    for level in levels:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        price = _finite_positive(level[0])
        amount = _finite_positive(level[1])
        if price is None or amount is None:
            continue
        remaining = target_usd - total_quote
        if remaining <= 0:
            break
        taken = min(price * amount * contract_size, remaining)
        total_quote += taken
        total_base += taken / price
    if total_quote + 1e-9 < target_usd or total_base <= 0:
        return None, round(total_quote, 8)
    return total_quote / total_base, round(total_quote, 8)


def _exact_market(exchange: Any, symbol_id: str) -> tuple[str, str, float]:
    matches: list[tuple[str, dict[str, Any]]] = []
    markets = getattr(exchange, "markets", None)
    if isinstance(markets, dict):
        for unified, market in markets.items():
            if not isinstance(market, dict) or str(market.get("id") or "") != symbol_id:
                continue
            if not market.get("swap") or not market.get("linear"):
                continue
            if str(market.get("settle") or "").upper() != "USDT":
                continue
            matches.append((str(unified), market))
    if len(matches) != 1:
        raise ValueError(f"exact market id {symbol_id!r} resolved to {len(matches)} markets")
    unified, market = matches[0]
    contract_size = _finite_positive(market.get("contractSize"))
    if contract_size is None:
        raise ValueError("derivative contract size is unavailable")
    return unified, str(market["id"]), contract_size


def summarize_book(
    *,
    symbol_id: str,
    unified_symbol: str,
    market_id: str,
    contract_size: float,
    side: str,
    target_usd: float,
    requested_at: datetime,
    observed_at: datetime,
    latency_ms: int,
    book: Any,
) -> ExecutableQuote:
    if side not in {"ask", "bid"}:
        raise ValueError("quote side must be ask or bid")
    if not isinstance(book, dict):
        raise ValueError("order book is not an object")
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ValueError("order book has an empty or invalid side")
    best_bid = _finite_positive(bids[0][0])
    best_ask = _finite_positive(asks[0][0])
    if best_bid is None or best_ask is None or best_ask < best_bid:
        raise ValueError("order book top is invalid or crossed")
    mid = (best_bid + best_ask) / 2
    levels = asks if side == "ask" else bids
    vwap, filled = execution_vwap(
        levels,
        target_usd=target_usd,
        contract_size=contract_size,
    )
    if vwap is None:
        raise ValueError(f"insufficient {side} depth for {target_usd:.2f} USD")
    impact = (vwap - mid) / mid if side == "ask" else (mid - vwap) / mid
    if not math.isfinite(impact):
        raise ValueError("quote impact is non-finite")
    exchange_ts = _finite_positive(book.get("timestamp"))
    exchange_event_at = (
        datetime.fromtimestamp(exchange_ts / 1000, tz=UTC) if exchange_ts is not None else None
    )
    return ExecutableQuote(
        symbol=symbol_id,
        unified_symbol=unified_symbol,
        market_id=market_id,
        side=side,
        requested_at=requested_at,
        observed_at=observed_at,
        exchange_event_at=exchange_event_at,
        latency_ms=latency_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread_bps=(best_ask - best_bid) / mid * 10_000,
        vwap=vwap,
        impact_bps=max(0.0, impact * 10_000),
        filled_notional_usd=filled,
        contract_size=contract_size,
    )


class BybitPaperMarket:
    def __init__(self, exchange: Any, contract: PaperContract) -> None:
        self._exchange = exchange
        self._contract = contract
        self._loaded = False

    async def load(self, *, reload: bool = False) -> None:
        if reload or not self._loaded:
            await self._exchange.load_markets(reload=reload)
            self._loaded = True

    async def _fetch_book(self, symbol: str) -> tuple[str, str, float, Any]:
        await self.load()
        try:
            unified, market_id, contract_size = _exact_market(self._exchange, symbol)
        except ValueError:
            # The capture universe can gain a newly listed market while this worker
            # remains alive. Refresh metadata once, then keep exact identity fail-closed.
            await self.load(reload=True)
            unified, market_id, contract_size = _exact_market(self._exchange, symbol)
        book = await self._exchange.fetch_order_book(unified, self._contract.book_limit)
        return unified, market_id, contract_size, book

    async def quote(self, symbol: str, side: str) -> QuoteResult:
        requested_at = datetime.now(UTC)
        started = perf_counter()
        try:
            unified, market_id, contract_size, book = await asyncio.wait_for(
                self._fetch_book(symbol),
                timeout=self._contract.max_quote_latency_seconds,
            )
            observed_at = datetime.now(UTC)
            latency_ms = round((perf_counter() - started) * 1000)
            return summarize_book(
                symbol_id=symbol,
                unified_symbol=unified,
                market_id=market_id,
                contract_size=contract_size,
                side=side,
                target_usd=self._contract.position_notional_usd,
                requested_at=requested_at,
                observed_at=observed_at,
                latency_ms=latency_ms,
                book=book,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            reason = "quote_timeout"
            error = str(exc) or type(exc).__name__
        except ValueError as exc:
            reason = "invalid_or_insufficient_book"
            error = str(exc)
        except Exception as exc:
            reason = "quote_fetch_failed"
            error = f"{type(exc).__name__}: {exc}"
        failed_at = datetime.now(UTC)
        return QuoteFailure(
            symbol=symbol,
            side=side,
            requested_at=requested_at,
            failed_at=failed_at,
            latency_ms=round((perf_counter() - started) * 1000),
            reason=reason,
            error=error[:1000],
        )

    async def close(self) -> None:
        await self._exchange.close()
