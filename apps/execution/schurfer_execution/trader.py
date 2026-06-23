from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from .orders import place_order

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_INTERVAL_SECONDS = 60
_PUMPS_KEY = "pumps:latest"
_SIGNALS_KEY = "signals:{base}"
_SEEN_KEY = "trader:seen:{base}"
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

    pumps = json.loads(raw).get("pumps", [])
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

        exchange = _pick_exchange(pump.get("exchanges", []), exchanges)
        if not exchange:
            log.info("trader.skip.no_exchange", base=base)
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
            continue

        score = await _fetch_score(base, rdb)
        if score < cfg.score_threshold:
            log.info("trader.skip.score", base=base, score=score, threshold=cfg.score_threshold)
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
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
        else:
            log.info("trader.blocked", base=base, reason=result.get("reason"))
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)


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
