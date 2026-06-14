import argparse
import asyncio

import redis.asyncio as aioredis
import structlog

from .config import Config
from .persistence import upsert_pumps
from .scanner import run_once

log = structlog.get_logger()


async def _run(once: bool) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    cfg = Config()
    log.info(
        "scanner.starting",
        exchanges=cfg.exchanges,
        min_pct=cfg.min_pct,
        interval=cfg.interval,
        db=bool(cfg.db_url),
    )

    rdb: aioredis.Redis = aioredis.from_url(f"redis://{cfg.redis_addr}")
    try:
        while True:
            pumps = await run_once(cfg.exchanges, cfg.min_pct, rdb)
            if pumps and cfg.db_url:
                await upsert_pumps(cfg.db_url, pumps)
            if once:
                break
            await asyncio.sleep(cfg.interval)
    finally:
        await rdb.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-exchange pump scanner")
    parser.add_argument("--once", action="store_true", help="Run one scan then exit")
    args = parser.parse_args()
    asyncio.run(_run(args.once))
