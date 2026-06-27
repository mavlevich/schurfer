from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from . import decisions, journal, notify, paper, risk
from . import exit as exit_module
from .orders import place_order

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_INTERVAL_SECONDS = 60
_PUMPS_KEY = "pumps:latest"
_SIGNALS_KEY = "signals:{base}"
_SEEN_KEY = "trader:seen:{base}"
_TRADE_ID_KEY = "trade:id:{exchange}:{base}"
_SEEN_TTL_TRADED = 86400  # 24h — don't re-enter the same token after a trade
_SEEN_TTL_SKIP = 1800  # 30min — recheck sooner when skipped
_SIGNALS_MAX_AGE = 90  # reject cached score older than 1.5x ticker interval


async def run_signal_trader(
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
        except Exception as e:
            log.error("trader.error", err=str(e))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    raw = await rdb.get(_PUMPS_KEY)
    if not raw:
        return

    pumps_data = json.loads(raw)
    pumps = pumps_data.get("pumps", [])
    if not pumps:
        return

    log.debug("trader.tick", pump_count=len(pumps))

    for pump in pumps:
        base = pump.get("base", "")
        if not base:
            continue

        seen_key = _SEEN_KEY.format(base=base)
        if await rdb.get(seen_key):
            continue

        pump_pct: float | None = pump.get("max_change_pct")
        exchange = _pick_exchange(pump.get("exchanges", []), exchanges)
        if not exchange:
            log.info("trader.skip.no_exchange", base=base)
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
            decisions.write_decision(
                cfg.db_url,
                base=base,
                exchange="",
                action="skipped",
                reason="no_configured_exchange",
                pump_pct=pump_pct,
            )
            continue

        score = await _fetch_score(base, rdb)
        if score < cfg.score_threshold:
            log.info("trader.skip.score", base=base, score=score, threshold=cfg.score_threshold)
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
            decisions.write_decision(
                cfg.db_url,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=f"score {score} < threshold {cfg.score_threshold}",
                score=score,
                pump_pct=pump_pct,
            )
            continue

        setup_context = {
            "score": score,
            "pump_pct": pump_pct,
            "exchanges": pump.get("exchanges", []),
            "signals_ts": pumps_data.get("ts"),
        }

        exit_params = exit_module.exit_params(setup_context.get("pump_pct"))
        liq_check = risk.check_liquidation_distance(
            exit_params["initial_sl_pct"],
            cfg.signal_leverage,
            cfg.liquidation_buffer_pct,
        )
        if not liq_check.allowed:
            log.info("trader.skip.liquidation_guard", base=base, reason=liq_check.reason)
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
            decisions.write_decision(
                cfg.db_url,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=liq_check.reason,
                score=score,
                pump_pct=pump_pct,
            )
            continue

        if cfg.dry_run:
            ex = exchanges.get(exchange)
            if not ex:
                await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
                continue
            try:
                ticker = await ex.fetch_ticker(f"{base.upper()}/USDT:USDT")
                price = float(ticker["last"])
            except Exception as exc:
                log.warning("trader.dry_run.price_failed", base=base, err=str(exc))
                await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
                continue

            await paper.open_paper(
                rdb,
                base=base,
                exchange=exchange,
                price=price,
                size_usd=cfg.signal_position_usd,
                leverage=cfg.signal_leverage,
                score=score,
                setup_context=setup_context,
                cfg=cfg,
            )
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_TRADED)
            decisions.write_decision(
                cfg.db_url,
                base=base,
                exchange=exchange,
                action="opened_dry_run",
                reason="paper trade",
                score=score,
                pump_pct=pump_pct,
            )
            continue

        result = await place_order(
            base=base,
            exchange=exchange,
            side="short",
            size_usd=cfg.signal_position_usd,
            leverage=cfg.signal_leverage,
            exchanges=exchanges,
            rdb=rdb,
            max_positions=cfg.max_positions,
            max_position_usd=cfg.max_position_usd,
            daily_loss_limit_usd=cfg.daily_loss_limit_usd,
            initial_sl_pct=exit_params["initial_sl_pct"],
            liquidation_buffer_pct=cfg.liquidation_buffer_pct,
        )

        if result.get("allowed"):
            log.info(
                "trader.opened",
                base=base,
                exchange=exchange,
                score=score,
                order_id=result.get("order_id"),
            )
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_TRADED)
            decisions.write_decision(
                cfg.db_url,
                base=base,
                exchange=exchange,
                action="opened",
                reason="ok",
                score=score,
                pump_pct=pump_pct,
            )

            entry_price = result.get("price", 0)
            await rdb.set(
                exit_module.params_key(exchange, base),
                json.dumps(exit_params),
                ex=_SEEN_TTL_TRADED,
            )
            await rdb.set(
                exit_module.entry_key(exchange, base),
                str(entry_price),
                ex=_SEEN_TTL_TRADED,
            )
            await rdb.set(
                exit_module.side_key(exchange, base),
                "short",
                ex=_SEEN_TTL_TRADED,
            )

            if cfg.db_url:
                trade_id = await journal.open_trade(
                    cfg.db_url,
                    base=base,
                    exchange=exchange,
                    order_id=result.get("order_id"),
                    size_usd=cfg.signal_position_usd,
                    leverage=cfg.signal_leverage,
                    entry_price=entry_price,
                    setup_context=setup_context,
                )
                if trade_id:
                    await rdb.set(
                        _TRADE_ID_KEY.format(exchange=exchange, base=base.upper()),
                        str(trade_id),
                        ex=_SEEN_TTL_TRADED,
                    )

            creds = notify.credentials(cfg)
            if creds:
                await notify.notify_open(
                    *creds,
                    base=base,
                    exchange=exchange,
                    size_usd=cfg.signal_position_usd,
                    leverage=cfg.signal_leverage,
                    price=result.get("price", 0),
                    score=score,
                    paper=False,
                )
        else:
            blocked_reason = result.get("reason", "unknown")
            log.info("trader.blocked", base=base, reason=blocked_reason)
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
            decisions.write_decision(
                cfg.db_url,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=blocked_reason,
                score=score,
                pump_pct=pump_pct,
            )


async def _fetch_score(base: str, rdb: Any) -> int:
    raw = await rdb.get(_SIGNALS_KEY.format(base=base))
    if not raw:
        return 0
    try:
        data = json.loads(raw)
        computed_at = data.get("computed_at", 0)
        if computed_at and (time.time() - computed_at) > _SIGNALS_MAX_AGE:
            log.warning("trader.score.stale", base=base, age=int(time.time() - computed_at))
            return 0
        return int(data.get("score", 0))
    except Exception:
        return 0


def _pick_exchange(
    pump_exchanges: list[dict[str, Any]],
    configured: dict[str, Any],
) -> str | None:
    """Return the highest-volume pump exchange that is configured in the execution service."""
    candidates = [e for e in pump_exchanges if e.get("exchange") in configured]
    if not candidates:
        return None
    best = max(candidates, key=lambda e: float(e.get("volume_24h_usd") or 0))
    return str(best["exchange"])
