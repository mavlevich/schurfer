"""Paper trading: track simulated positions in Redis, monitor exit conditions."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from . import exit as exit_module
from . import journal, liquidity, notify, symbols

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_KEY_PREFIX = "position:paper:"
_TRADE_ID_KEY = "trade:id:paper:{exchange}:{base}"
_INTERVAL_SECONDS = 30


def paper_key(exchange: str, base: str) -> str:
    return f"{_KEY_PREFIX}{exchange}:{base.upper()}"


async def open_paper(
    rdb: Any,
    *,
    instrument: symbols.ExecutionInstrument,
    price: float,
    size_usd: float,
    leverage: int,
    score: int,
    setup_context: dict[str, Any],
    cfg: Config,
    side: str = "short",
    exit_params: dict[str, float] | None = None,
) -> None:
    params = (
        exit_params
        if exit_params is not None
        else exit_module.exit_params(setup_context.get("pump_pct"))
    )
    paper_context = {**setup_context, "paper": True}
    (
        accounting_version,
        _accounting_status,
        entry_slippage_bps,
        exit_slippage_bps,
    ) = journal.accounting_contract(paper_context, side=side)
    strategy = setup_context.get("strategy", "unknown")
    entry = {
        "base": instrument.base,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "side": side,
        "strategy": strategy,
        "entry_price": price,
        "size_usd": size_usd,
        "leverage": leverage,
        "opened_at": time.time(),
        "score": score,
        "exit_params": params,
        "accounting_version": accounting_version,
        "entry_slippage_bps": entry_slippage_bps,
        "exit_slippage_bps": exit_slippage_bps,
    }
    await rdb.set(paper_key(instrument.exchange, instrument.base), json.dumps(entry), ex=86400 * 7)

    if cfg.db_url:
        trade_id = await journal.open_trade(
            cfg.db_url,
            symbol=instrument.symbol,
            exchange=instrument.exchange,
            side=side,
            order_id=None,
            size_usd=size_usd,
            leverage=leverage,
            entry_price=price,
            setup_context=paper_context,
        )
        if trade_id:
            await rdb.set(
                _TRADE_ID_KEY.format(exchange=instrument.exchange, base=instrument.base.upper()),
                str(trade_id),
                ex=86400 * 7,
            )

    creds = notify.credentials(cfg)
    if creds:
        await notify.notify_open(
            *creds,
            strategy=strategy,
            base=instrument.base,
            exchange=instrument.exchange,
            size_usd=size_usd,
            leverage=leverage,
            price=price,
            score=score,
            side=side,
            paper=True,
        )

    log.info(
        "paper.opened",
        symbol=instrument.symbol,
        exchange=instrument.exchange,
        price=price,
        score=score,
    )


async def close_paper(
    rdb: Any,
    *,
    pos: dict[str, Any],
    current_price: float,
    reason: str,
    cfg: Config,
    exchange_client: Any | None = None,
) -> None:
    base = pos["base"]
    exchange = pos["exchange"]
    symbol = pos.get("symbol")
    if not symbol and exchange_client is not None:
        try:
            symbol = symbols.resolve_execution_instrument(exchange_client, base).symbol
        except (RuntimeError, ValueError) as exc:
            log.warning(
                "paper.close.unresolved_legacy_symbol",
                base=base,
                exchange=exchange,
                err=str(exc),
            )
    entry_price = float(pos["entry_price"])
    side = pos.get("side", "short")
    strategy = pos.get("strategy", "unknown")
    leverage_raw = pos.get("leverage")

    # Fallback figures for the case journal.close_trade never runs at all (no
    # DB, no trade_id, or exchange_client unavailable). Once close_trade does
    # run, its CloseOutcome below is the single source of truth for both the
    # DB row and this notification — never recomputed independently, so the
    # two can never diverge.
    gross_pnl_pct = (
        (entry_price - current_price) / entry_price * 100
        if side == "short"
        else (current_price - entry_price) / entry_price * 100
    )
    accounting_status = "legacy"
    displayed_pnl_pct = gross_pnl_pct
    # A position stored before size tracking existed has no size_usd at all —
    # None stays None rather than fabricating a dollar figure.
    size_usd_raw = pos.get("size_usd")
    displayed_pnl_usd = (
        float(size_usd_raw) * gross_pnl_pct / 100 if size_usd_raw is not None else None
    )
    fees_usd: float | None = None
    funding_usd: float | None = None
    slippage_usd: float | None = None

    trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base.upper())
    trade_id_raw = await rdb.get(trade_id_key)
    exit_observation: dict[str, Any] | None = None
    exit_vwap: float | None = None
    fresh_exit_slippage_bps: float | None = None
    if trade_id_raw and cfg.db_url and exchange_client is not None and symbol is not None:
        try:
            exit_observation, exit_vwap, fresh_exit_slippage_bps = await _capture_exit_liquidity(
                exchange_client,
                symbol=symbol,
                exchange=exchange,
                side=side,
                requested_notional_usd=float(pos["size_usd"]),
            )
        except Exception as exc:
            # Capturing evidence is best effort. No malformed position payload,
            # exchange response, or observation bug may keep the position open.
            log.error(
                "paper.exit_liquidity_capture_failed",
                symbol=symbol,
                exchange=exchange,
                err=str(exc),
            )

    # exit_vwap (when available) already reflects the real cost of filling
    # this size, the same way entry_vwap does at open time -- use it as the
    # accounting/display exit price so entry and exit are priced the same
    # way (never entry=VWAP paired with exit=mark, which would make the
    # Entry->Exit line in Telegram misleading about what actually happened).
    # fresh_exit_slippage_bps is already 0.0 in that case, never a second
    # charge on top of a price that already paid it.
    exit_price_for_accounting = exit_vwap if exit_vwap is not None else current_price

    if trade_id_raw and cfg.db_url:
        trade_id = int(trade_id_raw)
        outcome = await journal.close_trade(
            cfg.db_url,
            trade_id=trade_id,
            exit_order_id=None,
            exit_price=exit_price_for_accounting,
            reason=reason,
            fresh_exit_slippage_bps=fresh_exit_slippage_bps,
            exit_observation=exit_observation,
        )
        if not outcome.committed:
            # Regression (colleague review): the Redis position key used to
            # be deleted unconditionally before this call. A DB outage then
            # meant the trade's row stayed "open" forever (never retried --
            # paper trades deliberately skip journal.try_commit_close's
            # pending-close/retry machinery, see below) while the position
            # simultaneously vanished from what _tick monitors, silently
            # orphaning it and permanently blocking re-entry on this symbol
            # (find_open_trade_id would see it "open" forever). Leaving the
            # position untouched here means the next monitor tick naturally
            # re-evaluates and retries the close instead.
            log.error(
                "paper.journal_close_failed",
                symbol=symbol,
                exchange=exchange,
                trade_id=trade_id,
            )
            return
        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        if outcome.accounting_status is not None:
            accounting_status = outcome.accounting_status
        if outcome.gross_pnl_pct is not None:
            gross_pnl_pct = outcome.gross_pnl_pct
        if outcome.gross_pnl_usd is not None:
            displayed_pnl_usd = outcome.gross_pnl_usd
        # net when fully resolved, else the same gross figure already
        # labeled "Gross PnL" below -- never a mix of a net percent with
        # a gross dollar amount or vice versa. Falling back to the
        # pre-computed pos-based estimate (not a bare None) covers the
        # idempotent already-closed retry, where CloseOutcome carries no
        # fresh accounting at all.
        displayed_pnl_pct = (
            outcome.net_pnl_pct if outcome.net_pnl_pct is not None else gross_pnl_pct
        )
        if outcome.net_pnl_usd is not None:
            displayed_pnl_usd = outcome.net_pnl_usd
        fees_usd = outcome.fees_usd
        funding_usd = outcome.funding_usd
        slippage_usd = outcome.slippage_usd

    # Paper trades are deliberately NOT routed through journal.try_commit_close:
    # that mechanism writes a journal:pending_close marker that tracker.py
    # treats as "a real close is outstanding" and withholds the trading-ready
    # lease for. A stuck paper-trade journal write must never block real
    # order placement. Reaching here means either there was nothing to
    # commit (no DB/trade_id) or the commit above already succeeded -- safe
    # to stop tracking this position and report the close.
    await rdb.delete(paper_key(exchange, base))

    creds = notify.credentials(cfg)
    if creds:
        await notify.notify_close(
            *creds,
            strategy=strategy,
            base=base,
            exchange=exchange,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price_for_accounting,
            size_usd=size_usd_raw,
            margin_usd=(
                float(size_usd_raw) / float(leverage_raw)
                if size_usd_raw is not None and leverage_raw
                else None
            ),
            gross_pnl_pct=gross_pnl_pct,
            pnl_pct=displayed_pnl_pct,
            pnl_usd=displayed_pnl_usd,
            pnl_kind="modeled_net" if accounting_status == "complete" else "gross",
            accounting_status=accounting_status,
            fees_usd=fees_usd,
            funding_usd=funding_usd,
            slippage_usd=slippage_usd,
            reason=reason,
            paper=True,
        )

    log.info(
        "paper.closed",
        symbol=symbol,
        exchange=exchange,
        gross_pnl_pct=round(gross_pnl_pct, 2),
        displayed_pnl_pct=round(displayed_pnl_pct, 2),
        accounting_status=accounting_status,
        exit_liquidity_status=(
            exit_observation.get("status") if exit_observation is not None else "not_observed"
        ),
        reason=reason,
    )


async def _capture_exit_liquidity(
    exchange_client: Any,
    *,
    symbol: str,
    exchange: str,
    side: str,
    requested_notional_usd: float,
) -> tuple[dict[str, Any], float | None, float | None]:
    """Capture a fresh close-time book and pick the side that actually prices
    this position's exit (bid for LONG, ask for SHORT — see
    liquidity.book_side_for). Both sides are recorded on the returned
    observation for evidence.

    Returns (observation, exit_vwap, fresh_exit_slippage_bps). exit_vwap is
    the price the caller should actually book the exit at — it already
    reflects the real cost of filling requested_notional_usd on that side,
    the same way early_momentum.py's entry_vwap does. fresh_exit_slippage_bps
    is therefore 0.0 exactly when exit_vwap is available (that cost is
    already inside the price — charging it again in calculate_performance
    would double count it), or None when the book couldn't be read or didn't
    have enough visible depth, so net accounting correctly falls back to
    incomplete rather than guessing.
    """
    capture = await liquidity.capture_snapshot(
        exchange_client,
        symbol,
        required_depth_usd=requested_notional_usd,
    )
    snapshot = capture.snapshot or {}
    bid_vwap, bid_impact, bid_filled = liquidity.quote_for_book_side(
        snapshot, book_side="bid", target_usd=requested_notional_usd
    )
    ask_vwap, ask_impact, ask_filled = liquidity.quote_for_book_side(
        snapshot, book_side="ask", target_usd=requested_notional_usd
    )
    exit_book_side = liquidity.book_side_for(position_side=side, leg="exit")
    exit_vwap, exit_filled = (
        (bid_vwap, bid_filled) if exit_book_side == "bid" else (ask_vwap, ask_filled)
    )
    status = capture.status
    error = capture.error
    if status == "sampled" and exit_vwap is None:
        status = f"insufficient_{exit_book_side}_depth"
        error = f"visible {exit_book_side} depth cannot fill requested notional"
    observation = {
        "observed_at": datetime.fromtimestamp(capture.observed_at_ms / 1000, tz=UTC),
        "exchange": exchange,
        "symbol": symbol,
        "market_id": snapshot.get("market_id"),
        "status": status,
        "requested_notional_usd": requested_notional_usd,
        "filled_notional_usd": exit_filled,
        "best_bid": snapshot.get("best_bid"),
        "best_ask": snapshot.get("best_ask"),
        "mid": snapshot.get("mid"),
        "spread_bps": snapshot.get("spread_bps"),
        "bid_vwap": bid_vwap,
        "bid_impact_bps": bid_impact,
        "ask_vwap": ask_vwap,
        "ask_impact_bps": ask_impact,
        "contract_size": snapshot.get("contract_size"),
        "latency_ms": capture.latency_ms,
        "error": error,
    }
    fresh_exit_slippage_bps = 0.0 if exit_vwap is not None else None
    return observation, exit_vwap, fresh_exit_slippage_bps


async def run_paper_monitor(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    while True:
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("paper_monitor.error", err=str(exc))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    keys = [k async for k in rdb.scan_iter(f"{_KEY_PREFIX}*")]
    if not keys:
        return

    for key in keys:
        try:
            raw = await rdb.get(key)
            if not raw:
                continue
            try:
                pos = json.loads(raw)
            except Exception as exc:
                log.warning("paper.bad_payload", key=str(key), err=str(exc))
                continue

            base = pos["base"]
            symbol = pos.get("symbol")
            exchange = pos["exchange"]
            entry_price = float(pos["entry_price"])
            opened_at = float(pos.get("opened_at", 0))
            side = pos.get("side", "short")

            ex = exchanges.get(exchange)
            if not ex:
                continue

            if not symbol:
                try:
                    instrument = symbols.resolve_execution_instrument(ex, base)
                    symbol = instrument.symbol
                    pos["symbol"] = symbol
                except (RuntimeError, ValueError) as e:
                    log.error(
                        "paper.monitor.unresolved_legacy_symbol",
                        base=base,
                        err=str(e),
                    )
                    continue

            try:
                ticker = await ex.fetch_ticker(symbol)
                mark = float(ticker.get("last") or 0)
            except Exception as exc:
                log.warning("paper.ticker_failed", symbol=symbol, exchange=exchange, err=str(exc))
                continue

            if mark <= 0:
                continue

            params = pos.get("exit_params") or exit_module.exit_params(None)
            bp_key = exit_module.best_price_key(exchange, base, paper=True)

            reason = await exit_module.check_exit(
                side=side,
                entry_price=entry_price,
                current_price=mark,
                opened_at=opened_at,
                params=params,
                rdb=rdb,
                bp_key=bp_key,
            )

            if reason:
                await rdb.delete(bp_key)
                await close_paper(
                    rdb,
                    pos=pos,
                    current_price=mark,
                    reason=reason,
                    cfg=cfg,
                    exchange_client=ex,
                )
        except Exception as exc:
            log.error("paper.trade_error", key=str(key), err=str(exc))
            continue
