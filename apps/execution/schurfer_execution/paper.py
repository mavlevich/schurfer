"""Paper trading: track simulated positions in Redis, monitor exit conditions."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from . import exit as exit_module
from . import journal, notify

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
    base: str,
    exchange: str,
    price: float,
    size_usd: float,
    leverage: int,
    score: int,
    setup_context: dict[str, Any],
    cfg: Config,
) -> None:
    params = exit_module.exit_params(setup_context.get("pump_pct"))
    entry = {
        "base": base,
        "exchange": exchange,
        "side": "short",
        "entry_price": price,
        "size_usd": size_usd,
        "leverage": leverage,
        "opened_at": time.time(),
        "score": score,
        "exit_params": params,
    }
    await rdb.set(paper_key(exchange, base), json.dumps(entry), ex=86400 * 7)

    if cfg.db_url:
        trade_id = await journal.open_trade(
            cfg.db_url,
            base=base,
            exchange=exchange,
            order_id=None,
            size_usd=size_usd,
            leverage=leverage,
            entry_price=price,
            setup_context={**setup_context, "paper": True},
        )
        if trade_id:
            await rdb.set(
                _TRADE_ID_KEY.format(exchange=exchange, base=base.upper()),
                str(trade_id),
                ex=86400 * 7,
            )

    creds = notify.credentials(cfg)
    if creds:
        await notify.notify_open(
            *creds,
            base=base,
            exchange=exchange,
            size_usd=size_usd,
            leverage=leverage,
            price=price,
            score=score,
            paper=True,
        )

    log.info("paper.opened", base=base, exchange=exchange, price=price, score=score)


async def close_paper(
    rdb: Any,
    *,
    pos: dict[str, Any],
    current_price: float,
    reason: str,
    cfg: Config,
) -> None:
    base = pos["base"]
    exchange = pos["exchange"]
    entry_price = float(pos["entry_price"])
    side = pos.get("side", "short")

    pnl_pct = (
        (entry_price - current_price) / entry_price * 100
        if side == "short"
        else (current_price - entry_price) / entry_price * 100
    )

    await rdb.delete(paper_key(exchange, base))

    # Paper trades are deliberately NOT routed through journal.try_commit_close:
    # that mechanism writes a journal:pending_close marker that tracker.py
    # treats as "a real close is outstanding" and withholds the trading-ready
    # lease for. A stuck paper-trade journal write must never block real
    # order placement. Best-effort commit + log is enough here — paper stats
    # are informational, not part of the daily-loss circuit breaker.
    trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base.upper())
    trade_id_raw = await rdb.get(trade_id_key)
    if trade_id_raw and cfg.db_url:
        trade_id = int(trade_id_raw)
        committed = await journal.close_trade(
            cfg.db_url,
            trade_id=trade_id,
            exit_order_id=None,
            exit_price=current_price,
            reason=reason,
        )
        if committed:
            await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        else:
            log.error(
                "paper.journal_close_failed",
                base=base,
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
            pnl_pct=pnl_pct,
            reason=reason,
            paper=True,
        )

    log.info("paper.closed", base=base, exchange=exchange, pnl_pct=round(pnl_pct, 2), reason=reason)


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
        raw = await rdb.get(key)
        if not raw:
            continue
        try:
            pos = json.loads(raw)
        except Exception as exc:
            log.warning("paper.bad_payload", key=str(key), err=str(exc))
            continue

        base = pos["base"]
        exchange = pos["exchange"]
        entry_price = float(pos["entry_price"])
        opened_at = float(pos.get("opened_at", 0))
        side = pos.get("side", "short")

        ex = exchanges.get(exchange)
        if not ex:
            continue

        try:
            ticker = await ex.fetch_ticker(f"{base.upper()}/USDT:USDT")
            mark = float(ticker.get("last") or 0)
        except Exception as exc:
            log.warning("paper.ticker_failed", base=base, exchange=exchange, err=str(exc))
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
            await close_paper(rdb, pos=pos, current_price=mark, reason=reason, cfg=cfg)
