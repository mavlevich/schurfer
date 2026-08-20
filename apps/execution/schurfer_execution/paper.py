"""Paper trading: track simulated positions in Redis, monitor exit conditions."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from schurfer_performance import PAPER_ACCOUNTING_VERSION, calculate_performance

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
    ) = journal.accounting_contract(paper_context)
    entry = {
        "base": instrument.base,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "side": side,
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

    pnl_pct = (
        (entry_price - current_price) / entry_price * 100
        if side == "short"
        else (current_price - entry_price) / entry_price * 100
    )
    accounting_status = "legacy"
    displayed_pnl_pct = pnl_pct
    # Mirrors displayed_pnl_pct's own fallback: modeled net when the full
    # accounting resolved (status "complete"), otherwise the same raw,
    # unmodeled gross figure the "Gross PnL" label already promises below
    # (never a mix of net percent with gross dollars or vice versa). A
    # position stored before size tracking existed has no size_usd at all —
    # None stays None rather than fabricating a dollar figure.
    size_usd_raw = pos.get("size_usd")
    displayed_pnl_usd = float(size_usd_raw) * pnl_pct / 100 if size_usd_raw is not None else None
    accounting_version = pos.get("accounting_version")
    if accounting_version == PAPER_ACCOUNTING_VERSION:
        accounting = calculate_performance(
            position_usd=float(pos["size_usd"]),
            entry_price=entry_price,
            exit_price=current_price,
            side=side,
            duration_minutes=max(0.0, (time.time() - float(pos["opened_at"])) / 60),
            entry_slippage_bps=pos.get("entry_slippage_bps"),
            exit_slippage_bps=pos.get("exit_slippage_bps"),
        )
        accounting_status = accounting.status
        if accounting.net_return_pct is not None:
            displayed_pnl_pct = accounting.net_return_pct
        if accounting.net_pnl_usd is not None:
            displayed_pnl_usd = accounting.net_pnl_usd

    # Paper trades are deliberately NOT routed through journal.try_commit_close:
    # that mechanism writes a journal:pending_close marker that tracker.py
    # treats as "a real close is outstanding" and withholds the trading-ready
    # lease for. A stuck paper-trade journal write must never block real
    # order placement. Best-effort commit + log is enough here — paper stats
    # are informational, not part of the daily-loss circuit breaker.
    trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base.upper())
    trade_id_raw = await rdb.get(trade_id_key)
    exit_observation: dict[str, Any] | None = None
    if trade_id_raw and cfg.db_url and exchange_client is not None and symbol is not None:
        try:
            exit_observation = await _capture_exit_liquidity(
                exchange_client,
                symbol=symbol,
                exchange=exchange,
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

    await rdb.delete(paper_key(exchange, base))

    if trade_id_raw and cfg.db_url:
        trade_id = int(trade_id_raw)
        committed = await journal.close_trade(
            cfg.db_url,
            trade_id=trade_id,
            exit_order_id=None,
            exit_price=current_price,
            reason=reason,
        )
        if exit_observation is not None:
            await journal.record_exit_liquidity(
                cfg.db_url,
                trade_id=trade_id,
                observation=exit_observation,
            )
        if committed:
            await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        else:
            log.error(
                "paper.journal_close_failed",
                symbol=symbol,
                exchange=exchange,
                trade_id=trade_id,
            )

    creds = notify.credentials(cfg)
    if creds:
        await notify.notify_close(
            *creds,
            base=base,
            exchange=exchange,
            entry_price=entry_price,
            exit_price=current_price,
            pnl_pct=displayed_pnl_pct,
            pnl_usd=displayed_pnl_usd,
            pnl_kind="modeled_net" if accounting_status == "complete" else "gross",
            reason=reason,
            paper=True,
        )

    log.info(
        "paper.closed",
        symbol=symbol,
        exchange=exchange,
        gross_pnl_pct=round(pnl_pct, 2),
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
    requested_notional_usd: float,
) -> dict[str, Any]:
    capture = await liquidity.capture_snapshot(
        exchange_client,
        symbol,
        required_depth_usd=requested_notional_usd,
    )
    snapshot = capture.snapshot or {}
    target_key = liquidity.depth_target_key(requested_notional_usd)
    ask_impacts = snapshot.get("ask_impact_bps")
    ask_vwaps = snapshot.get("ask_vwap")
    ask_filled = snapshot.get("ask_filled_usd")
    ask_impact = ask_impacts.get(target_key) if isinstance(ask_impacts, dict) else None
    ask_vwap = ask_vwaps.get(target_key) if isinstance(ask_vwaps, dict) else None
    filled_notional = ask_filled.get(target_key) if isinstance(ask_filled, dict) else None
    status = capture.status
    error = capture.error
    if status == "sampled" and ask_impact is None:
        status = "insufficient_ask_depth"
        error = "visible ask depth cannot fill requested notional"
    return {
        "observed_at": datetime.fromtimestamp(capture.observed_at_ms / 1000, tz=UTC),
        "exchange": exchange,
        "symbol": symbol,
        "market_id": snapshot.get("market_id"),
        "status": status,
        "requested_notional_usd": requested_notional_usd,
        "filled_notional_usd": filled_notional,
        "best_bid": snapshot.get("best_bid"),
        "best_ask": snapshot.get("best_ask"),
        "mid": snapshot.get("mid"),
        "spread_bps": snapshot.get("spread_bps"),
        "ask_vwap": ask_vwap,
        "ask_impact_bps": ask_impact,
        "contract_size": snapshot.get("contract_size"),
        "latency_ms": capture.latency_ms,
        "error": error,
    }


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
