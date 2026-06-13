import argparse
import asyncio

import redis.asyncio as aioredis
import structlog

from .config import Config
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
    )

    rdb: aioredis.Redis = aioredis.from_url(f"redis://{cfg.redis_addr}")
    try:
        while True:
            await run_once(cfg.exchanges, cfg.min_pct, rdb)
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
